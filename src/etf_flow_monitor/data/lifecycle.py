"""ETF lifecycle event helpers for share-jump review."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from etf_flow_monitor.data.schemas import normalize_etf_basic_frame, normalize_etf_share_frame
from etf_flow_monitor.utils.calendar import TradingCalendar
from etf_flow_monitor.utils.io import (
    clean_excel_text,
    format_tushare_date,
    parse_excel_friendly_date,
    parse_excel_friendly_date_series,
)


LIFECYCLE_EVENT_COLUMNS = [
    "fund_code",
    "name",
    "event_date",
    "announcement_date",
    "event_type",
    "event_keyword",
    "title",
    "source_url",
    "source_file",
    "event_date_source",
    "review_status",
    "review_note",
]

SHARE_JUMP_AUDIT_COLUMNS = [
    "fund_code",
    "name",
    "trade_date",
    "prev_trade_date",
    "prev_shares",
    "shares",
    "share_change",
    "share_change_pct",
    "abs_share_change",
    "match_status",
    "matched_event_type",
    "matched_event_date",
    "matched_announcement_date",
    "matched_event_keyword",
    "matched_event_title",
    "matched_source_url",
]

ANNOUNCEMENT_REQUEST_COLUMNS = [
    "fund_code",
    "name",
    "trade_date",
    "prev_trade_date",
    "request_start_date",
    "request_end_date",
    "share_change",
    "share_change_pct",
]

LIFECYCLE_REVIEW_COLUMNS = [
    *ANNOUNCEMENT_REQUEST_COLUMNS,
    "review_layer",
    "review_reason",
    "category",
    "subcategory",
    "fund_type",
    "list_date",
    "days_since_list",
    "share_ratio",
    "nearest_integer_ratio",
    "integer_ratio_error",
]

ANNOUNCEMENT_TEMPLATE_COLUMNS = [
    "fund_code",
    "announcement_date",
    "event_date",
    "title",
    "content",
    "source_url",
]

MANUAL_CONFIRMATION_COLUMNS = [
    "fund_code",
    "name",
    "trade_date",
    "prev_trade_date",
    "share_change",
    "share_change_pct",
    "confirmed_at",
    "confirm_note",
]

PENDING_CONFIRMATION_COLUMNS = [
    "fund_code",
    "name",
    "trade_date",
    "prev_trade_date",
    "request_start_date",
    "request_end_date",
    "share_change",
    "share_change_pct",
    "checked_start_date",
    "checked_end_date",
    "announcement_rows",
    "last_checked_at",
    "status",
]

FLOW_ADJUSTMENT_COLUMNS = [
    "fund_code",
    "name",
    "trade_date",
    "prev_trade_date",
    "event_type",
    "event_date",
    "announcement_date",
    "event_keyword",
    "event_title",
    "source_url",
    "share_change",
    "share_change_pct",
    "adjustment_action",
    "review_status",
    "review_note",
]

MATCH_STATUS_MATCHED = "matched_lifecycle_event"
MATCH_STATUS_MANUAL_CONFIRMED = "manual_confirmed_no_lifecycle_event"
REVIEW_LAYER_HIGH = "high_suspicion"
REVIEW_LAYER_LOW = "low_suspicion_observation"
FLOW_ADJUSTMENT_ACTION_ZERO = "zero_estimated_net_flow"
FLOW_ADJUSTMENT_EVENT_TYPES = frozenset({"share_split", "share_conversion", "liquidation", "transformation", "merger"})

EVENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("liquidation", ("终止上市", "进入清算", "清算报告", "基金合同终止并清算")),
    ("share_split", ("基金份额拆分", "份额拆分", "拆分比例")),
    ("share_conversion", ("基金份额折算", "份额折算", "折算结果", "折算基准日", "净值归一")),
    ("transformation", ("基金转型", "转型为", "变更注册", "基金合同变更生效", "变更基金合同生效")),
    ("merger", ("基金合并", "吸收合并")),
)


def empty_lifecycle_events() -> pd.DataFrame:
    return pd.DataFrame(columns=LIFECYCLE_EVENT_COLUMNS)


def empty_share_jump_audit() -> pd.DataFrame:
    return pd.DataFrame(columns=SHARE_JUMP_AUDIT_COLUMNS)


def empty_announcement_request_plan() -> pd.DataFrame:
    return pd.DataFrame(columns=LIFECYCLE_REVIEW_COLUMNS)


def empty_lifecycle_observation_plan() -> pd.DataFrame:
    return pd.DataFrame(columns=LIFECYCLE_REVIEW_COLUMNS)


def empty_announcement_template() -> pd.DataFrame:
    return pd.DataFrame(columns=ANNOUNCEMENT_TEMPLATE_COLUMNS)


def empty_manual_confirmations() -> pd.DataFrame:
    return pd.DataFrame(columns=MANUAL_CONFIRMATION_COLUMNS)


def empty_pending_confirmations() -> pd.DataFrame:
    return pd.DataFrame(columns=PENDING_CONFIRMATION_COLUMNS)


def empty_flow_adjustments() -> pd.DataFrame:
    return pd.DataFrame(columns=FLOW_ADJUSTMENT_COLUMNS)


def classify_lifecycle_announcement(text: object) -> tuple[str, str]:
    normalized = _clean_text(text)
    if not normalized:
        return "", ""
    for event_type, keywords in EVENT_KEYWORDS:
        for keyword in keywords:
            if keyword in normalized:
                return event_type, keyword
    return "", ""


def extract_lifecycle_events_from_announcements(
    announcements: pd.DataFrame | None,
    *,
    basic: pd.DataFrame | None = None,
    source_file: str | Path = "",
) -> pd.DataFrame:
    if announcements is None or announcements.empty:
        return empty_lifecycle_events()

    working = announcements.copy()
    basic_map = _basic_name_map(basic)
    source_file_text = str(source_file or "")
    rows: list[dict[str, object]] = []
    for _, row in working.iterrows():
        fund_code = _first_text(row, ("fund_code", "ts_code", "code", "证券代码", "基金代码")).upper()
        if not fund_code:
            continue
        title = _first_text(row, ("title", "ann_title", "公告标题", "公告名称", "name"))
        content = _first_text(row, ("content", "text", "summary", "公告内容", "正文"))
        event_type, keyword = classify_lifecycle_announcement(f"{title} {content}")
        if not event_type:
            continue
        announcement_date = _first_date(row, ("announcement_date", "ann_date", "pub_date", "publish_date", "公告日期", "披露日期", "date"))
        explicit_event_date = _first_date(row, ("event_date", "effective_date", "生效日期", "除权日", "折算日", "终止上市日"))
        event_date = explicit_event_date or announcement_date
        if event_date is None:
            continue
        rows.append(
            {
                "fund_code": fund_code,
                "name": _first_text(row, ("name", "fund_name", "基金简称", "证券简称")) or basic_map.get(fund_code, ""),
                "event_date": event_date,
                "announcement_date": announcement_date,
                "event_type": event_type,
                "event_keyword": keyword,
                "title": title,
                "source_url": _first_text(row, ("source_url", "url", "link", "公告链接")),
                "source_file": source_file_text,
                "event_date_source": "explicit" if explicit_event_date is not None else "announcement_date",
                "review_status": "auto_from_announcement",
                "review_note": "",
            }
        )
    return normalize_lifecycle_events(pd.DataFrame(rows), basic=basic)


def normalize_lifecycle_events(events: pd.DataFrame | None, *, basic: pd.DataFrame | None = None) -> pd.DataFrame:
    if events is None or events.empty:
        return empty_lifecycle_events()
    working = events.copy()
    for column in LIFECYCLE_EVENT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    result = working[LIFECYCLE_EVENT_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    result["name"] = result["name"].map(clean_excel_text)
    basic_map = _basic_name_map(basic)
    missing_name = result["name"].eq("")
    if basic_map and missing_name.any():
        result.loc[missing_name, "name"] = result.loc[missing_name, "fund_code"].map(basic_map).fillna("")
    for column in ("event_date", "announcement_date"):
        result[column] = parse_excel_friendly_date_series(result[column])
    for column in ("event_type", "event_keyword", "title", "source_url", "source_file", "event_date_source", "review_status", "review_note"):
        result[column] = result[column].map(clean_excel_text)
    result = result.loc[result["fund_code"].ne("") & result["event_date"].notna() & result["event_type"].ne("")].copy()
    return (
        result.drop_duplicates(subset=["fund_code", "event_date", "event_type", "title"], keep="last")
        .sort_values(["event_date", "fund_code", "event_type"], kind="stable")
        .reset_index(drop=True)
    )


def merge_lifecycle_events(existing: pd.DataFrame | None, extracted: pd.DataFrame | None, *, basic: pd.DataFrame | None = None) -> pd.DataFrame:
    left = normalize_lifecycle_events(existing, basic=basic)
    right = normalize_lifecycle_events(extracted, basic=basic)
    if left.empty:
        return right
    if right.empty:
        return left
    merged = pd.concat([left.assign(_source_order=0), right.assign(_source_order=1)], ignore_index=True)
    merged = merged.sort_values(["fund_code", "event_date", "event_type", "title", "_source_order"], kind="stable")
    merged = merged.drop_duplicates(subset=["fund_code", "event_date", "event_type", "title"], keep="first")
    return normalize_lifecycle_events(merged.drop(columns=["_source_order"]), basic=basic)


def detect_share_jumps(
    shares: pd.DataFrame | None,
    *,
    basic: pd.DataFrame | None = None,
    min_change_pct: float = 0.20,
) -> pd.DataFrame:
    share_frame = normalize_etf_share_frame(shares)
    if share_frame.empty:
        return empty_share_jump_audit()

    working = share_frame.sort_values(["fund_code", "trade_date"], kind="stable").copy()
    group = working.groupby("fund_code", sort=False)
    working["prev_trade_date"] = group["trade_date"].shift(1)
    working["prev_shares"] = group["shares"].shift(1)
    working["share_change"] = working["shares"] - working["prev_shares"]
    working["share_change_pct"] = working["share_change"] / working["prev_shares"].where(working["prev_shares"].ne(0))
    working["abs_share_change"] = working["share_change"].abs()
    mask = working["prev_shares"].notna() & working["share_change_pct"].abs().ge(float(min_change_pct))
    result = working.loc[mask].copy()
    if result.empty:
        return empty_share_jump_audit()
    basic_map = _basic_name_map(basic)
    result["name"] = result["fund_code"].map(basic_map).fillna("")
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    result["match_status"] = "unmatched"
    return result[SHARE_JUMP_AUDIT_COLUMNS].sort_values(["trade_date", "abs_share_change"], ascending=[True, False], kind="stable").reset_index(drop=True)


def match_share_jumps_to_events(
    jumps: pd.DataFrame | None,
    events: pd.DataFrame | None,
    *,
    match_window_days: int = 10,
) -> pd.DataFrame:
    if jumps is None or jumps.empty:
        return empty_share_jump_audit()
    audit = jumps.copy()
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in audit.columns:
            audit[column] = ""
    audit["trade_date"] = parse_excel_friendly_date_series(audit["trade_date"])
    audit["prev_trade_date"] = parse_excel_friendly_date_series(audit["prev_trade_date"])
    for column in ("matched_event_date", "matched_announcement_date"):
        audit[column] = audit[column].astype("object")
    lifecycle = normalize_lifecycle_events(events)
    if lifecycle.empty:
        audit["match_status"] = "unmatched"
        return audit[SHARE_JUMP_AUDIT_COLUMNS].reset_index(drop=True)

    event_groups = {fund_code: group.copy() for fund_code, group in lifecycle.groupby("fund_code", sort=False)}
    window = pd.Timedelta(days=max(int(match_window_days), 0))
    for idx, row in audit.iterrows():
        trade_date = row["trade_date"]
        fund_code = str(row["fund_code"]).upper()
        candidates = event_groups.get(fund_code)
        if candidates is None or pd.isna(trade_date):
            audit.loc[idx, "match_status"] = "unmatched"
            continue
        distance = (candidates["event_date"] - trade_date).abs()
        in_window = candidates.loc[distance.le(window)].copy()
        if in_window.empty:
            audit.loc[idx, "match_status"] = "unmatched"
            continue
        in_window["_distance_days"] = (in_window["event_date"] - trade_date).abs().dt.days
        matched = in_window.sort_values(["_distance_days", "event_date"], kind="stable").iloc[0]
        audit.loc[idx, "match_status"] = MATCH_STATUS_MATCHED
        audit.loc[idx, "matched_event_type"] = matched["event_type"]
        audit.loc[idx, "matched_event_date"] = matched["event_date"]
        audit.loc[idx, "matched_announcement_date"] = matched["announcement_date"]
        audit.loc[idx, "matched_event_keyword"] = matched["event_keyword"]
        audit.loc[idx, "matched_event_title"] = matched["title"]
        audit.loc[idx, "matched_source_url"] = matched["source_url"]
    return audit[SHARE_JUMP_AUDIT_COLUMNS].reset_index(drop=True)


def apply_manual_confirmations(audit: pd.DataFrame | None, confirmations: pd.DataFrame | None) -> pd.DataFrame:
    if audit is None or audit.empty:
        return empty_share_jump_audit()
    result = audit.copy()
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    confirmed = normalize_manual_confirmations(confirmations)
    if confirmed.empty:
        return result[SHARE_JUMP_AUDIT_COLUMNS].reset_index(drop=True)

    confirmed_keys = set(_jump_keys(confirmed))
    audit_keys = _jump_keys(result)
    matched = result["match_status"].fillna("").astype(str).eq(MATCH_STATUS_MATCHED)
    manual_mask = audit_keys.isin(confirmed_keys) & ~matched
    result.loc[manual_mask, "match_status"] = MATCH_STATUS_MANUAL_CONFIRMED
    result.loc[manual_mask, "matched_event_keyword"] = "manual_confirmed"
    result.loc[manual_mask, "matched_event_title"] = "人工确认：未找到需纳入生命周期表的公告"
    return result[SHARE_JUMP_AUDIT_COLUMNS].reset_index(drop=True)


def normalize_manual_confirmations(confirmations: pd.DataFrame | None) -> pd.DataFrame:
    if confirmations is None or confirmations.empty:
        return empty_manual_confirmations()
    working = confirmations.copy()
    for column in MANUAL_CONFIRMATION_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    result = working[MANUAL_CONFIRMATION_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    result["name"] = result["name"].map(clean_excel_text)
    for column in ("trade_date", "prev_trade_date"):
        result[column] = parse_excel_friendly_date_series(result[column])
    for column in ("share_change", "share_change_pct"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    for column in ("confirmed_at", "confirm_note"):
        result[column] = result[column].map(clean_excel_text)
    result = result.loc[result["fund_code"].ne("") & result["trade_date"].notna()].copy()
    return (
        result.drop_duplicates(subset=["fund_code", "trade_date", "prev_trade_date"], keep="last")
        .sort_values(["trade_date", "fund_code"], kind="stable")
        .reset_index(drop=True)
    )


def normalize_pending_confirmations(confirmations: pd.DataFrame | None) -> pd.DataFrame:
    if confirmations is None or confirmations.empty:
        return empty_pending_confirmations()
    working = confirmations.copy()
    for column in PENDING_CONFIRMATION_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    result = working[PENDING_CONFIRMATION_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    result["name"] = result["name"].map(clean_excel_text)
    for column in ("trade_date", "prev_trade_date", "request_start_date", "request_end_date", "checked_start_date", "checked_end_date"):
        result[column] = parse_excel_friendly_date_series(result[column])
    for column in ("share_change", "share_change_pct", "announcement_rows"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    for column in ("last_checked_at", "status"):
        result[column] = result[column].map(clean_excel_text)
    result = result.loc[result["fund_code"].ne("") & result["trade_date"].notna()].copy()
    return (
        result.drop_duplicates(subset=["fund_code", "trade_date", "prev_trade_date"], keep="last")
        .sort_values(["trade_date", "fund_code"], kind="stable")
        .reset_index(drop=True)
    )


def build_flow_adjustments_from_audit(audit: pd.DataFrame | None) -> pd.DataFrame:
    if audit is None or audit.empty:
        return empty_flow_adjustments()
    working = audit.copy()
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    working["fund_code"] = working["fund_code"].map(clean_excel_text).str.upper()
    working["name"] = working["name"].map(clean_excel_text)
    working["trade_date"] = parse_excel_friendly_date_series(working["trade_date"])
    working["prev_trade_date"] = parse_excel_friendly_date_series(working["prev_trade_date"])
    working["matched_event_date"] = parse_excel_friendly_date_series(working["matched_event_date"])
    working["matched_announcement_date"] = parse_excel_friendly_date_series(working["matched_announcement_date"])
    for column in ("share_change", "share_change_pct"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    matched = working["match_status"].fillna("").astype(str).eq(MATCH_STATUS_MATCHED)
    event_type = working["matched_event_type"].fillna("").astype(str)
    working = working.loc[matched & event_type.isin(FLOW_ADJUSTMENT_EVENT_TYPES)].copy()
    if working.empty:
        return empty_flow_adjustments()
    result = pd.DataFrame(
        {
            "fund_code": working["fund_code"],
            "name": working["name"],
            "trade_date": working["trade_date"],
            "prev_trade_date": working["prev_trade_date"],
            "event_type": working["matched_event_type"].map(clean_excel_text),
            "event_date": working["matched_event_date"],
            "announcement_date": working["matched_announcement_date"],
            "event_keyword": working["matched_event_keyword"].map(clean_excel_text),
            "event_title": working["matched_event_title"].map(clean_excel_text),
            "source_url": working["matched_source_url"].map(clean_excel_text),
            "share_change": working["share_change"],
            "share_change_pct": working["share_change_pct"],
            "adjustment_action": FLOW_ADJUSTMENT_ACTION_ZERO,
            "review_status": "auto_from_matched_lifecycle_event",
            "review_note": "non_flow_lifecycle_share_jump",
        }
    )
    return normalize_flow_adjustments(result)


def normalize_flow_adjustments(adjustments: pd.DataFrame | None) -> pd.DataFrame:
    if adjustments is None or adjustments.empty:
        return empty_flow_adjustments()
    working = adjustments.copy()
    for column in FLOW_ADJUSTMENT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    result = working[FLOW_ADJUSTMENT_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    result["name"] = result["name"].map(clean_excel_text)
    for column in ("trade_date", "prev_trade_date", "event_date", "announcement_date"):
        result[column] = parse_excel_friendly_date_series(result[column])
    for column in ("share_change", "share_change_pct"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    for column in ("event_type", "event_keyword", "event_title", "source_url", "adjustment_action", "review_status", "review_note"):
        result[column] = result[column].map(clean_excel_text)
    result = result.loc[
        result["fund_code"].ne("")
        & result["trade_date"].notna()
        & result["adjustment_action"].eq(FLOW_ADJUSTMENT_ACTION_ZERO)
    ].copy()
    return (
        result.drop_duplicates(subset=["fund_code", "trade_date", "event_type", "event_title"], keep="last")
        .sort_values(["trade_date", "fund_code", "event_type"], kind="stable")
        .reset_index(drop=True)
    )


def apply_flow_adjustments_to_snapshot(flow: pd.DataFrame | None, adjustments: pd.DataFrame | None) -> pd.DataFrame:
    result = pd.DataFrame() if flow is None else flow.copy()
    if result.empty:
        for column in (
            "estimated_net_flow_raw",
            "estimated_net_flow_adjustment",
            "lifecycle_adjustment_action",
            "lifecycle_event_type",
            "lifecycle_event_date",
            "lifecycle_event_title",
        ):
            if column not in result.columns:
                result[column] = pd.Series(dtype="string")
        return result

    if "estimated_net_flow_raw" not in result.columns:
        result["estimated_net_flow_raw"] = pd.to_numeric(result.get("estimated_net_flow"), errors="coerce").fillna(0.0)
    result["estimated_net_flow_adjustment"] = 0.0
    result["lifecycle_adjustment_action"] = ""
    result["lifecycle_event_type"] = ""
    result["lifecycle_event_date"] = ""
    result["lifecycle_event_title"] = ""

    normalized = normalize_flow_adjustments(adjustments)
    if normalized.empty:
        return result

    flow_keys = _flow_adjustment_keys(result)
    adjustment_map = normalized.drop_duplicates(subset=["fund_code", "trade_date"], keep="last").copy()
    adjustment_map["_key"] = _flow_adjustment_keys(adjustment_map)
    by_key = adjustment_map.set_index("_key")
    matched = flow_keys.isin(set(by_key.index))
    if not matched.any():
        return result

    matched_keys = flow_keys.loc[matched]
    matched_adjustments = by_key.loc[matched_keys].reset_index(drop=True)
    raw = pd.to_numeric(result.loc[matched, "estimated_net_flow"], errors="coerce").fillna(0.0)
    result.loc[matched, "estimated_net_flow_adjustment"] = -raw.to_numpy()
    result.loc[matched, "estimated_net_flow"] = 0.0
    result.loc[matched, "flow_direction"] = "flat"
    result.loc[matched, "lifecycle_adjustment_action"] = matched_adjustments["adjustment_action"].to_numpy()
    result.loc[matched, "lifecycle_event_type"] = matched_adjustments["event_type"].to_numpy()
    result.loc[matched, "lifecycle_event_date"] = matched_adjustments["event_date"].map(_format_date_for_csv).to_numpy()
    result.loc[matched, "lifecycle_event_title"] = matched_adjustments["event_title"].to_numpy()
    return result


def build_announcement_request_plan(
    audit: pd.DataFrame | None,
    *,
    window_days: int = 5,
    calendar: TradingCalendar | None = None,
) -> pd.DataFrame:
    if audit is None or audit.empty:
        return empty_announcement_request_plan()
    working = audit.copy()
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    resolved_statuses = {MATCH_STATUS_MATCHED, MATCH_STATUS_MANUAL_CONFIRMED}
    unmatched = working.loc[~working["match_status"].fillna("").astype(str).isin(resolved_statuses)].copy()
    if unmatched.empty:
        return empty_announcement_request_plan()

    unmatched["trade_date"] = parse_excel_friendly_date_series(unmatched["trade_date"])
    unmatched["prev_trade_date"] = parse_excel_friendly_date_series(unmatched["prev_trade_date"])
    unmatched["fund_code"] = unmatched["fund_code"].map(clean_excel_text).str.upper()
    unmatched = unmatched.loc[unmatched["fund_code"].ne("") & unmatched["trade_date"].notna()].copy()
    if unmatched.empty:
        return empty_announcement_request_plan()

    windows = unmatched["trade_date"].map(lambda value: _announcement_request_window(value, window_days=window_days, calendar=calendar))
    unmatched["request_start_date"] = windows.map(lambda item: item[0])
    unmatched["request_end_date"] = windows.map(lambda item: item[1])
    result = unmatched[ANNOUNCEMENT_REQUEST_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    result["name"] = result["name"].map(clean_excel_text)
    return (
        result.drop_duplicates(subset=["fund_code", "trade_date", "request_start_date", "request_end_date"], keep="first")
        .sort_values(["request_start_date", "fund_code", "trade_date"], kind="stable")
        .reset_index(drop=True)
    )


def build_lifecycle_review_plans(
    audit: pd.DataFrame | None,
    *,
    category_map: pd.DataFrame | None = None,
    window_days: int = 5,
    calendar: TradingCalendar | None = None,
    min_listing_days: int = 60,
    early_listing_abs_min_pct: float = 0.70,
    integer_ratio_tolerance: float = 0.20,
    positive_min_pct: float = 1.20,
    negative_max_pct: float = -0.50,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = _build_lifecycle_review_base(
        audit,
        category_map=category_map,
        window_days=window_days,
        calendar=calendar,
        min_listing_days=min_listing_days,
        early_listing_abs_min_pct=early_listing_abs_min_pct,
        integer_ratio_tolerance=integer_ratio_tolerance,
        positive_min_pct=positive_min_pct,
        negative_max_pct=negative_max_pct,
    )
    if base.empty:
        return empty_announcement_request_plan(), empty_lifecycle_observation_plan()
    high = base.loc[base["review_layer"].eq(REVIEW_LAYER_HIGH)].copy()
    low = base.loc[base["review_layer"].eq(REVIEW_LAYER_LOW)].copy()
    return _sort_review_plan(high), _sort_review_plan(low)


def lifecycle_summary(events: pd.DataFrame | None, audit: pd.DataFrame | None) -> dict[str, object]:
    normalized_events = normalize_lifecycle_events(events)
    normalized_audit = empty_share_jump_audit() if audit is None else audit.copy()
    matched = (
        normalized_audit["match_status"].fillna("").astype(str).eq(MATCH_STATUS_MATCHED)
        if "match_status" in normalized_audit.columns
        else pd.Series(dtype=bool)
    )
    manual_confirmed = (
        normalized_audit["match_status"].fillna("").astype(str).eq(MATCH_STATUS_MANUAL_CONFIRMED)
        if "match_status" in normalized_audit.columns
        else pd.Series(dtype=bool)
    )
    resolved = matched | manual_confirmed if len(matched) else matched
    return {
        "event_rows": int(len(normalized_events)),
        "event_funds": _nunique(normalized_events, "fund_code"),
        "event_type_counts": _value_counts(normalized_events, "event_type"),
        "share_jump_rows": int(len(normalized_audit)),
        "matched_share_jump_rows": int(matched.sum()) if len(matched) else 0,
        "manual_confirmed_share_jump_rows": int(manual_confirmed.sum()) if len(manual_confirmed) else 0,
        "unmatched_share_jump_rows": int((~resolved).sum()) if len(resolved) else int(len(normalized_audit)),
    }


def prepare_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    date_columns = {
        "event_date",
        "announcement_date",
        "trade_date",
        "prev_trade_date",
        "matched_event_date",
        "matched_announcement_date",
        "request_start_date",
        "request_end_date",
        "checked_start_date",
        "checked_end_date",
        "list_date",
    }
    for column in result.columns:
        if column in date_columns or pd.api.types.is_datetime64_any_dtype(result[column]):
            result[column] = result[column].map(_format_date_for_csv)
    return result


def _basic_name_map(basic: pd.DataFrame | None) -> dict[str, str]:
    normalized = normalize_etf_basic_frame(basic)
    if normalized.empty or "name" not in normalized.columns:
        return {}
    return dict(zip(normalized["fund_code"].astype(str).str.upper(), normalized["name"].fillna("").astype(str)))


def _format_date_for_csv(value: object) -> str:
    parsed = parse_excel_friendly_date(value)
    if pd.isna(parsed):
        return ""
    return format_tushare_date(pd.Timestamp(parsed))


def _clean_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _first_text(row: pd.Series, candidates: Iterable[str]) -> str:
    for column in candidates:
        if column in row.index:
            value = _clean_text(row.get(column))
            if value:
                return value
    return ""


def _first_date(row: pd.Series, candidates: Iterable[str]) -> pd.Timestamp | None:
    for column in candidates:
        if column not in row.index:
            continue
        value = row.get(column)
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            continue
        parsed = parse_excel_friendly_date(value)
        if pd.notna(parsed):
            return pd.Timestamp(parsed).normalize()
    return None


def _nunique(frame: pd.DataFrame, column: str) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    return int(frame[column].dropna().astype(str).str.upper().nunique())


def _value_counts(frame: pd.DataFrame, column: str) -> dict[str, int]:
    if frame.empty or column not in frame.columns:
        return {}
    return {str(key): int(value) for key, value in frame[column].fillna("").astype(str).value_counts().sort_index().items()}


def _announcement_request_window(
    trade_date: object,
    *,
    window_days: int,
    calendar: TradingCalendar | None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    parsed = parse_excel_friendly_date(trade_date)
    if pd.isna(parsed):
        return pd.NaT, pd.NaT
    current = pd.Timestamp(parsed).normalize()
    days = max(int(window_days), 0)
    if calendar is None:
        window = pd.Timedelta(days=days)
        return current - window, current + window
    start = calendar.shift_trade_date(current.date(), -days)
    end = calendar.shift_trade_date(current.date(), days)
    return pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()


def _build_lifecycle_review_base(
    audit: pd.DataFrame | None,
    *,
    category_map: pd.DataFrame | None,
    window_days: int,
    calendar: TradingCalendar | None,
    min_listing_days: int,
    early_listing_abs_min_pct: float,
    integer_ratio_tolerance: float,
    positive_min_pct: float,
    negative_max_pct: float,
) -> pd.DataFrame:
    if audit is None or audit.empty:
        return empty_lifecycle_observation_plan()
    working = audit.copy()
    for column in SHARE_JUMP_AUDIT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    resolved_statuses = {MATCH_STATUS_MATCHED, MATCH_STATUS_MANUAL_CONFIRMED}
    working = working.loc[~working["match_status"].fillna("").astype(str).isin(resolved_statuses)].copy()
    if working.empty:
        return empty_lifecycle_observation_plan()

    working["fund_code"] = working["fund_code"].map(clean_excel_text).str.upper()
    working["name"] = working["name"].map(clean_excel_text)
    working["trade_date"] = parse_excel_friendly_date_series(working["trade_date"])
    working["prev_trade_date"] = parse_excel_friendly_date_series(working["prev_trade_date"])
    for column in ("prev_shares", "shares", "share_change", "share_change_pct", "abs_share_change"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    working = working.loc[working["fund_code"].ne("") & working["trade_date"].notna()].copy()
    if working.empty:
        return empty_lifecycle_observation_plan()

    metadata = _lifecycle_category_metadata(category_map)
    if not metadata.empty:
        working = working.merge(metadata, on="fund_code", how="left")
    for column in ("category", "subcategory", "fund_type"):
        if column not in working.columns:
            working[column] = ""
        working[column] = working[column].map(clean_excel_text)
    if "list_date" not in working.columns:
        working["list_date"] = pd.NaT
    working["list_date"] = parse_excel_friendly_date_series(working["list_date"])
    working["days_since_list"] = (working["trade_date"] - working["list_date"]).dt.days
    working["share_ratio"] = working["shares"] / working["prev_shares"].where(working["prev_shares"].ne(0))
    working["nearest_integer_ratio"] = working["share_ratio"].round()
    working["integer_ratio_error"] = (working["share_ratio"] - working["nearest_integer_ratio"]).abs()

    money_like = working["category"].eq("货币") | working["fund_type"].str.contains("货币", na=False)
    early_listing = working["days_since_list"].notna() & working["days_since_list"].lt(max(int(min_listing_days), 0))
    integer_like = (
        working["share_change_pct"].gt(0)
        & working["nearest_integer_ratio"].between(2, 20)
        & working["integer_ratio_error"].le(max(float(integer_ratio_tolerance), 0))
    )
    positive_large = working["share_change_pct"].ge(float(positive_min_pct))
    negative_large = working["share_change_pct"].le(float(negative_max_pct))
    early_listing_large = early_listing & working["share_change_pct"].abs().ge(
        max(float(early_listing_abs_min_pct), 0)
    )
    established_listing_signal = ~early_listing & (integer_like | positive_large | negative_large)
    high_mask = ~money_like & (established_listing_signal | early_listing_large)

    windows = working["trade_date"].map(lambda value: _announcement_request_window(value, window_days=window_days, calendar=calendar))
    working["request_start_date"] = windows.map(lambda item: item[0])
    working["request_end_date"] = windows.map(lambda item: item[1])
    working["review_layer"] = REVIEW_LAYER_LOW
    working.loc[high_mask, "review_layer"] = REVIEW_LAYER_HIGH
    working["review_reason"] = [
        _review_reason(
            is_high=bool(is_high),
            is_money=bool(is_money),
            is_early=bool(is_early),
            early_listing_large=bool(is_early_large),
            integer_like=bool(is_integer),
            positive_large=bool(is_positive_large),
            negative_large=bool(is_negative_large),
            min_listing_days=int(min_listing_days),
            early_listing_abs_min_pct=float(early_listing_abs_min_pct),
            positive_min_pct=float(positive_min_pct),
            negative_max_pct=float(negative_max_pct),
        )
        for is_high, is_money, is_early, is_early_large, is_integer, is_positive_large, is_negative_large in zip(
            high_mask,
            money_like,
            early_listing,
            early_listing_large,
            integer_like,
            positive_large,
            negative_large,
        )
    ]
    result = working[LIFECYCLE_REVIEW_COLUMNS].copy()
    return _sort_review_plan(result)


def _lifecycle_category_metadata(category_map: pd.DataFrame | None) -> pd.DataFrame:
    if category_map is None or category_map.empty:
        return pd.DataFrame(columns=["fund_code", "category", "subcategory", "fund_type", "list_date"])
    working = category_map.copy()
    for column in ("fund_code", "category", "subcategory", "fund_type", "list_date"):
        if column not in working.columns:
            working[column] = ""
    result = working[["fund_code", "category", "subcategory", "fund_type", "list_date"]].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    for column in ("category", "subcategory", "fund_type"):
        result[column] = result[column].map(clean_excel_text)
    result["list_date"] = parse_excel_friendly_date_series(result["list_date"])
    result = result.loc[result["fund_code"].ne("")].drop_duplicates("fund_code", keep="last")
    return result.reset_index(drop=True)


def _review_reason(
    *,
    is_high: bool,
    is_money: bool,
    is_early: bool,
    early_listing_large: bool,
    integer_like: bool,
    positive_large: bool,
    negative_large: bool,
    min_listing_days: int,
    early_listing_abs_min_pct: float,
    positive_min_pct: float,
    negative_max_pct: float,
) -> str:
    reasons: list[str] = []
    if is_money:
        reasons.append("low:money_etf_noise")
    if is_early and early_listing_large:
        reasons.append(f"high:early_listing_abs_change_ge_{early_listing_abs_min_pct:.2f}")
    elif is_early:
        reasons.append(f"low:listed_less_than_{min_listing_days}d")
    if integer_like:
        reasons.append("high:positive_integer_ratio")
    if positive_large:
        reasons.append(f"high:positive_change_ge_{positive_min_pct:.2f}")
    if negative_large:
        reasons.append(f"high:negative_change_le_{negative_max_pct:.2f}")
    if not reasons:
        reasons.append("low:ordinary_large_subscription_redemption")
    if not is_high and not any(reason.startswith("low:") for reason in reasons):
        reasons.append("low:filtered_by_scope")
    return ";".join(reasons)


def _sort_review_plan(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=LIFECYCLE_REVIEW_COLUMNS)
    result = frame.copy()
    for column in LIFECYCLE_REVIEW_COLUMNS:
        if column not in result.columns:
            result[column] = pd.NA
    return (
        result[LIFECYCLE_REVIEW_COLUMNS]
        .drop_duplicates(subset=["fund_code", "trade_date", "request_start_date", "request_end_date"], keep="first")
        .sort_values(["request_start_date", "fund_code", "trade_date"], kind="stable")
        .reset_index(drop=True)
    )


def _jump_keys(frame: pd.DataFrame) -> pd.Series:
    working = frame.copy()
    fund_code = working.get("fund_code", pd.Series(index=working.index, dtype=object)).map(clean_excel_text).str.upper()
    trade_date = parse_excel_friendly_date_series(working.get("trade_date", pd.Series(index=working.index, dtype=object))).map(_format_date_for_csv)
    prev_trade_date = parse_excel_friendly_date_series(working.get("prev_trade_date", pd.Series(index=working.index, dtype=object))).map(_format_date_for_csv)
    return fund_code + "|" + trade_date.fillna("") + "|" + prev_trade_date.fillna("")


def _flow_adjustment_keys(frame: pd.DataFrame) -> pd.Series:
    working = frame.copy()
    fund_code = working.get("fund_code", pd.Series(index=working.index, dtype=object)).map(clean_excel_text).str.upper()
    trade_date = parse_excel_friendly_date_series(working.get("trade_date", pd.Series(index=working.index, dtype=object))).map(_format_date_for_csv)
    return fund_code + "|" + trade_date.fillna("")
