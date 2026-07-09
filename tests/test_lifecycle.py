from __future__ import annotations

import json

import pandas as pd

from etf_flow_monitor.data.lifecycle import (
    MATCH_STATUS_MANUAL_CONFIRMED,
    apply_flow_adjustments_to_snapshot,
    apply_manual_confirmations,
    build_announcement_request_plan,
    build_flow_adjustments_from_audit,
    build_lifecycle_review_plans,
    classify_lifecycle_announcement,
    detect_share_jumps,
    extract_lifecycle_events_from_announcements,
    match_share_jumps_to_events,
)
from etf_flow_monitor.data.cninfo_announcements import normalize_cninfo_rows
from etf_flow_monitor.data.exchange_announcements import normalize_sse_rows, normalize_szse_rows
from etf_flow_monitor.utils.calendar import trading_calendar_from_frame
from tools.build_etf_lifecycle_table import (
    _status_is_current,
    _update_lifecycle_status,
    load_cached_share_cross_sections,
)
from tools.update_etf_announcements import (
    _build_exchange_fallback_jobs_for_empty_windows,
    _merge_exchange_request_windows,
    build_auto_confirmations_from_retried_no_announcements,
    build_auto_confirmations_from_retried_windows,
    build_liquidation_follow_up_jobs,
    build_pending_confirmations_from_request_plan,
    fetch_cninfo_announcements_with_exchange_fallback,
    fetch_exchange_announcements,
    fetch_tushare_announcements,
    merge_announcement_frames,
    normalize_announcement_frame,
    prepare_announcements_for_csv,
)
from etf_flow_monitor.data.tushare_http import TusharePermissionError


def test_extract_lifecycle_events_from_announcements_detects_share_conversion() -> None:
    announcements = pd.DataFrame(
        [
            {
                "ts_code": "510300.SH",
                "ann_date": "20260105",
                "event_date": "20260106",
                "title": "关于华泰柏瑞沪深300ETF基金份额折算结果的公告",
                "source_url": "https://example.com/ann",
            }
        ]
    )
    basic = pd.DataFrame([{"fund_code": "510300.SH", "name": "华泰柏瑞沪深300ETF"}])

    events = extract_lifecycle_events_from_announcements(announcements, basic=basic)

    assert len(events) == 1
    assert events.loc[0, "fund_code"] == "510300.SH"
    assert events.loc[0, "name"] == "华泰柏瑞沪深300ETF"
    assert events.loc[0, "event_type"] == "share_conversion"
    assert events.loc[0, "event_keyword"] == "基金份额折算"
    assert events.loc[0, "event_date"] == pd.Timestamp("2026-01-06")
    assert events.loc[0, "event_date_source"] == "explicit"


def test_lifecycle_status_invalidates_when_cached_start_moves_earlier(tmp_path) -> None:
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": "etf_lifecycle_status_v1",
                "data_latest_date": "20260626",
                "verified_through": "20260626",
                "lifecycle_current": True,
                "request_plan_rows": 0,
            }
        ),
        encoding="utf-8",
    )

    assert not _status_is_current(
        status_path,
        data_earliest_date=pd.Timestamp("2024-01-01"),
        data_latest_date=pd.Timestamp("2026-06-26"),
    )

    _update_lifecycle_status(
        status_path,
        data_earliest_date=pd.Timestamp("2024-01-01"),
        data_latest_date=pd.Timestamp("2026-06-26"),
        local_cache_start_date=pd.Timestamp("2024-01-01"),
        lifecycle_current=True,
        request_plan_rows=0,
    )
    assert _status_is_current(
        status_path,
        data_earliest_date=pd.Timestamp("2024-01-01"),
        data_latest_date=pd.Timestamp("2026-06-26"),
    )
    assert not _status_is_current(
        status_path,
        data_earliest_date=pd.Timestamp("2023-01-03"),
        data_latest_date=pd.Timestamp("2026-06-26"),
    )


def test_lifecycle_announcement_classification_keeps_liquidation_warnings_out() -> None:
    assert classify_lifecycle_announcement("可能触发基金合同终止情形的提示性公告") == ("", "")
    assert classify_lifecycle_announcement("关于基金合同终止并清算的公告") == ("liquidation", "基金合同终止并清算")
    assert classify_lifecycle_announcement("关于终止上市的公告") == ("liquidation", "终止上市")


def test_share_jump_audit_matches_nearby_lifecycle_event() -> None:
    shares = pd.DataFrame(
        [
            {"ts_code": "510300.SH", "trade_date": "20260105", "fd_share": 1000.0},
            {"ts_code": "510300.SH", "trade_date": "20260106", "fd_share": 2000.0},
        ]
    )
    events = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "event_date": "20260106",
                "announcement_date": "20260105",
                "event_type": "share_split",
                "event_keyword": "份额拆分",
                "title": "关于基金份额拆分的公告",
            }
        ]
    )

    jumps = detect_share_jumps(shares, min_change_pct=0.20)
    audit = match_share_jumps_to_events(jumps, events, match_window_days=3)

    assert len(audit) == 1
    assert audit.loc[0, "share_change"] == 1000.0
    assert audit.loc[0, "share_change_pct"] == 1.0
    assert audit.loc[0, "match_status"] == "matched_lifecycle_event"
    assert audit.loc[0, "matched_event_type"] == "share_split"


def test_announcement_request_plan_uses_configured_trading_day_window() -> None:
    audit = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "name": "沪深300ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "share_change": 1000.0,
                "share_change_pct": 1.0,
                "match_status": "unmatched",
            },
            {
                "fund_code": "159290.SZ",
                "name": "创业板ETF",
                "trade_date": "20260107",
                "prev_trade_date": "20260106",
                "share_change": 500.0,
                "share_change_pct": 0.5,
                "match_status": "matched_lifecycle_event",
            },
        ]
    )
    calendar = trading_calendar_from_frame(
        pd.DataFrame(
            [
                {"exchange": "SSE", "cal_date": "2025-12-31", "is_open": 1, "pretrade_date": "2025-12-30"},
                {"exchange": "SSE", "cal_date": "2026-01-01", "is_open": 0, "pretrade_date": "2025-12-31"},
                {"exchange": "SSE", "cal_date": "2026-01-02", "is_open": 1, "pretrade_date": "2025-12-31"},
                {"exchange": "SSE", "cal_date": "2026-01-05", "is_open": 1, "pretrade_date": "2026-01-02"},
                {"exchange": "SSE", "cal_date": "2026-01-06", "is_open": 1, "pretrade_date": "2026-01-05"},
                {"exchange": "SSE", "cal_date": "2026-01-07", "is_open": 1, "pretrade_date": "2026-01-06"},
                {"exchange": "SSE", "cal_date": "2026-01-08", "is_open": 1, "pretrade_date": "2026-01-07"},
                {"exchange": "SSE", "cal_date": "2026-01-09", "is_open": 1, "pretrade_date": "2026-01-08"},
            ]
        ),
        exchange="SSE",
    )

    plan = build_announcement_request_plan(audit, window_days=2, calendar=calendar)

    assert plan["fund_code"].tolist() == ["510300.SH"]
    assert plan.loc[0, "request_start_date"] == pd.Timestamp("2026-01-02")
    assert plan.loc[0, "request_end_date"] == pd.Timestamp("2026-01-08")


def test_manual_confirmations_remove_jump_from_request_plan() -> None:
    audit = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "name": "沪深300ETF",
                "trade_date": "2026/1/6",
                "prev_trade_date": "20260105.0",
                "share_change": 1000.0,
                "share_change_pct": 1.0,
                "match_status": "unmatched",
            }
        ]
    )
    confirmations = pd.DataFrame(
        [
            {
                "fund_code": '="510300.SH"',
                "name": "沪深300ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "share_change": 1000.0,
                "share_change_pct": 1.0,
            }
        ]
    )

    confirmed = apply_manual_confirmations(audit, confirmations)
    plan = build_announcement_request_plan(confirmed, window_days=2)

    assert confirmed.loc[0, "match_status"] == MATCH_STATUS_MANUAL_CONFIRMED
    assert plan.empty


def test_lifecycle_review_plans_split_high_and_low_suspicion() -> None:
    audit = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "name": "沪深300ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "prev_shares": 1000.0,
                "shares": 2000.0,
                "share_change": 1000.0,
                "share_change_pct": 1.0,
                "match_status": "unmatched",
            },
            {
                "fund_code": "511880.SH",
                "name": "货币ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "prev_shares": 1000.0,
                "shares": 3000.0,
                "share_change": 2000.0,
                "share_change_pct": 2.0,
                "match_status": "unmatched",
            },
            {
                "fund_code": "588000.SH",
                "name": "新上市ETF",
                "trade_date": "20260120",
                "prev_trade_date": "20260119",
                "prev_shares": 1000.0,
                "shares": 4000.0,
                "share_change": 3000.0,
                "share_change_pct": 3.0,
                "match_status": "unmatched",
            },
            {
                "fund_code": "159999.SZ",
                "name": "普通申赎ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "prev_shares": 1000.0,
                "shares": 1600.0,
                "share_change": 600.0,
                "share_change_pct": 0.6,
                "match_status": "unmatched",
            },
        ]
    )
    category_map = pd.DataFrame(
        [
            {"fund_code": "510300.SH", "category": "宽基", "fund_type": "股票型", "list_date": "20200101"},
            {"fund_code": "511880.SH", "category": "货币", "fund_type": "货币型", "list_date": "20200101"},
            {"fund_code": "588000.SH", "category": "科技", "fund_type": "股票型", "list_date": "20260101"},
            {"fund_code": "159999.SZ", "category": "宽基", "fund_type": "股票型", "list_date": "20200101"},
        ]
    )

    high, low = build_lifecycle_review_plans(audit, category_map=category_map, window_days=2)

    assert high["fund_code"].tolist() == ["510300.SH"]
    assert high.loc[0, "review_layer"] == "high_suspicion"
    assert "high:positive_integer_ratio" in high.loc[0, "review_reason"]
    assert set(low["fund_code"]) == {"511880.SH", "588000.SH", "159999.SZ"}
    assert low.loc[low["fund_code"].eq("511880.SH"), "review_reason"].item().startswith("low:money_etf_noise")
    assert "low:listed_less_than_60d" in low.loc[low["fund_code"].eq("588000.SH"), "review_reason"].item()
    assert low.loc[low["fund_code"].eq("159999.SZ"), "review_reason"].item() == "low:ordinary_large_subscription_redemption"


def test_load_cached_share_cross_sections_filters_to_etf_universe(tmp_path) -> None:
    cache_file = tmp_path / "cache" / "tushare" / "daily_cross_section" / "etf_share" / "20260106.csv"
    cache_file.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {"ts_code": "510300.SH", "trade_date": "20260106", "fd_share": 1000.0},
            {"ts_code": "000917.OF", "trade_date": "20260106", "fd_share": 2000.0},
        ]
    ).to_csv(cache_file, index=False, encoding="utf-8-sig")

    shares = load_cached_share_cross_sections(tmp_path / "cache", "tushare", codes={"510300.SH"})

    assert shares["fund_code"].tolist() == ["510300.SH"]


def test_update_announcements_normalizes_and_preserves_manual_event_date() -> None:
    existing = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "announcement_date": "20260105",
                "event_date": "20260106",
                "title": "关于基金份额拆分的公告",
                "content": "manual",
                "source_url": "",
            }
        ]
    )
    fresh = normalize_announcement_frame(
        pd.DataFrame(
            [
                {
                    "ts_code": "510300.SH",
                    "ann_date": "20260105",
                    "title": "关于基金份额拆分的公告",
                    "url": "https://example.com/ann",
                }
            ]
        )
    )

    merged = merge_announcement_frames(existing, fresh)
    output = prepare_announcements_for_csv(merged)

    assert len(output) == 1
    assert output.loc[0, "event_date"] == "20260106"
    assert output.loc[0, "content"] == "manual"
    assert output.loc[0, "source_url"] == "https://example.com/ann"


def test_exchange_announcement_normalizers_build_public_urls() -> None:
    sse = normalize_sse_rows(
        [
            {
                "SECURITY_CODE": "510300",
                "SSEDATE": "2026-01-05",
                "TITLE": "关于基金份额折算的公告",
                "URL": "/disclosure/fund/announcement/c/new/2026-01-05/510300.pdf",
                "BULLETIN_TYPE_DESC": "基金运作(基金)",
            }
        ]
    )
    szse = normalize_szse_rows(
        [
            {
                "secCode": ["159290"],
                "publishTime": "2026-06-27 00:00:00",
                "title": "创业板综指增强ETF东财：基金合同",
                "attachPath": "/disc/disk03/finalpage/2026-06-27/sample.PDF",
                "attachFormat": "PDF",
            }
        ]
    )

    assert sse[0]["fund_code"] == "510300.SH"
    assert sse[0]["announcement_date"] == "20260105"
    assert sse[0]["source_url"].startswith("https://www.sse.com.cn/disclosure/")
    assert szse[0]["fund_code"] == "159290.SZ"
    assert szse[0]["announcement_date"] == "20260627"
    assert szse[0]["source_url"] == "https://disc.static.szse.cn/download/disc/disk03/finalpage/2026-06-27/sample.PDF"


def test_cninfo_announcement_normalizer_builds_fund_rows() -> None:
    rows = normalize_cninfo_rows(
        [
            {
                "secCode": "159211",
                "secName": "深证100ETF富国",
                "orgId": "jjjl0000040",
                "announcementTitle": "富国基金管理有限公司关于新增方正证券股份有限公司为部分基金申购赎回代理券商的公告",
                "announcementTime": 1751558400000,
                "adjunctUrl": "finalpage/2025-07-04/1224071363.PDF",
                "adjunctType": "PDF",
            }
        ],
        fallback_fund_code="159211.SZ",
    )

    assert rows == [
        {
            "fund_code": "159211.SZ",
            "announcement_date": "20250704",
            "event_date": "",
            "title": "富国基金管理有限公司关于新增方正证券股份有限公司为部分基金申购赎回代理券商的公告",
            "content": "",
            "source_url": "https://static.cninfo.com.cn/finalpage/2025-07-04/1224071363.PDF",
        }
    ]


def test_exchange_request_windows_merge_by_fund() -> None:
    plan = pd.DataFrame(
        [
            {"fund_code": "159290.SZ", "request_start_date": "20260101", "request_end_date": "20260110"},
            {"fund_code": "159290.SZ", "request_start_date": "20260108", "request_end_date": "20260115"},
            {"fund_code": "510300.SH", "request_start_date": "20260201", "request_end_date": "20260203"},
        ]
    )
    plan["request_start_date"] = pd.to_datetime(plan["request_start_date"])
    plan["request_end_date"] = pd.to_datetime(plan["request_end_date"])

    jobs = _merge_exchange_request_windows(plan)

    assert jobs == [
        {"fund_code": "159290.SZ", "start_date": pd.Timestamp("2026-01-01"), "end_date": pd.Timestamp("2026-01-15")},
        {"fund_code": "510300.SH", "start_date": pd.Timestamp("2026-02-01"), "end_date": pd.Timestamp("2026-02-03")},
    ]


def test_pending_confirmations_are_created_only_when_window_has_no_announcements() -> None:
    plan = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "name": "沪深300ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "request_start_date": "20260102",
                "request_end_date": "20260108",
                "share_change": 1000.0,
                "share_change_pct": 1.0,
            },
            {
                "fund_code": "159290.SZ",
                "name": "创业板ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "request_start_date": "20260102",
                "request_end_date": "20260108",
                "share_change": 500.0,
                "share_change_pct": 0.5,
            },
            {
                "fund_code": "588000.SH",
                "name": "科创ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "request_start_date": "20260102",
                "request_end_date": "20260108",
                "share_change": 300.0,
                "share_change_pct": 0.3,
            },
        ]
    )
    announcements = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "announcement_date": "20260105",
                "title": "关于基金份额折算的公告",
                "source_url": "https://example.com/a.pdf",
            }
        ]
    )

    pending = build_pending_confirmations_from_request_plan(plan, announcements, failed_codes={"588000.SH"})

    assert pending["fund_code"].tolist() == ["159290.SZ"]
    assert pending.loc[0, "status"] == "no_announcement_found"


def test_liquidation_warning_builds_forward_follow_up_job() -> None:
    plan = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "trade_date": "20260106",
                "request_start_date": "20260102",
                "request_end_date": "20260108",
            }
        ]
    )
    announcements = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "announcement_date": "20260105",
                "title": "关于可能触发基金合同终止情形的提示性公告",
                "content": "",
                "source_url": "https://example.com/warning.pdf",
            }
        ]
    )

    jobs = build_liquidation_follow_up_jobs(plan, announcements, follow_up_end_date="20260131")

    assert jobs == [{"fund_code": "159290.SZ", "start_date": pd.Timestamp("2026-01-05"), "end_date": pd.Timestamp("2026-01-31")}]


def test_retried_no_announcement_rows_are_auto_confirmed() -> None:
    previous = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "name": "创业板ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "share_change": 500.0,
                "share_change_pct": 0.5,
                "status": "no_announcement_found",
            }
        ]
    )
    current = previous.copy()

    confirmations = build_auto_confirmations_from_retried_no_announcements(current, previous)

    assert confirmations["fund_code"].tolist() == ["159290.SZ"]
    assert confirmations.loc[0, "confirm_note"] == "auto_confirmed_no_announcement_after_retry"


def test_retried_non_lifecycle_window_is_auto_confirmed() -> None:
    previous = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "name": "创业板ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "share_change": 500.0,
                "share_change_pct": 0.5,
                "status": "no_announcement_found",
            }
        ]
    )
    request_plan = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "name": "创业板ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "request_start_date": "20251220",
                "request_end_date": "20260120",
                "share_change": 500.0,
                "share_change_pct": 0.5,
            }
        ]
    )
    announcements = pd.DataFrame(
        [
            {
                "fund_code": "159290.SZ",
                "announcement_date": "20260115",
                "title": "关于新增流动性服务商的公告",
                "content": "",
            }
        ]
    )

    confirmations = build_auto_confirmations_from_retried_windows(request_plan, announcements, previous)

    assert confirmations["fund_code"].tolist() == ["159290.SZ"]
    assert confirmations.loc[0, "confirm_note"] == "auto_confirmed_no_lifecycle_after_retry"


def test_flow_adjustments_zero_matched_lifecycle_net_flow() -> None:
    audit = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "name": "沪深300ETF",
                "trade_date": "20260106",
                "prev_trade_date": "20260105",
                "share_change": 1000.0,
                "share_change_pct": 1.0,
                "match_status": "matched_lifecycle_event",
                "matched_event_type": "share_split",
                "matched_event_date": "20260106",
                "matched_announcement_date": "20260105",
                "matched_event_keyword": "份额拆分",
                "matched_event_title": "关于基金份额拆分的公告",
                "matched_source_url": "https://example.com/split.pdf",
            }
        ]
    )
    flow = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "trade_date": "20260106",
                "estimated_net_flow": 100_000_000.0,
                "flow_direction": "inflow",
            }
        ]
    )

    adjustments = build_flow_adjustments_from_audit(audit)
    adjusted = apply_flow_adjustments_to_snapshot(flow, adjustments)

    assert adjustments.loc[0, "adjustment_action"] == "zero_estimated_net_flow"
    assert adjusted.loc[0, "estimated_net_flow_raw"] == 100_000_000.0
    assert adjusted.loc[0, "estimated_net_flow"] == 0.0
    assert adjusted.loc[0, "estimated_net_flow_adjustment"] == -100_000_000.0
    assert adjusted.loc[0, "flow_direction"] == "flat"


class FakeExchangeAnnouncementClient:
    sleep_seconds = 0.0

    def fetch(self, fund_code, *, start_date, end_date):
        return [
            {
                "fund_code": fund_code,
                "announcement_date": "20260105",
                "event_date": "",
                "title": "关于基金份额折算的公告",
                "content": "",
                "source_url": "https://example.com/ann.pdf",
            }
        ]


def test_fetch_exchange_announcements_normalizes_rows() -> None:
    fresh, errors, skipped_reason = fetch_exchange_announcements(
        FakeExchangeAnnouncementClient(),
        [{"fund_code": "510300.SH", "start_date": pd.Timestamp("2026-01-01"), "end_date": pd.Timestamp("2026-01-10")}],
    )

    assert errors == []
    assert skipped_reason == ""
    assert fresh.loc[0, "fund_code"] == "510300.SH"
    assert fresh.loc[0, "announcement_date"] == pd.Timestamp("2026-01-05")


def test_exchange_fallback_jobs_only_include_empty_successful_windows() -> None:
    jobs = [
        {"fund_code": "510300.SH", "start_date": pd.Timestamp("2026-01-01"), "end_date": pd.Timestamp("2026-01-10")},
        {"fund_code": "561310.SH", "start_date": pd.Timestamp("2026-07-01"), "end_date": pd.Timestamp("2026-07-15")},
        {"fund_code": "588000.SH", "start_date": pd.Timestamp("2026-07-01"), "end_date": pd.Timestamp("2026-07-15")},
    ]
    announcements = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "announcement_date": "20260105",
                "title": "关于基金份额折算的公告",
                "source_url": "https://example.com/hit.pdf",
            }
        ]
    )

    fallback_jobs = _build_exchange_fallback_jobs_for_empty_windows(jobs, announcements, failed_codes={"588000.SH"})

    assert fallback_jobs == [
        {"fund_code": "561310.SH", "start_date": pd.Timestamp("2026-07-01"), "end_date": pd.Timestamp("2026-07-15")}
    ]


class EmptyCninfoAnnouncementClient:
    sleep_seconds = 0.0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, fund_code, *, start_date, end_date):
        self.calls.append(fund_code)
        return []


class ExchangeFallbackAnnouncementClient:
    sleep_seconds = 0.0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, fund_code, *, start_date, end_date):
        self.calls.append(fund_code)
        return [
            {
                "fund_code": fund_code,
                "announcement_date": "20260703",
                "event_date": "",
                "title": "国泰基金管理有限公司关于国泰中证消费电子主题交易型开放式指数证券投资基金实施基金份额拆分并调整最小申购、赎回单位及相关业务安排的公告",
                "content": "基金运作(基金) 基金折算/拆分",
                "source_url": "https://www.sse.com.cn/disclosure/fund/announcement/c/new/2026-07-03/561310_20260703_0PCA.pdf",
            }
        ]


def test_cninfo_empty_window_falls_back_to_exchange() -> None:
    cninfo_client = EmptyCninfoAnnouncementClient()
    exchange_client = ExchangeFallbackAnnouncementClient()
    jobs = [{"fund_code": "561310.SH", "start_date": pd.Timestamp("2026-07-01"), "end_date": pd.Timestamp("2026-07-15")}]

    fresh, errors, skipped_reason, detail = fetch_cninfo_announcements_with_exchange_fallback(
        cninfo_client,
        exchange_client,
        jobs,
        retries=0,
    )

    assert cninfo_client.calls == ["561310.SH"]
    assert exchange_client.calls == ["561310.SH"]
    assert errors == []
    assert skipped_reason == ""
    assert detail == {"exchange_fallback_jobs": 1, "exchange_fallback_rows": 1, "exchange_fallback_errors": 0}
    assert fresh.loc[0, "fund_code"] == "561310.SH"
    assert fresh.loc[0, "announcement_date"] == pd.Timestamp("2026-07-03")
    assert classify_lifecycle_announcement(fresh.loc[0, "title"])[0] == "share_split"


class FlakyExchangeAnnouncementClient:
    sleep_seconds = 0.0

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, fund_code, *, start_date, end_date):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("Remote end closed connection without response")
        return [
            {
                "fund_code": fund_code,
                "announcement_date": "20260105",
                "event_date": "",
                "title": "关于基金份额折算的公告",
                "content": "",
                "source_url": "https://example.com/ann.pdf",
            }
        ]


def test_fetch_exchange_announcements_retries_transient_connection_drop() -> None:
    client = FlakyExchangeAnnouncementClient()

    fresh, errors, skipped_reason = fetch_exchange_announcements(
        client,
        [{"fund_code": "159540.SZ", "start_date": pd.Timestamp("2026-01-01"), "end_date": pd.Timestamp("2026-01-10")}],
        retries=1,
        retry_sleep_seconds=0,
    )

    assert client.calls == 2
    assert errors == []
    assert skipped_reason == ""
    assert fresh.loc[0, "fund_code"] == "159540.SZ"


class FailingExchangeAnnouncementClient:
    sleep_seconds = 0.0

    def fetch(self, fund_code, *, start_date, end_date):
        raise ConnectionError("Remote end closed connection without response")


def test_fetch_exchange_announcements_records_failures_without_default_abort() -> None:
    fresh, errors, skipped_reason = fetch_exchange_announcements(
        FailingExchangeAnnouncementClient(),
        [
            {"fund_code": "159540.SZ", "start_date": pd.Timestamp("2026-01-01"), "end_date": pd.Timestamp("2026-01-10")},
            {"fund_code": "159543.SZ", "start_date": pd.Timestamp("2026-02-01"), "end_date": pd.Timestamp("2026-02-10")},
        ],
        retries=0,
        max_errors=1,
    )

    assert fresh.empty
    assert skipped_reason == ""
    assert [item["fund_code"] for item in errors] == ["159540.SZ", "159543.SZ"]


class PermissionDeniedAnnouncementClient:
    def __init__(self) -> None:
        self.calls = 0

    def query(self, api_name, params=None, fields=None):
        self.calls += 1
        raise TusharePermissionError("Tushare permission denied: anns_d code=40203 msg=抱歉，您没有接口(anns_d)访问权限")


def test_fetch_tushare_announcements_stops_after_permission_error() -> None:
    client = PermissionDeniedAnnouncementClient()

    fresh, errors, skipped_reason = fetch_tushare_announcements(
        client,
        ["159001.SZ", "159003.SZ"],
        start_date=pd.Timestamp("2024-01-01"),
        end_date=pd.Timestamp("2026-06-27"),
        api_name="anns_d",
        fields="ts_code,ann_date,title",
    )

    assert client.calls == 1
    assert fresh.empty
    assert len(errors) == 1
    assert "没有接口" in skipped_reason
