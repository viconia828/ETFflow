"""Build a local ETF lifecycle event table and share-jump audit report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from datetime import datetime

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import category_map_codes, load_category_map  # noqa: E402
from etf_flow_monitor.data.cache_store import CacheStore  # noqa: E402
from etf_flow_monitor.data.lifecycle import (  # noqa: E402
    MATCH_STATUS_MANUAL_CONFIRMED,
    MATCH_STATUS_MATCHED,
    apply_manual_confirmations,
    build_flow_adjustments_from_audit,
    build_lifecycle_review_plans,
    classify_lifecycle_announcement,
    detect_share_jumps,
    empty_announcement_request_plan,
    empty_announcement_template,
    empty_flow_adjustments,
    empty_lifecycle_observation_plan,
    empty_lifecycle_events,
    empty_manual_confirmations,
    empty_pending_confirmations,
    extract_lifecycle_events_from_announcements,
    lifecycle_summary,
    match_share_jumps_to_events,
    merge_lifecycle_events,
    normalize_lifecycle_events,
    normalize_manual_confirmations,
    normalize_pending_confirmations,
    prepare_for_csv,
)
from etf_flow_monitor.data.schemas import normalize_etf_basic_frame, normalize_etf_share_frame  # noqa: E402
from etf_flow_monitor.utils.calendar import trading_calendar_from_frame  # noqa: E402
from etf_flow_monitor.utils.io import (
    format_tushare_date,
    parse_excel_friendly_date,
    parse_excel_friendly_date_series,
    read_user_csv,
    write_json,
    write_user_csv,
)  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build local ETF lifecycle table and audit share jumps.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--announcement-file", default="", help="CSV exported from ETF announcements. Blank uses config.")
    parser.add_argument("--events-output", default="", help="Lifecycle event CSV output. Blank uses config.")
    parser.add_argument("--manual-confirmations", default="", help="Manual confirmation CSV. Blank uses config.")
    parser.add_argument("--request-plan-output", default="", help="Announcement request plan CSV. Blank uses config.")
    parser.add_argument("--observation-output", default="", help="Low-suspicion observation CSV. Blank uses config.")
    parser.add_argument("--flow-adjustments-output", default="", help="Flow adjustment CSV. Blank uses config.")
    parser.add_argument("--status-output", default="", help="Lifecycle status JSON. Blank uses config.")
    parser.add_argument("--start-date", default="", help="Audit start date YYYYMMDD. Blank uses all cached shares.")
    parser.add_argument("--end-date", default="", help="Audit end date YYYYMMDD. Blank uses all cached shares.")
    parser.add_argument("--min-share-change-pct", type=float, default=None, help="Flag share jumps at or above this absolute pct. Blank uses config.")
    parser.add_argument("--match-window-days", type=int, default=10)
    parser.add_argument("--announcement-window-days", type=int, default=None, help="Fetch announcements +/- this many days around an unmatched jump.")
    parser.add_argument("--no-announcement-retry-window-days", type=int, default=None, help="Retry no-announcement jumps with +/- this many trading days.")
    parser.add_argument("--skip-if-current", action="store_true", help="Skip audit when lifecycle status already covers cached share data.")
    parser.add_argument("--force", action="store_true", help="Ignore lifecycle status and rebuild audit/request plan.")
    parser.add_argument("--verbose", action="store_true", help="Print full JSON summary to console.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    cache = CacheStore(config.cache_dir)
    basic = normalize_etf_basic_frame(cache.load_static_frame(config.source_name, "etf_basic", config.etf_market))
    category_map = load_category_map(config.category_map_path)
    category_map_full = _read_csv_if_exists(config.category_map_path, category_map)
    basic_codes = set(basic["fund_code"].dropna().astype(str).str.upper()) if "fund_code" in basic.columns else set()
    configured_codes = set(category_map_codes(category_map))
    universe_codes = configured_codes & basic_codes if configured_codes and basic_codes else configured_codes or basic_codes

    announcement_path = Path(args.announcement_file or config.announcement_file_path)
    events_path = Path(args.events_output or config.lifecycle_events_path)
    manual_confirmations_path = Path(args.manual_confirmations or config.lifecycle_manual_confirmations_path)
    pending_confirmations_path = config.lifecycle_pending_confirmations_path
    request_plan_path = Path(args.request_plan_output or config.lifecycle_request_plan_path)
    observation_plan_path = Path(args.observation_output or config.lifecycle_observation_plan_path)
    flow_adjustments_path = Path(args.flow_adjustments_output or config.lifecycle_flow_adjustments_path)
    status_path = Path(args.status_output or config.lifecycle_status_path)
    min_share_change_pct = (
        float(args.min_share_change_pct)
        if args.min_share_change_pct is not None
        else float(config.lifecycle_min_share_change_pct)
    )
    announcement_window_days = (
        int(args.announcement_window_days)
        if args.announcement_window_days is not None
        else int(config.lifecycle_announcement_window_days)
    )
    no_announcement_retry_window_days = (
        int(args.no_announcement_retry_window_days)
        if args.no_announcement_retry_window_days is not None
        else int(config.lifecycle_no_announcement_retry_window_days)
    )
    start_date = _parse_optional_date(args.start_date)
    end_date = _parse_optional_date(args.end_date)
    data_earliest_date, data_latest_date = cached_share_date_bounds(
        config.cache_dir,
        config.source_name,
        start_date=start_date,
        end_date=end_date,
    )
    status_updates_enabled = start_date is None

    if (
        status_updates_enabled
        and args.skip_if_current
        and not args.force
        and data_latest_date is not None
        and _status_is_current(status_path, data_earliest_date=data_earliest_date, data_latest_date=data_latest_date)
    ):
        empty_plan = empty_announcement_request_plan()
        _write_csv(request_plan_path, prepare_for_csv(empty_plan))
        _write_csv(observation_plan_path, prepare_for_csv(empty_lifecycle_observation_plan()))
        existing_flow_adjustments = _read_csv_if_exists(flow_adjustments_path, empty_flow_adjustments())
        status = _read_status(status_path)
        summary = {
            "schema_version": "etf_lifecycle_audit_v1",
            "skipped": True,
            "skip_reason": "lifecycle_status_current",
            "data_earliest_date": format_tushare_date(data_earliest_date) if data_earliest_date is not None else "",
            "data_latest_date": format_tushare_date(data_latest_date),
            "verified_from": str(status.get("verified_from") or status.get("data_earliest_date") or ""),
            "verified_through": str(status.get("verified_through") or ""),
            "request_plan_output": str(request_plan_path),
            "request_plan_rows": 0,
            "observation_plan_output": str(observation_plan_path),
            "observation_plan_rows": 0,
            "flow_adjustments_output": str(flow_adjustments_path),
            "flow_adjustment_rows": int(len(existing_flow_adjustments)),
            "status_output": str(status_path),
        }
        _print_lifecycle_summary(summary, verbose=args.verbose)
        return 0

    _ensure_announcement_template(announcement_path)
    existing_events = _manual_lifecycle_events(_read_csv_if_exists(events_path, empty_lifecycle_events()))
    announcements = _read_csv_if_exists(announcement_path, pd.DataFrame())
    manual_confirmations = _read_csv_if_exists(manual_confirmations_path, empty_manual_confirmations())
    pending_confirmations = _read_csv_if_exists(pending_confirmations_path, empty_pending_confirmations())
    pending_confirmations = _drop_pending_confirmations(pending_confirmations, manual_confirmations)
    _write_csv(pending_confirmations_path, prepare_for_csv(pending_confirmations))
    calendar_frame = cache.load_calendar(config.source_name, config.calendar_exchange)
    if calendar_frame is None or calendar_frame.empty:
        raise RuntimeError(f"official trading calendar cache missing: source={config.source_name} exchange={config.calendar_exchange}")
    calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
    extracted_events = extract_lifecycle_events_from_announcements(announcements, basic=basic, source_file=announcement_path)
    events = merge_lifecycle_events(existing_events, extracted_events, basic=basic)
    _write_csv(events_path, prepare_for_csv(events))

    shares = load_cached_share_cross_sections(
        config.cache_dir,
        config.source_name,
        start_date=start_date,
        end_date=end_date,
        codes=universe_codes,
    )
    jumps = detect_share_jumps(shares, basic=basic, min_change_pct=min_share_change_pct)
    audit = match_share_jumps_to_events(jumps, events, match_window_days=args.match_window_days)
    audit = apply_manual_confirmations(audit, manual_confirmations)
    request_plan, observation_plan = build_lifecycle_review_plans(
        audit,
        category_map=category_map_full,
        window_days=announcement_window_days,
        calendar=calendar,
        min_listing_days=config.lifecycle_high_suspicion_min_listing_days,
        integer_ratio_tolerance=config.lifecycle_integer_ratio_tolerance,
        positive_min_pct=config.lifecycle_high_suspicion_positive_min_pct,
        negative_max_pct=config.lifecycle_high_suspicion_negative_max_pct,
    )
    request_plan = _expand_no_announcement_retry_windows(
        request_plan,
        pending_confirmations,
        window_days=no_announcement_retry_window_days,
        calendar=calendar,
    )
    auto_confirmations = _auto_confirm_non_lifecycle_announcement_windows(request_plan, announcements)
    if not auto_confirmations.empty:
        manual_confirmations = _append_manual_confirmations(manual_confirmations_path, manual_confirmations, auto_confirmations)
        pending_confirmations = _drop_pending_confirmations(pending_confirmations, auto_confirmations)
        _write_csv(pending_confirmations_path, prepare_for_csv(pending_confirmations))
        audit = apply_manual_confirmations(audit, manual_confirmations)
        request_plan, observation_plan = build_lifecycle_review_plans(
            audit,
            category_map=category_map_full,
            window_days=announcement_window_days,
            calendar=calendar,
            min_listing_days=config.lifecycle_high_suspicion_min_listing_days,
            integer_ratio_tolerance=config.lifecycle_integer_ratio_tolerance,
            positive_min_pct=config.lifecycle_high_suspicion_positive_min_pct,
            negative_max_pct=config.lifecycle_high_suspicion_negative_max_pct,
        )
        request_plan = _expand_no_announcement_retry_windows(
            request_plan,
            pending_confirmations,
            window_days=no_announcement_retry_window_days,
            calendar=calendar,
        )
    flow_adjustments = build_flow_adjustments_from_audit(audit)
    _write_csv(request_plan_path, prepare_for_csv(request_plan))
    _write_csv(observation_plan_path, prepare_for_csv(observation_plan))
    _write_csv(flow_adjustments_path, prepare_for_csv(flow_adjustments))
    summary = lifecycle_summary(events, audit)
    lifecycle_current = request_plan.empty
    if status_updates_enabled:
        verified_through = _update_lifecycle_status(
            status_path,
            data_earliest_date=data_earliest_date,
            data_latest_date=data_latest_date,
            local_cache_start_date=pd.Timestamp(config.local_cache_start_date).normalize(),
            lifecycle_current=lifecycle_current,
            request_plan_rows=len(request_plan),
        )
    else:
        verified_through = str(_read_status(status_path).get("verified_through") or "")
    summary.update(
        {
            "schema_version": "etf_lifecycle_audit_v1",
            "announcement_file": str(announcement_path),
            "events_output": str(events_path),
            "manual_confirmations": str(manual_confirmations_path),
            "manual_confirmation_rows": int(len(manual_confirmations)),
            "request_plan_output": str(request_plan_path),
            "request_plan_rows": int(len(request_plan)),
            "observation_plan_output": str(observation_plan_path),
            "observation_plan_rows": int(len(observation_plan)),
            "flow_adjustments_output": str(flow_adjustments_path),
            "flow_adjustment_rows": int(len(flow_adjustments)),
            "status_output": str(status_path),
            "data_earliest_date": format_tushare_date(data_earliest_date) if data_earliest_date is not None else "",
            "data_latest_date": format_tushare_date(data_latest_date) if data_latest_date is not None else "",
            "verified_through": verified_through,
            "lifecycle_current": bool(lifecycle_current),
            "status_updates_enabled": bool(status_updates_enabled),
            "share_cache_rows": int(len(shares)),
            "monitor_universe_funds": int(len(universe_codes)),
            "start_date": format_tushare_date(start_date) if start_date is not None else "",
            "end_date": format_tushare_date(end_date) if end_date is not None else "",
            "min_share_change_pct": float(min_share_change_pct),
            "match_window_days": int(args.match_window_days),
            "announcement_window_days": int(announcement_window_days),
            "no_announcement_retry_window_days": int(no_announcement_retry_window_days),
            "auto_confirmed_non_lifecycle_announcement_rows": int(len(auto_confirmations)),
            "high_suspicion_min_listing_days": int(config.lifecycle_high_suspicion_min_listing_days),
            "integer_ratio_tolerance": float(config.lifecycle_integer_ratio_tolerance),
            "high_suspicion_positive_min_pct": float(config.lifecycle_high_suspicion_positive_min_pct),
            "high_suspicion_negative_max_pct": float(config.lifecycle_high_suspicion_negative_max_pct),
        }
    )

    output_dir = config.output_dir / "lifecycle_audit"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_path = output_dir / f"etf_lifecycle_share_jump_audit_{stamp}.csv"
    summary_path = output_dir / f"etf_lifecycle_share_jump_audit_{stamp}.json"
    markdown_path = output_dir / f"etf_lifecycle_share_jump_audit_{stamp}.md"
    _write_csv(audit_path, prepare_for_csv(audit))
    write_json(summary_path, summary)
    _write_markdown(markdown_path, summary, audit)

    _print_lifecycle_summary(
        summary,
        request_plan_path=request_plan_path,
        observation_plan_path=observation_plan_path,
        summary_path=summary_path,
        markdown_path=markdown_path,
        verbose=args.verbose,
    )
    return 0


def _print_lifecycle_summary(
    summary: dict[str, object],
    *,
    request_plan_path: Path | None = None,
    observation_plan_path: Path | None = None,
    summary_path: Path | None = None,
    markdown_path: Path | None = None,
    verbose: bool = False,
) -> None:
    if bool(summary.get("skipped")):
        verified = str(summary.get("verified_through") or summary.get("data_latest_date") or "")
        print(f"[life] 已核对至 {verified}，无需生成新的公告待抓清单。", flush=True)
        if verbose:
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return

    print(
        "[life] 跳变核对："
        f"已审计跳变 {_as_int(summary.get('share_jump_rows'))} 条，"
        f"生命周期事件 {_as_int(summary.get('event_rows'))} 条，"
        f"已匹配 {_as_int(summary.get('matched_share_jump_rows'))} 条，"
        f"高疑似待抓 {_as_int(summary.get('request_plan_rows'))} 条，"
        f"低疑似观察 {_as_int(summary.get('observation_plan_rows'))} 条。",
        flush=True,
    )
    if markdown_path is not None:
        print(f"[life] 详细报告：{markdown_path}", flush=True)
    if verbose:
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def _as_int(value: object) -> int:
    try:
        if pd.isna(value):
            return 0
    except (TypeError, ValueError):
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_cached_share_cross_sections(
    cache_dir: Path,
    source_name: str,
    *,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
    codes: set[str] | None = None,
) -> pd.DataFrame:
    directory = cache_dir / source_name / "daily_cross_section" / "etf_share"
    if not directory.exists():
        return normalize_etf_share_frame(None)
    frames: list[pd.DataFrame] = []
    for path in sorted(directory.glob("*.csv")):
        if len(path.stem) != 8 or not path.stem.isdigit():
            continue
        date_key = pd.to_datetime(path.stem, format="%Y%m%d", errors="coerce")
        if pd.isna(date_key):
            continue
        date_key = pd.Timestamp(date_key).normalize()
        if start_date is not None and date_key < start_date:
            continue
        if end_date is not None and date_key > end_date:
            continue
        frames.append(pd.read_csv(path, encoding="utf-8-sig"))
    if not frames:
        return normalize_etf_share_frame(None)
    result = normalize_etf_share_frame(pd.concat(frames, ignore_index=True))
    if codes:
        result = result.loc[result["fund_code"].astype(str).str.upper().isin(codes)].copy()
    return result.reset_index(drop=True)


def _manual_lifecycle_events(events: pd.DataFrame) -> pd.DataFrame:
    normalized = normalize_lifecycle_events(events)
    if normalized.empty or "review_status" not in normalized.columns:
        return normalized
    auto_mask = normalized["review_status"].fillna("").astype(str).eq("auto_from_announcement")
    return normalized.loc[~auto_mask].copy().reset_index(drop=True)


def _expand_no_announcement_retry_windows(
    request_plan: pd.DataFrame,
    pending_confirmations: pd.DataFrame,
    *,
    window_days: int,
    calendar: object,
) -> pd.DataFrame:
    if request_plan is None or request_plan.empty:
        return request_plan
    pending = normalize_pending_confirmations(pending_confirmations)
    if pending.empty:
        return request_plan
    pending = pending.loc[pending["status"].fillna("").astype(str).eq("no_announcement_found")].copy()
    if pending.empty:
        return request_plan
    retry_keys = set(_jump_keys(pending))
    result = request_plan.copy()
    result_keys = _jump_keys(result)
    retry_mask = result_keys.isin(retry_keys)
    if not retry_mask.any():
        return result
    for idx in result.index[retry_mask]:
        start, end = _trading_day_window(result.loc[idx, "trade_date"], window_days=window_days, calendar=calendar)
        result.loc[idx, "request_start_date"] = start
        result.loc[idx, "request_end_date"] = end
    return result.sort_values(["request_start_date", "fund_code", "trade_date"], kind="stable").reset_index(drop=True)


def _auto_confirm_non_lifecycle_announcement_windows(request_plan: pd.DataFrame, announcements: pd.DataFrame) -> pd.DataFrame:
    if request_plan is None or request_plan.empty or announcements is None or announcements.empty:
        return empty_manual_confirmations()
    plan = request_plan.copy()
    ann = announcements.copy()
    required = {"fund_code", "announcement_date", "title"}
    if not required.issubset(ann.columns):
        return empty_manual_confirmations()
    ann["fund_code"] = ann["fund_code"].fillna("").astype(str).str.upper()
    ann["announcement_date"] = parse_excel_friendly_date_series(ann["announcement_date"])
    for column in ("title", "content"):
        if column not in ann.columns:
            ann[column] = ""
        ann[column] = ann[column].fillna("").astype(str)
    rows: list[dict[str, object]] = []
    checked_at = datetime.now().isoformat(timespec="seconds")
    for _, row in plan.iterrows():
        fund_code = str(row.get("fund_code") or "").upper()
        start_date = parse_excel_friendly_date(row.get("request_start_date"))
        end_date = parse_excel_friendly_date(row.get("request_end_date"))
        if not fund_code or pd.isna(start_date) or pd.isna(end_date):
            continue
        hits = ann.loc[
            ann["fund_code"].eq(fund_code)
            & ann["announcement_date"].ge(pd.Timestamp(start_date).normalize())
            & ann["announcement_date"].le(pd.Timestamp(end_date).normalize())
        ].copy()
        if hits.empty:
            continue
        lifecycle_hits = [
            classify_lifecycle_announcement(f"{hit.get('title', '')} {hit.get('content', '')}")[0]
            for _, hit in hits.iterrows()
        ]
        if any(lifecycle_hits):
            continue
        rows.append(
            {
                "fund_code": fund_code,
                "name": row.get("name", ""),
                "trade_date": row.get("trade_date", pd.NaT),
                "prev_trade_date": row.get("prev_trade_date", pd.NaT),
                "share_change": row.get("share_change", pd.NA),
                "share_change_pct": row.get("share_change_pct", pd.NA),
                "confirmed_at": checked_at,
                "confirm_note": "auto_confirmed_non_lifecycle_announcements",
            }
        )
    if not rows:
        return empty_manual_confirmations()
    return normalize_manual_confirmations(pd.DataFrame(rows))


def _append_manual_confirmations(path: Path, existing: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    combined = normalize_manual_confirmations(pd.concat([existing, fresh], ignore_index=True))
    write_user_csv(path, prepare_for_csv(combined))
    return combined


def _drop_pending_confirmations(pending: pd.DataFrame, confirmations: pd.DataFrame) -> pd.DataFrame:
    pending_frame = normalize_pending_confirmations(pending)
    if pending_frame.empty:
        return pending_frame
    confirmation_keys = set(_jump_keys(normalize_manual_confirmations(confirmations)))
    if not confirmation_keys:
        return pending_frame
    keep_mask = ~_jump_keys(pending_frame).isin(confirmation_keys)
    return normalize_pending_confirmations(pending_frame.loc[keep_mask].copy())


def _trading_day_window(value: object, *, window_days: int, calendar: object) -> tuple[pd.Timestamp, pd.Timestamp]:
    parsed = parse_excel_friendly_date(value)
    if pd.isna(parsed):
        return pd.NaT, pd.NaT
    current = pd.Timestamp(parsed).normalize()
    days = max(int(window_days), 0)
    try:
        start = calendar.shift_trade_date(current.date(), -days)
        end = calendar.shift_trade_date(current.date(), days)
        return pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    except Exception:  # noqa: BLE001
        return current - pd.Timedelta(days=days), current + pd.Timedelta(days=days)


def _jump_keys(frame: pd.DataFrame) -> pd.Series:
    fund_code = frame.get("fund_code", pd.Series(index=frame.index, dtype=object)).fillna("").astype(str).str.upper()
    trade_date = parse_excel_friendly_date_series(frame.get("trade_date", pd.Series(index=frame.index, dtype=object))).map(format_tushare_date).fillna("")
    prev_trade_date = parse_excel_friendly_date_series(frame.get("prev_trade_date", pd.Series(index=frame.index, dtype=object))).map(format_tushare_date).fillna("")
    return fund_code + "|" + trade_date + "|" + prev_trade_date


def cached_share_date_bounds(
    cache_dir: Path,
    source_name: str,
    *,
    start_date: pd.Timestamp | None = None,
    end_date: pd.Timestamp | None = None,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    directory = cache_dir / source_name / "daily_cross_section" / "etf_share"
    if not directory.exists():
        return None, None
    earliest: pd.Timestamp | None = None
    latest: pd.Timestamp | None = None
    for path in directory.glob("*.csv"):
        if len(path.stem) != 8 or not path.stem.isdigit():
            continue
        parsed = pd.to_datetime(path.stem, format="%Y%m%d", errors="coerce")
        if pd.isna(parsed):
            continue
        current = pd.Timestamp(parsed).normalize()
        if start_date is not None and current < pd.Timestamp(start_date).normalize():
            continue
        if end_date is not None and current > end_date:
            continue
        if earliest is None or current < earliest:
            earliest = current
        if latest is None or current > latest:
            latest = current
    return earliest, latest


def _ensure_announcement_template(path: Path) -> None:
    if path.exists():
        return
    write_user_csv(path, empty_announcement_template())


def _read_csv_if_exists(path: Path, fallback: pd.DataFrame) -> pd.DataFrame:
    if not path.exists():
        return fallback.copy()
    return read_user_csv(path)


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    write_user_csv(path, frame)


def _read_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_is_current(
    path: Path,
    *,
    data_earliest_date: pd.Timestamp | None,
    data_latest_date: pd.Timestamp,
) -> bool:
    status = _read_status(path)
    if not bool(status.get("lifecycle_current")):
        return False
    if int(status.get("request_plan_rows") or 0) != 0:
        return False
    if data_earliest_date is None:
        return False
    verified_from = pd.to_datetime(
        status.get("verified_from") or status.get("data_earliest_date"),
        format="%Y%m%d",
        errors="coerce",
    )
    verified = pd.to_datetime(status.get("verified_through"), format="%Y%m%d", errors="coerce")
    if pd.isna(verified_from) or pd.isna(verified):
        return False
    return (
        pd.Timestamp(verified_from).normalize() <= pd.Timestamp(data_earliest_date).normalize()
        and pd.Timestamp(verified).normalize() >= pd.Timestamp(data_latest_date).normalize()
    )


def _update_lifecycle_status(
    path: Path,
    *,
    data_earliest_date: pd.Timestamp | None,
    data_latest_date: pd.Timestamp | None,
    local_cache_start_date: pd.Timestamp | None,
    lifecycle_current: bool,
    request_plan_rows: int,
) -> str:
    previous = _read_status(path)
    previous_verified_from = str(previous.get("verified_from") or previous.get("data_earliest_date") or "")
    previous_verified = str(previous.get("verified_through") or "")
    verified_from = (
        format_tushare_date(data_earliest_date)
        if lifecycle_current and data_earliest_date is not None
        else previous_verified_from
    )
    verified_through = format_tushare_date(data_latest_date) if lifecycle_current and data_latest_date is not None else previous_verified
    status = {
        "schema_version": "etf_lifecycle_status_v1",
        "data_earliest_date": format_tushare_date(data_earliest_date) if data_earliest_date is not None else "",
        "data_latest_date": format_tushare_date(data_latest_date) if data_latest_date is not None else "",
        "verified_from": verified_from,
        "verified_through": verified_through,
        "local_cache_start_date": format_tushare_date(local_cache_start_date) if local_cache_start_date is not None else "",
        "lifecycle_current": bool(lifecycle_current),
        "request_plan_rows": int(request_plan_rows),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_json(path, status)
    return verified_through


def _parse_optional_date(value: str | None) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    return pd.to_datetime(text, format="%Y%m%d", errors="raise").normalize()


def _write_markdown(path: Path, summary: dict[str, object], audit: pd.DataFrame) -> None:
    lines = [
        "# ETF 生命周期份额跳变核对报告",
        "",
        f"- 生命周期事件数：{summary['event_rows']}",
        f"- 疑似份额跳变数：{summary['share_jump_rows']}",
        f"- 已匹配生命周期事件：{summary['matched_share_jump_rows']}",
        f"- 未匹配：{summary['unmatched_share_jump_rows']}",
        f"- 高疑似待抓公告：{summary.get('request_plan_rows', 0)}",
        f"- 低疑似观察记录：{summary.get('observation_plan_rows', 0)}",
        "",
        "## 未匹配份额跳变 Top 30",
        "",
    ]
    if audit.empty or "match_status" not in audit.columns:
        unmatched = audit
    else:
        resolved_statuses = {MATCH_STATUS_MATCHED, MATCH_STATUS_MANUAL_CONFIRMED}
        unmatched = audit.loc[~audit["match_status"].fillna("").astype(str).isin(resolved_statuses)].copy()
    if unmatched.empty:
        lines.append("无。")
    else:
        display = unmatched.sort_values(["abs_share_change"], ascending=[False], kind="stable").head(30)
        lines.extend(_markdown_table(display, ["fund_code", "name", "trade_date", "share_change", "share_change_pct", "shares", "prev_shares"]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _markdown_table(frame: pd.DataFrame, columns: list[str]) -> list[str]:
    if frame.empty:
        return ["无。"]
    available = [column for column in columns if column in frame.columns]
    rows = [["代码", "名称", "日期", "份额变化(万份)", "变化比例", "当前份额(万份)", "前值份额(万份)"][: len(available)]]
    for _, row in frame[available].iterrows():
        values: list[str] = []
        for column in available:
            value = row[column]
            if column in {"trade_date", "prev_trade_date"}:
                values.append(format_tushare_date(value) if pd.notna(value) else "")
            elif column == "share_change_pct":
                values.append(f"{float(value):.2%}" if pd.notna(value) else "")
            elif column in {"share_change", "shares", "prev_shares", "abs_share_change"}:
                values.append(f"{float(value):,.2f}" if pd.notna(value) else "")
            else:
                values.append(str(value) if pd.notna(value) else "")
        rows.append(values)
    separator = ["---"] * len(rows[0])
    return [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(separator) + " |",
        *["| " + " | ".join(row) + " |" for row in rows[1:]],
    ]


if __name__ == "__main__":
    raise SystemExit(main())
