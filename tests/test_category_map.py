from __future__ import annotations

import pandas as pd

from etf_flow_monitor.data.category_map import apply_category_map, category_map_stats, load_category_map
from etf_flow_monitor.data.cross_border_fill import fill_cross_border_previous_values
from etf_flow_monitor.utils.calendar import resolve_monitor_market_date, trading_calendar_from_frame
from tools.validate_category_map import validate_category_map
from tools.build_etf_category_map import infer_category, infer_etf_candidate
from tools.refine_category_map_with_sw import refine_category_map


def test_infer_category_does_not_treat_stock_benchmark_bond_leg_as_bond() -> None:
    category, subcategory, rule = infer_category(
        fund_type="股票型",
        benchmark="沪深300指数×80%+中证全债指数×20%",
        name="华夏沪深300ETF",
    )

    assert category == "宽基"
    assert subcategory == "沪深300"
    assert rule == "fund_type/benchmark/name:broad_index"


def test_infer_category_prioritizes_broad_index_over_securities_word() -> None:
    category, subcategory, rule = infer_category(
        fund_type="股票型",
        benchmark="上海证券交易所50成份指数×100%",
        name="华夏上证50ETF",
    )
    manager_name_category, manager_name_subcategory, _ = infer_category(
        fund_type="股票型",
        benchmark="中证500指数收益率×100%",
        name="中银证券中证500ETF",
    )

    assert category == "宽基"
    assert subcategory == "上证50"
    assert rule == "fund_type/benchmark/name:broad_index"
    assert (manager_name_category, manager_name_subcategory) == ("宽基", "中证500")


def test_infer_etf_candidate_excludes_lof() -> None:
    is_candidate, rule = infer_etf_candidate(fund_code="160641.SZ", fund_type="债券型", name="鹏华丰锐债券(LOF)")

    assert is_candidate is False
    assert rule == "exclude:name_non_etf"


def test_infer_category_splits_hong_kong_and_overseas() -> None:
    hk_category, hk_subcategory, _ = infer_category(
        fund_type="股票型",
        benchmark="恒生港股通科技主题指数收益率×100%",
        name="恒生港股通科技ETF",
    )
    overseas_category, overseas_subcategory, _ = infer_category(
        fund_type="股票型",
        benchmark="纳斯达克100指数收益率×100%",
        name="纳斯达克100ETF(QDII)",
    )

    assert (hk_category, hk_subcategory) == ("港股", "港股")
    assert (overseas_category, overseas_subcategory) == ("海外", "海外")


def test_infer_category_does_not_classify_free_cash_flow_as_money() -> None:
    category, subcategory, rule = infer_category(
        fund_type="股票型",
        benchmark="国证自由现金流指数收益率×100%",
        name="华夏国证自由现金流ETF",
    )

    assert category == "红利价值"
    assert subcategory == "自由现金流"
    assert rule == "fund_type/benchmark/name:free_cash_flow"


def test_apply_category_map_merges_manual_fields() -> None:
    flow = pd.DataFrame(
        [
            {"fund_code": "510300.SH", "trade_date": "2026-06-25", "estimated_net_flow": 1.0},
            {"fund_code": "159999.SZ", "trade_date": "2026-06-25", "estimated_net_flow": 2.0},
        ]
    )
    category_map = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "category": "宽基",
                "subcategory": "沪深300",
                "review_note": "人工确认",
            }
        ]
    )

    merged = apply_category_map(flow, category_map)

    assert merged.loc[0, "category"] == "宽基"
    assert merged.loc[0, "subcategory"] == "沪深300"
    assert merged.loc[0, "review_note"] == "人工确认"
    assert merged.loc[0, "category_source"] == "manual_map"
    assert merged.loc[1, "category"] == "其他"
    assert merged.loc[1, "subcategory"] == "待人工确认"
    assert merged.loc[1, "category_source"] == "unmapped"
    assert category_map_stats(merged) == {
        "category_mapped_rows": 1,
        "category_unmapped_rows": 1,
        "category_mapped_funds": 1,
        "category_unmapped_funds": 1,
    }


def test_category_map_loader_accepts_excel_formula_text_and_chinese(tmp_path) -> None:
    path = tmp_path / "etf_category_map.csv"
    path.write_text(
        "\ufefffund_code,category,subcategory,review_note\n"
        '="510300.SH",电子,半导体材料,人工确认\n',
        encoding="utf-8",
    )

    category_map = load_category_map(path)

    assert category_map.loc[0, "fund_code"] == "510300.SH"
    assert category_map.loc[0, "category"] == "电子"
    assert category_map.loc[0, "subcategory"] == "半导体材料"


def test_category_map_loader_accepts_gb18030_excel_csv(tmp_path) -> None:
    path = tmp_path / "etf_category_map.csv"
    payload = "fund_code,category,subcategory,review_note\n510300.SH,电子,半导体材料,ANSI保存\n"
    path.write_bytes(payload.encode("gb18030"))

    category_map = load_category_map(path)

    assert category_map.loc[0, "fund_code"] == "510300.SH"
    assert category_map.loc[0, "category"] == "电子"
    assert category_map.loc[0, "subcategory"] == "半导体材料"

def test_trading_calendar_from_frame_maps_closed_day() -> None:
    frame = pd.DataFrame(
        [
            {"exchange": "SSE", "cal_date": "2026-06-24", "is_open": 1, "pretrade_date": "2026-06-23"},
            {"exchange": "SSE", "cal_date": "2026-06-25", "is_open": 1, "pretrade_date": "2026-06-24"},
            {"exchange": "SSE", "cal_date": "2026-06-26", "is_open": 0, "pretrade_date": "2026-06-25"},
        ]
    )

    calendar = trading_calendar_from_frame(frame, exchange="SSE")

    assert calendar.resolve_request_date_market_date(pd.Timestamp("2026-06-26").date()).isoformat() == "2026-06-25"


def test_monitor_market_date_rolls_back_when_explicit_request_is_today() -> None:
    calendar = trading_calendar_from_frame(
        pd.DataFrame(
            [
                {"exchange": "SSE", "cal_date": "2026-06-24", "is_open": 1, "pretrade_date": "2026-06-23"},
                {"exchange": "SSE", "cal_date": "2026-06-25", "is_open": 1, "pretrade_date": "2026-06-24"},
                {"exchange": "SSE", "cal_date": "2026-06-26", "is_open": 1, "pretrade_date": "2026-06-25"},
            ]
        ),
        exchange="SSE",
    )

    market_date, mode = resolve_monitor_market_date(
        calendar,
        pd.Timestamp("2026-06-26").date(),
        explicit_request=True,
        current_date=pd.Timestamp("2026-06-26").date(),
    )
    historical_date, historical_mode = resolve_monitor_market_date(
        calendar,
        pd.Timestamp("2026-06-25").date(),
        explicit_request=True,
        current_date=pd.Timestamp("2026-06-26").date(),
    )

    assert market_date == pd.Timestamp("2026-06-25").date()
    assert mode == "explicit_current_date_latest_available_market_date"
    assert historical_date == pd.Timestamp("2026-06-25").date()
    assert historical_mode == "explicit_request_date"


def test_validate_category_map_flags_duplicates(tmp_path) -> None:
    path = tmp_path / "category.csv"
    pd.DataFrame(
        [
            {"fund_code": "510300.SH", "category": "宽基", "subcategory": "沪深300", "review_note": ""},
            {"fund_code": "510300.SH", "category": "宽基", "subcategory": "沪深300", "review_note": ""},
        ]
    ).to_csv(path, index=False, encoding="utf-8-sig")

    payload = validate_category_map(path)

    assert payload["summary"]["blocking_issue_count"] == 1
    assert payload["issues"][0]["kind"] == "duplicate_fund_code"


def test_fill_cross_border_previous_values_carries_missing_trade_date() -> None:
    daily = pd.DataFrame(
        [
            {"ts_code": "159792.SZ", "trade_date": "20260624", "close": 0.5, "vol": 1, "amount": 10},
            {"ts_code": "510300.SH", "trade_date": "20260624", "close": 5.0, "vol": 2, "amount": 20},
        ]
    )
    shares = pd.DataFrame(
        [
            {"ts_code": "159792.SZ", "trade_date": "20260624", "fd_share": 1000, "source": "test"},
            {"ts_code": "510300.SH", "trade_date": "20260624", "fd_share": 2000, "source": "test"},
        ]
    )
    category_map = pd.DataFrame(
        [
            {"fund_code": "159792.SZ", "category": "港股", "subcategory": "港股", "review_note": ""},
            {"fund_code": "510300.SH", "category": "宽基", "subcategory": "沪深300", "review_note": ""},
        ]
    )

    filled_daily, filled_shares, stats = fill_cross_border_previous_values(
        daily,
        shares,
        category_map,
        [pd.Timestamp("2026-06-24"), pd.Timestamp("2026-06-25")],
    )

    assert stats["cross_border_daily_ffill_rows"] == 1
    assert stats["cross_border_share_ffill_rows"] == 1
    assert filled_daily.loc[filled_daily["trade_date"].eq(pd.Timestamp("2026-06-25")), "fund_code"].tolist() == ["159792.SZ"]
    assert filled_shares.loc[filled_shares["trade_date"].eq(pd.Timestamp("2026-06-25")), "fund_code"].tolist() == ["159792.SZ"]


def test_refine_category_map_with_sw_skips_composite_index_and_maps_securities() -> None:
    category_map = pd.DataFrame(
        [
            {
                "fund_code": "510140.SH",
                "name": "华夏上证综合ETF",
                "category": "宽基",
                "subcategory": "指数增强/其他宽基候选",
                "fund_type": "股票型",
                "benchmark": "上证综合指数收益率×100%",
                "review_note": "",
            },
            {
                "fund_code": "512000.SH",
                "name": "券商ETF",
                "category": "金融地产",
                "subcategory": "金融地产",
                "fund_type": "股票型",
                "benchmark": "中证全指证券公司指数收益率×100%",
                "review_note": "",
            },
            {
                "fund_code": "510050.SH",
                "name": "华夏上证50ETF",
                "category": "宽基",
                "subcategory": "上证50",
                "fund_type": "股票型",
                "benchmark": "上海证券交易所50成份指数×100%",
                "review_note": "",
            },
            {
                "fund_code": "515190.SH",
                "name": "中银证券中证500ETF",
                "category": "宽基",
                "subcategory": "中证500",
                "fund_type": "股票型",
                "benchmark": "中证500指数收益率×100%",
                "review_note": "",
            },
        ]
    )
    sw_frame = pd.DataFrame(
        [
            {"index_code": "801230.SI", "industry_name": "综合", "level": "L1", "industry_code": "510000", "is_pub": "1", "parent_code": "0", "src": "SW2021"},
            {"index_code": "801231.SI", "industry_name": "综合Ⅱ", "level": "L2", "industry_code": "510100", "is_pub": "1", "parent_code": "510000", "src": "SW2021"},
            {"index_code": "801790.SI", "industry_name": "非银金融", "level": "L1", "industry_code": "490000", "is_pub": "1", "parent_code": "0", "src": "SW2021"},
            {"index_code": "801193.SI", "industry_name": "证券", "level": "L2", "industry_code": "490100", "is_pub": "1", "parent_code": "490000", "src": "SW2021"},
        ]
    )

    refined, summary = refine_category_map(category_map, sw_frame)

    assert summary["refined_rows"] == 1
    assert refined.loc[refined["fund_code"].eq("510140.SH"), "category"].item() == "宽基"
    assert refined.loc[refined["fund_code"].eq("510050.SH"), "category"].item() == "宽基"
    assert refined.loc[refined["fund_code"].eq("510050.SH"), "subcategory"].item() == "上证50"
    assert refined.loc[refined["fund_code"].eq("515190.SH"), "category"].item() == "宽基"
    assert refined.loc[refined["fund_code"].eq("515190.SH"), "subcategory"].item() == "中证500"
    assert refined.loc[refined["fund_code"].eq("512000.SH"), "category"].item() == "非银金融"
    assert refined.loc[refined["fund_code"].eq("512000.SH"), "subcategory"].item() == "证券"
