"""Fetch ETF announcement rows into the local lifecycle announcement table."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
import time

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import category_map_codes, load_category_map  # noqa: E402
from etf_flow_monitor.data.cninfo_announcements import CninfoAnnouncementClient  # noqa: E402
from etf_flow_monitor.data.exchange_announcements import ExchangeAnnouncementClient  # noqa: E402
from etf_flow_monitor.data.lifecycle import (  # noqa: E402
    MANUAL_CONFIRMATION_COLUMNS,
    classify_lifecycle_announcement,
    empty_announcement_template,
    empty_manual_confirmations,
    empty_pending_confirmations,
    normalize_manual_confirmations,
    normalize_pending_confirmations,
    prepare_for_csv as prepare_lifecycle_for_csv,
)
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.data.tushare_http import TusharePermissionError  # noqa: E402
from etf_flow_monitor.run_ledger import RunLedger, make_log_dir  # noqa: E402
from etf_flow_monitor.utils.calendar import current_shanghai_date, normalize_date_input  # noqa: E402
from etf_flow_monitor.utils.io import (
    clean_excel_text,
    format_tushare_date,
    parse_excel_friendly_date,
    parse_excel_friendly_date_series,
    read_user_csv,
    write_json,
    write_user_csv,
)  # noqa: E402


ANNOUNCEMENT_COLUMNS = ["fund_code", "announcement_date", "event_date", "title", "content", "source_url"]
DEFAULT_ANNOUNCEMENT_FIELDS = "ts_code,ann_date,name,title,url,rec_time"
POSSIBLE_LIQUIDATION_WARNING_KEYWORDS = ("可能触发基金合同终止",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update local ETF announcement CSV from CNINFO, exchange, or Tushare sources.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--source", default="", choices=("", "cninfo", "exchange", "tushare"), help="Blank uses config announcement_source.")
    parser.add_argument("--start-date", default="", help="Fetch start date YYYYMMDD. Blank = local_cache_start_date - 12 months.")
    parser.add_argument("--end-date", default="", help="Fetch end date YYYYMMDD. Blank = Shanghai today.")
    parser.add_argument("--announcement-file", default="", help="Output CSV. Blank uses config announcement_file_path.")
    parser.add_argument("--request-plan", default="", help="Announcement request plan CSV. Blank uses config lifecycle_request_plan_path.")
    parser.add_argument("--pending-confirmations", default="", help="No-announcement pending confirmation CSV. Blank uses config.")
    parser.add_argument("--manual-confirmations", default="", help="Manual no-event confirmation CSV. Blank uses config.")
    parser.add_argument("--api-name", default="", help="Tushare announcement API name. Blank uses config announcement_api_name.")
    parser.add_argument("--fields", default=DEFAULT_ANNOUNCEMENT_FIELDS)
    parser.add_argument("--codes", default="", help="Comma-separated fund codes for a targeted update.")
    parser.add_argument("--max-codes", type=int, default=0, help="Limit code count for smoke tests. 0 = all.")
    parser.add_argument("--exchange-page-size", type=int, default=50)
    parser.add_argument("--exchange-sleep-seconds", type=float, default=None, help="CNINFO/exchange request interval. Blank uses config.")
    parser.add_argument("--exchange-retries", type=int, default=3)
    parser.add_argument("--exchange-retry-sleep-seconds", type=float, default=5.0)
    parser.add_argument("--heartbeat-seconds", type=int, default=20)
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument("--stop-on-max-errors", action="store_true", help="Abort exchange update after --max-errors final failures.")
    parser.add_argument("--verbose", action="store_true", help="Print full JSON summary to console.")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    ledger = RunLedger(log_dir=make_log_dir(config.output_dir, prefix="announcement_update"), argv=raw_argv, config_path=config_path)

    try:
        announcement_path = Path(args.announcement_file or config.announcement_file_path)
        pending_confirmations_path = Path(args.pending_confirmations or config.lifecycle_pending_confirmations_path)
        manual_confirmations_path = Path(args.manual_confirmations or config.lifecycle_manual_confirmations_path)
        request_plan = pd.DataFrame()
        explicit_source_codes = False
        source_name = str(args.source or config.announcement_source or "cninfo").strip().lower() or "cninfo"
        if source_name in {"cninfo", "exchange"}:
            request_plan_path = Path(args.request_plan or config.lifecycle_request_plan_path)
            request_plan = _read_request_plan(request_plan_path)
            explicit_source_codes = bool([code.strip() for code in str(args.codes or "").split(",") if code.strip()])
            jobs = _resolve_exchange_jobs(
                request_plan=request_plan,
                raw_codes=args.codes,
                start_date_arg=args.start_date,
                end_date_arg=args.end_date,
            )
            if args.max_codes and args.max_codes > 0:
                allowed = {job["fund_code"] for job in jobs[: int(args.max_codes)]}
                jobs = [job for job in jobs if job["fund_code"] in allowed]
            if not jobs:
                summary = {
                    "schema_version": "etf_announcement_update_v1",
                    "source": source_name,
                    "announcement_file": str(announcement_path),
                    "request_plan": str(request_plan_path),
                    "pending_confirmations": str(pending_confirmations_path),
                    "pending_confirmation_rows": 0,
                    "skip_reason": "empty announcement request plan",
                    "jobs": 0,
                    "completed_jobs": 0,
                    "failed_jobs": 0,
                    "fresh_rows": 0,
                    "errors": 0,
                }
                summary_path = _write_update_summary(config.output_dir, summary)
                return _finish(ledger, 0, "skipped", summary, {"summary": summary_path}, verbose=args.verbose)
            print(f"[ann] source={source_name} jobs={len(jobs)} request_plan={request_plan_path}", flush=True)
            ledger.progress("fetch_announcements", f"source={source_name} jobs={len(jobs)}")
            sleep_seconds = (
                float(args.exchange_sleep_seconds)
                if args.exchange_sleep_seconds is not None
                else float(config.announcement_sleep_seconds)
            )
            client = (
                CninfoAnnouncementClient(
                    page_size=args.exchange_page_size,
                    sleep_seconds=sleep_seconds,
                    ignore_proxy=True,
                )
                if source_name == "cninfo"
                else ExchangeAnnouncementClient(
                    page_size=args.exchange_page_size,
                    sleep_seconds=sleep_seconds,
                    ignore_proxy=True,
                )
            )
            fresh, errors, remote_skipped_reason = fetch_exchange_announcements(
                client,
                jobs,
                heartbeat_seconds=args.heartbeat_seconds,
                max_errors=args.max_errors,
                retries=args.exchange_retries,
                retry_sleep_seconds=args.exchange_retry_sleep_seconds,
                stop_on_max_errors=args.stop_on_max_errors,
                source_label=source_name,
            )
            api_name = ""
            start_date = min((job["start_date"] for job in jobs), default=pd.NaT)
            end_date = max((job["end_date"] for job in jobs), default=pd.NaT)
            code_count = len({job["fund_code"] for job in jobs})
            source_detail = {
                "request_plan": str(request_plan_path),
                "jobs": int(len(jobs)),
                "pending_confirmations": str(pending_confirmations_path),
            }
        elif source_name == "tushare":
            source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
            basic = source.get_etf_basic(market=config.etf_market, refresh=False)
            category_map = load_category_map(config.category_map_path)
            codes = _resolve_codes(args.codes, category_map, basic)
            if args.max_codes and args.max_codes > 0:
                codes = codes[: int(args.max_codes)]
            if not codes:
                summary = {"schema_version": "etf_announcement_update_v1", "source": source_name, "skip_reason": "empty ETF universe", "codes": 0, "fresh_rows": 0, "errors": 0}
                summary_path = _write_update_summary(config.output_dir, summary)
                return _finish(ledger, 0, "skipped", summary, {"summary": summary_path}, verbose=args.verbose)

            start_date = _resolve_start_date(args.start_date, config.local_cache_start_date)
            end_date = _resolve_end_date(args.end_date)
            api_name = str(args.api_name or config.announcement_api_name or "anns_d").strip() or "anns_d"

            print(f"[ann] source=tushare api={api_name} start={format_tushare_date(start_date)} end={format_tushare_date(end_date)} codes={len(codes)}", flush=True)
            ledger.progress(
                "fetch_announcements",
                f"source=tushare api={api_name} start={format_tushare_date(start_date)} end={format_tushare_date(end_date)} codes={len(codes)}",
            )
            fresh, errors, remote_skipped_reason = fetch_tushare_announcements(
                source.client,
                codes,
                start_date=start_date,
                end_date=end_date,
                api_name=api_name,
                fields=args.fields,
                heartbeat_seconds=args.heartbeat_seconds,
                max_errors=args.max_errors,
            )
            code_count = len(codes)
            source_detail = {}
        else:
            raise ValueError(f"unsupported announcement source: {source_name}")

        initial_error_count = int(len(errors))
        existing = _read_announcements(announcement_path)
        merged = merge_announcement_frames(existing, fresh)
        follow_up_jobs: list[dict[str, object]] = []
        follow_up_errors: list[dict[str, str]] = []
        follow_up_fresh = pd.DataFrame()
        if source_name in {"cninfo", "exchange"}:
            follow_up_jobs = build_liquidation_follow_up_jobs(
                request_plan,
                merged,
                follow_up_end_date=_resolve_end_date(args.end_date),
            )
            if follow_up_jobs:
                print(f"[ann] liquidation follow-up jobs={len(follow_up_jobs)}", flush=True)
                follow_up_fresh, follow_up_errors, _ = fetch_exchange_announcements(
                    client,
                    follow_up_jobs,
                    heartbeat_seconds=args.heartbeat_seconds,
                    max_errors=args.max_errors,
                    retries=args.exchange_retries,
                    retry_sleep_seconds=args.exchange_retry_sleep_seconds,
                    stop_on_max_errors=args.stop_on_max_errors,
                    source_label=f"{source_name}-liquidation-followup",
                )
                fresh = normalize_announcement_frame(pd.concat([fresh, follow_up_fresh], ignore_index=True))
                errors.extend(follow_up_errors)
                merged = merge_announcement_frames(existing, fresh)
        _write_announcements(announcement_path, merged)
        pending_confirmations = empty_pending_confirmations()
        auto_confirmed_no_announcement = empty_manual_confirmations()
        if source_name in {"cninfo", "exchange"} and not explicit_source_codes:
            previous_pending_confirmations = _read_pending_confirmations(pending_confirmations_path)
            failed_codes = {str(item.get("fund_code") or "").upper() for item in errors if item.get("fund_code")}
            pending_confirmations = build_pending_confirmations_from_request_plan(
                request_plan,
                merged,
                failed_codes=failed_codes,
            )
            auto_confirmed_no_announcement = build_auto_confirmations_from_retried_windows(
                request_plan,
                merged,
                previous_pending_confirmations,
                failed_codes=failed_codes,
            )
            if not auto_confirmed_no_announcement.empty:
                _append_manual_confirmations(manual_confirmations_path, auto_confirmed_no_announcement)
                pending_confirmations = _drop_confirmed_pending(pending_confirmations, auto_confirmed_no_announcement)
            _write_pending_confirmations(pending_confirmations_path, pending_confirmations)
            if not auto_confirmed_no_announcement.empty:
                print(
                    f"[ann] {len(auto_confirmed_no_announcement)} 条扩大窗口后仍未抓到公告，已自动确认无生命周期事件。",
                    flush=True,
                )
            if not pending_confirmations.empty:
                print(
                    f"[ann] {len(pending_confirmations)} 条跳变窗口未抓到公告；"
                    f"下次运行会扩大公告窗口后继续重试。清单：{pending_confirmations_path}",
                    flush=True,
                )

        summary = {
            "schema_version": "etf_announcement_update_v1",
            "source": source_name,
            "api_name": api_name,
            "announcement_file": str(announcement_path),
            "start_date": format_tushare_date(start_date) if pd.notna(start_date) else "",
            "end_date": format_tushare_date(end_date) if pd.notna(end_date) else "",
            "codes": int(code_count),
            "fresh_rows": int(len(fresh)),
            "existing_rows": int(len(existing)),
            "merged_rows": int(len(merged)),
            "errors": int(len(errors)),
            "initial_errors": int(initial_error_count),
            "error_samples": errors[:5],
            "remote_skipped_reason": remote_skipped_reason,
            "liquidation_follow_up_jobs": int(len(follow_up_jobs)),
            "liquidation_follow_up_rows": int(len(follow_up_fresh)),
            "liquidation_follow_up_errors": int(len(follow_up_errors)),
        }
        summary.update(source_detail)
        if source_name in {"cninfo", "exchange"}:
            completed_jobs = int(summary.get("jobs", 0)) - int(initial_error_count)
            summary["completed_jobs"] = max(completed_jobs, 0)
            summary["failed_jobs"] = int(initial_error_count)
            summary["pending_confirmation_rows"] = int(len(pending_confirmations))
            summary["auto_confirmed_no_announcement_rows"] = int(len(auto_confirmed_no_announcement))
            if errors:
                summary["pending_skipped_error_codes"] = sorted({str(item.get("fund_code") or "").upper() for item in errors if item.get("fund_code")})
        summary_path = _write_update_summary(config.output_dir, summary)
        ledger.record_outputs(announcement_file=str(announcement_path), summary=str(summary_path))
        finish_status = "partial_success" if errors else "success"
        return _finish(ledger, 0, finish_status, summary, {"summary": summary_path}, verbose=args.verbose)
    except Exception as exc:  # noqa: BLE001
        ledger.finish(exit_code=1, status="failed", error=str(exc))
        print(f"[ann] update failed: {exc}", file=sys.stderr, flush=True)
        return 1


def fetch_tushare_announcements(
    client,
    codes: list[str],
    *,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    api_name: str,
    fields: str,
    heartbeat_seconds: int = 20,
    max_errors: int = 10,
) -> tuple[pd.DataFrame, list[dict[str, str]], str]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    remote_skipped_reason = ""
    started_at = time.monotonic()
    last_heartbeat_at = started_at
    for idx, code in enumerate(codes, start=1):
        try:
            payload = client.query(
                api_name,
                params={
                    "ts_code": code,
                    "start_date": format_tushare_date(start_date),
                    "end_date": format_tushare_date(end_date),
                },
                fields=fields,
            )
            rows.extend(payload)
        except TusharePermissionError as exc:
            remote_skipped_reason = str(exc)
            errors.append({"fund_code": code, "error": str(exc)})
            print(f"[ann] remote announcement update skipped: {exc}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append({"fund_code": code, "error": str(exc)})
            print(f"[ann] fetch failed code={code}: {exc}", flush=True)
            if len(errors) >= max(int(max_errors), 1):
                raise RuntimeError(f"announcement update stopped after {len(errors)} errors; first={errors[0]}")
        now = time.monotonic()
        if now - last_heartbeat_at >= max(int(heartbeat_seconds), 1):
            elapsed = int(now - started_at)
            print(f"[ann heartbeat] progress={idx}/{len(codes)} rows={len(rows)} errors={len(errors)} elapsed={elapsed}s", flush=True)
            last_heartbeat_at = now
    return normalize_announcement_frame(pd.DataFrame(rows)), errors, remote_skipped_reason


def fetch_exchange_announcements(
    client: object,
    jobs: list[dict[str, object]],
    *,
    heartbeat_seconds: int = 20,
    max_errors: int = 10,
    retries: int = 3,
    retry_sleep_seconds: float = 5.0,
    stop_on_max_errors: bool = False,
    source_label: str = "exchange",
) -> tuple[pd.DataFrame, list[dict[str, str]], str]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    started_at = time.monotonic()
    last_heartbeat_at = started_at
    max_attempts = max(int(retries), 0) + 1
    retry_sleep = max(float(retry_sleep_seconds), 0.0)
    for idx, job in enumerate(jobs, start=1):
        fund_code = str(job["fund_code"])
        start_date = pd.Timestamp(job["start_date"]).normalize()
        end_date = pd.Timestamp(job["end_date"]).normalize()
        for attempt in range(1, max_attempts + 1):
            try:
                rows.extend(client.fetch(fund_code, start_date=start_date, end_date=end_date))
                break
            except Exception as exc:  # noqa: BLE001
                if attempt < max_attempts:
                    wait_seconds = retry_sleep * attempt
                    print(
                        f"[ann retry] code={fund_code} window={format_tushare_date(start_date)}-{format_tushare_date(end_date)} "
                        f"attempt={attempt}/{max_attempts} wait={wait_seconds:.1f}s error={exc}",
                        flush=True,
                    )
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    continue
                errors.append(
                    {
                        "fund_code": fund_code,
                        "start_date": format_tushare_date(start_date),
                        "end_date": format_tushare_date(end_date),
                        "attempts": str(max_attempts),
                        "error": str(exc),
                    }
                )
                print(
                    f"[ann] {source_label} fetch failed code={fund_code} "
                    f"window={format_tushare_date(start_date)}-{format_tushare_date(end_date)} "
                    f"after {max_attempts} attempts: {exc}",
                    flush=True,
                )
                if stop_on_max_errors and len(errors) >= max(int(max_errors), 1):
                    raise RuntimeError(f"{source_label} announcement update stopped after {len(errors)} errors; first={errors[0]}")
        _sleep_after_exchange_job(client)
        now = time.monotonic()
        if now - last_heartbeat_at >= max(int(heartbeat_seconds), 1):
            elapsed = int(now - started_at)
            print(f"[ann heartbeat] progress={idx}/{len(jobs)} rows={len(rows)} errors={len(errors)} elapsed={elapsed}s", flush=True)
            last_heartbeat_at = now
    return normalize_announcement_frame(pd.DataFrame(rows)), errors, ""


def _sleep_after_exchange_job(client: object) -> None:
    sleep_seconds = getattr(client, "sleep_seconds", 0.0)
    try:
        seconds = max(float(sleep_seconds), 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds > 0:
        time.sleep(seconds)


def normalize_announcement_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
    working = pd.DataFrame() if frame is None else frame.copy()
    working = working.rename(
        columns={
            "ts_code": "fund_code",
            "ann_date": "announcement_date",
            "pub_date": "announcement_date",
            "publish_date": "announcement_date",
            "url": "source_url",
            "link": "source_url",
            "ann_title": "title",
            "summary": "content",
            "text": "content",
        }
    )
    for column in ANNOUNCEMENT_COLUMNS:
        if column not in working.columns:
            working[column] = pd.NA
    result = working[ANNOUNCEMENT_COLUMNS].copy()
    result["fund_code"] = result["fund_code"].map(clean_excel_text).str.upper()
    for column in ("announcement_date", "event_date"):
        result[column] = parse_excel_friendly_date_series(result[column])
    for column in ("title", "content", "source_url"):
        result[column] = result[column].map(clean_excel_text)
    result = result.loc[result["fund_code"].ne("") & result["announcement_date"].notna() & result["title"].ne("")].copy()
    return result.sort_values(["announcement_date", "fund_code", "title"], kind="stable").reset_index(drop=True)


def merge_announcement_frames(existing: pd.DataFrame | None, fresh: pd.DataFrame | None) -> pd.DataFrame:
    left = normalize_announcement_frame(existing)
    right = normalize_announcement_frame(fresh)
    if left.empty:
        return right
    if right.empty:
        return left
    merged = pd.concat([left.assign(_source_order=0), right.assign(_source_order=1)], ignore_index=True)
    merged = merged.sort_values(["fund_code", "announcement_date", "title", "_source_order"], kind="stable")
    rows = []
    for _, group in merged.groupby(["fund_code", "announcement_date", "title"], sort=False, dropna=False):
        base = group.iloc[0].copy()
        for _, row in group.iloc[1:].iterrows():
            for column in ANNOUNCEMENT_COLUMNS:
                if _is_blank_value(base.get(column)) and not _is_blank_value(row.get(column)):
                    base[column] = row[column]
        rows.append(base.drop(labels=["_source_order"], errors="ignore"))
    return normalize_announcement_frame(pd.DataFrame(rows))


def prepare_announcements_for_csv(frame: pd.DataFrame) -> pd.DataFrame:
    result = normalize_announcement_frame(frame)
    for column in ("announcement_date", "event_date"):
        result[column] = result[column].map(lambda value: format_tushare_date(value) if pd.notna(value) else "")
    return result[ANNOUNCEMENT_COLUMNS]


def _resolve_exchange_jobs(
    *,
    request_plan: pd.DataFrame,
    raw_codes: str,
    start_date_arg: str,
    end_date_arg: str,
) -> list[dict[str, object]]:
    explicit_codes = [clean_excel_text(code).upper() for code in str(raw_codes or "").split(",") if clean_excel_text(code)]
    if explicit_codes:
        start_date = _resolve_start_date(start_date_arg, current_shanghai_date())
        end_date = _resolve_end_date(end_date_arg)
        return [{"fund_code": code, "start_date": start_date, "end_date": end_date} for code in dict.fromkeys(explicit_codes)]
    if request_plan.empty:
        return []
    return _merge_exchange_request_windows(request_plan)


def _read_request_plan(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return _normalize_request_plan(read_user_csv(path))


def _normalize_request_plan(frame: pd.DataFrame | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    required = {"fund_code", "request_start_date", "request_end_date"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    frame = frame.copy()
    frame["fund_code"] = frame["fund_code"].map(clean_excel_text).str.upper()
    frame["request_start_date"] = parse_excel_friendly_date_series(frame["request_start_date"])
    frame["request_end_date"] = parse_excel_friendly_date_series(frame["request_end_date"])
    for column in ("trade_date", "prev_trade_date"):
        if column in frame.columns:
            frame[column] = parse_excel_friendly_date_series(frame[column])
    for column in ("name",):
        if column in frame.columns:
            frame[column] = frame[column].map(clean_excel_text)
    for column in ("share_change", "share_change_pct"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.loc[frame["fund_code"].ne("") & frame["request_start_date"].notna() & frame["request_end_date"].notna()].copy()
    return frame.sort_values(["fund_code", "request_start_date", "request_end_date"], kind="stable").reset_index(drop=True)


def _merge_exchange_request_windows(plan: pd.DataFrame) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    if plan.empty:
        return jobs
    for fund_code, group in plan.groupby("fund_code", sort=True):
        current_start: pd.Timestamp | None = None
        current_end: pd.Timestamp | None = None
        for _, row in group.sort_values(["request_start_date", "request_end_date"], kind="stable").iterrows():
            start = pd.Timestamp(row["request_start_date"]).normalize()
            end = pd.Timestamp(row["request_end_date"]).normalize()
            if current_start is None or current_end is None:
                current_start, current_end = start, end
                continue
            if start <= current_end + pd.Timedelta(days=1):
                current_end = max(current_end, end)
                continue
            jobs.append({"fund_code": str(fund_code), "start_date": current_start, "end_date": current_end})
            current_start, current_end = start, end
        if current_start is not None and current_end is not None:
            jobs.append({"fund_code": str(fund_code), "start_date": current_start, "end_date": current_end})
    return jobs


def build_liquidation_follow_up_jobs(
    request_plan: pd.DataFrame | None,
    announcements: pd.DataFrame | None,
    *,
    follow_up_end_date: object,
) -> list[dict[str, object]]:
    """Probe forward from possible liquidation warnings until the current update end date."""
    plan = _normalize_request_plan(request_plan)
    ann = normalize_announcement_frame(announcements)
    if plan.empty or ann.empty:
        return []
    follow_up_end = parse_excel_friendly_date(follow_up_end_date)
    if pd.isna(follow_up_end):
        return []
    follow_up_end = pd.Timestamp(follow_up_end).normalize()

    rows: list[dict[str, object]] = []
    for _, row in plan.iterrows():
        fund_code = str(row.get("fund_code") or "").upper()
        start = parse_excel_friendly_date(row.get("request_start_date"))
        end = parse_excel_friendly_date(row.get("request_end_date"))
        if not fund_code or pd.isna(start) or pd.isna(end):
            continue
        hits = ann.loc[
            ann["fund_code"].eq(fund_code)
            & ann["announcement_date"].ge(pd.Timestamp(start).normalize())
            & ann["announcement_date"].le(pd.Timestamp(end).normalize())
        ].copy()
        if hits.empty:
            continue
        warning_hits = hits.loc[hits.apply(_is_possible_liquidation_warning, axis=1)].copy()
        if warning_hits.empty:
            continue
        warning_start = pd.Timestamp(warning_hits["announcement_date"].min()).normalize()
        if warning_start > follow_up_end:
            continue
        rows.append(
            {
                "fund_code": fund_code,
                "request_start_date": warning_start,
                "request_end_date": follow_up_end,
            }
        )
    return _merge_exchange_request_windows(pd.DataFrame(rows))


def _is_possible_liquidation_warning(row: pd.Series) -> bool:
    text = f"{row.get('title', '')} {row.get('content', '')}"
    if classify_lifecycle_announcement(text)[0] == "liquidation":
        return False
    return any(keyword in text for keyword in POSSIBLE_LIQUIDATION_WARNING_KEYWORDS)


def _resolve_codes(raw_codes: str, category_map: pd.DataFrame, basic: pd.DataFrame) -> list[str]:
    explicit = [clean_excel_text(code).upper() for code in str(raw_codes or "").split(",") if clean_excel_text(code)]
    if explicit:
        return list(dict.fromkeys(explicit))
    category_codes = set(category_map_codes(category_map))
    basic_codes = set(basic["fund_code"].dropna().astype(str).str.upper()) if basic is not None and "fund_code" in basic.columns else set()
    codes = category_codes & basic_codes if category_codes and basic_codes else category_codes or basic_codes
    return sorted(codes)


def _resolve_start_date(value: str, local_cache_start_date: object) -> pd.Timestamp:
    if str(value or "").strip():
        return pd.Timestamp(normalize_date_input(value, field_name="start_date")).normalize()
    return pd.Timestamp(local_cache_start_date).normalize() - pd.DateOffset(months=12)


def _resolve_end_date(value: str) -> pd.Timestamp:
    if str(value or "").strip():
        return pd.Timestamp(normalize_date_input(value, field_name="end_date")).normalize()
    return pd.Timestamp(current_shanghai_date()).normalize()


def _read_announcements(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_announcement_template()
    return read_user_csv(path)


def _write_announcements(path: Path, frame: pd.DataFrame) -> None:
    write_user_csv(path, prepare_announcements_for_csv(frame))


def build_pending_confirmations_from_request_plan(
    request_plan: pd.DataFrame | None,
    announcements: pd.DataFrame | None,
    *,
    failed_codes: set[str] | None = None,
) -> pd.DataFrame:
    plan = _normalize_request_plan(request_plan)
    if plan.empty:
        return empty_pending_confirmations()
    failed = {str(code or "").upper() for code in (failed_codes or set()) if str(code or "").strip()}
    normalized_announcements = normalize_announcement_frame(announcements)
    rows: list[dict[str, object]] = []
    checked_at = datetime.now().isoformat(timespec="seconds")
    for _, row in plan.iterrows():
        fund_code = str(row["fund_code"]).upper()
        if fund_code in failed:
            continue
        start_date = pd.Timestamp(row["request_start_date"]).normalize()
        end_date = pd.Timestamp(row["request_end_date"]).normalize()
        hits = normalized_announcements.loc[
            normalized_announcements["fund_code"].eq(fund_code)
            & normalized_announcements["announcement_date"].ge(start_date)
            & normalized_announcements["announcement_date"].le(end_date)
        ]
        if not hits.empty:
            continue
        rows.append(
            {
                "fund_code": fund_code,
                "name": row.get("name", ""),
                "trade_date": row.get("trade_date", pd.NaT),
                "prev_trade_date": row.get("prev_trade_date", pd.NaT),
                "request_start_date": start_date,
                "request_end_date": end_date,
                "share_change": row.get("share_change", pd.NA),
                "share_change_pct": row.get("share_change_pct", pd.NA),
                "checked_start_date": start_date,
                "checked_end_date": end_date,
                "announcement_rows": 0,
                "last_checked_at": checked_at,
                "status": "no_announcement_found",
            }
        )
    return normalize_pending_confirmations(pd.DataFrame(rows))


def _write_pending_confirmations(path: Path, frame: pd.DataFrame) -> None:
    write_user_csv(path, prepare_lifecycle_for_csv(normalize_pending_confirmations(frame)))


def _read_pending_confirmations(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_pending_confirmations()
    return normalize_pending_confirmations(read_user_csv(path))


def build_auto_confirmations_from_retried_no_announcements(
    pending_confirmations: pd.DataFrame | None,
    previous_pending_confirmations: pd.DataFrame | None,
) -> pd.DataFrame:
    current = normalize_pending_confirmations(pending_confirmations)
    previous = normalize_pending_confirmations(previous_pending_confirmations)
    if current.empty or previous.empty:
        return empty_manual_confirmations()
    previous_keys = set(_pending_keys(previous))
    current_keys = _pending_keys(current)
    retry_failed = current.loc[current_keys.isin(previous_keys)].copy()
    if retry_failed.empty:
        return empty_manual_confirmations()

    result = pd.DataFrame(columns=MANUAL_CONFIRMATION_COLUMNS)
    for column in ("fund_code", "name", "trade_date", "prev_trade_date", "share_change", "share_change_pct"):
        result[column] = retry_failed[column] if column in retry_failed.columns else pd.NA
    result["confirmed_at"] = datetime.now().isoformat(timespec="seconds")
    result["confirm_note"] = "auto_confirmed_no_announcement_after_retry"
    return normalize_manual_confirmations(result[MANUAL_CONFIRMATION_COLUMNS])


def build_auto_confirmations_from_retried_windows(
    request_plan: pd.DataFrame | None,
    announcements: pd.DataFrame | None,
    previous_pending_confirmations: pd.DataFrame | None,
    *,
    failed_codes: set[str] | None = None,
) -> pd.DataFrame:
    plan = _normalize_request_plan(request_plan)
    previous = normalize_pending_confirmations(previous_pending_confirmations)
    if plan.empty or previous.empty:
        return empty_manual_confirmations()
    previous_keys = set(_pending_keys(previous))
    failed = {str(code or "").upper() for code in (failed_codes or set()) if str(code or "").strip()}
    ann = normalize_announcement_frame(announcements)

    rows: list[dict[str, object]] = []
    checked_at = datetime.now().isoformat(timespec="seconds")
    plan_keys = _pending_keys(plan)
    for idx, row in plan.loc[plan_keys.isin(previous_keys)].iterrows():
        fund_code = str(row.get("fund_code") or "").upper()
        if not fund_code or fund_code in failed:
            continue
        start_date = parse_excel_friendly_date(row.get("request_start_date"))
        end_date = parse_excel_friendly_date(row.get("request_end_date"))
        if pd.isna(start_date) or pd.isna(end_date):
            continue
        hits = ann.loc[
            ann["fund_code"].eq(fund_code)
            & ann["announcement_date"].ge(pd.Timestamp(start_date).normalize())
            & ann["announcement_date"].le(pd.Timestamp(end_date).normalize())
        ].copy()
        lifecycle_hit = False
        for _, hit in hits.iterrows():
            event_type, _ = classify_lifecycle_announcement(f"{hit.get('title', '')} {hit.get('content', '')}")
            if event_type:
                lifecycle_hit = True
                break
        if lifecycle_hit:
            continue
        note = "auto_confirmed_no_announcement_after_retry" if hits.empty else "auto_confirmed_no_lifecycle_after_retry"
        rows.append(
            {
                "fund_code": fund_code,
                "name": row.get("name", ""),
                "trade_date": row.get("trade_date", pd.NaT),
                "prev_trade_date": row.get("prev_trade_date", pd.NaT),
                "share_change": row.get("share_change", pd.NA),
                "share_change_pct": row.get("share_change_pct", pd.NA),
                "confirmed_at": checked_at,
                "confirm_note": note,
            }
        )
    if not rows:
        return empty_manual_confirmations()
    return normalize_manual_confirmations(pd.DataFrame(rows))


def _append_manual_confirmations(path: Path, fresh: pd.DataFrame) -> pd.DataFrame:
    existing = normalize_manual_confirmations(read_user_csv(path)) if path.exists() else empty_manual_confirmations()
    combined = normalize_manual_confirmations(pd.concat([existing, fresh], ignore_index=True))
    write_user_csv(path, prepare_lifecycle_for_csv(combined))
    return combined


def _drop_confirmed_pending(pending: pd.DataFrame, confirmations: pd.DataFrame) -> pd.DataFrame:
    confirmation_keys = set(_pending_keys(normalize_manual_confirmations(confirmations)))
    if not confirmation_keys:
        return normalize_pending_confirmations(pending)
    pending_frame = normalize_pending_confirmations(pending)
    keep_mask = ~_pending_keys(pending_frame).isin(confirmation_keys)
    return normalize_pending_confirmations(pending_frame.loc[keep_mask].copy())


def _pending_keys(frame: pd.DataFrame) -> pd.Series:
    working = frame.copy()
    fund_code = working.get("fund_code", pd.Series(index=working.index, dtype=object)).map(clean_excel_text).str.upper()
    trade_date = parse_excel_friendly_date_series(working.get("trade_date", pd.Series(index=working.index, dtype=object))).map(format_tushare_date)
    prev_trade_date = parse_excel_friendly_date_series(working.get("prev_trade_date", pd.Series(index=working.index, dtype=object))).map(format_tushare_date)
    return fund_code + "|" + trade_date.fillna("") + "|" + prev_trade_date.fillna("")


def _write_update_summary(output_dir: Path, summary: dict[str, object]) -> Path:
    directory = output_dir / "lifecycle_audit"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"announcement_update_{stamp}.json"
    write_json(path, summary)
    return path


def _is_blank_value(value: object) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip() == ""


def _finish(
    ledger: RunLedger,
    exit_code: int,
    status: str,
    stats: dict,
    outputs: dict[str, Path],
    *,
    verbose: bool = False,
) -> int:
    ledger.record_stats(stats)
    if outputs:
        ledger.record_outputs(**{key: str(value) for key, value in outputs.items()})
    ledger.finish(exit_code=exit_code, status=status)
    if verbose:
        print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    else:
        _print_finish_summary(status, stats, outputs)
    return int(exit_code)


def _print_finish_summary(status: str, stats: dict, outputs: dict[str, Path]) -> None:
    skip_reason = str(stats.get("skip_reason") or "").strip()
    if status == "skipped" or skip_reason:
        print(f"[ann] 跳过：{skip_reason or status}", flush=True)
        return

    source = str(stats.get("source") or "").strip() or "-"
    pending_rows = stats.get("pending_confirmation_rows")
    jobs = stats.get("jobs")
    completed_jobs = stats.get("completed_jobs")
    failed_jobs = stats.get("failed_jobs", stats.get("errors"))
    follow_up_jobs = _as_int(stats.get("liquidation_follow_up_jobs"))
    follow_up_rows = _as_int(stats.get("liquidation_follow_up_rows"))
    follow_up_errors = _as_int(stats.get("liquidation_follow_up_errors"))
    parts = [
        f"来源={source}",
        f"新抓公告={_as_int(stats.get('fresh_rows'))}",
        f"本地公告表={_as_int(stats.get('merged_rows'))}",
    ]
    if jobs is not None:
        parts.extend(
            [
                f"任务={_as_int(jobs)}",
                f"完成={_as_int(completed_jobs)}",
                f"失败={_as_int(failed_jobs)}",
            ]
        )
    else:
        parts.append(f"错误={_as_int(stats.get('errors'))}")
    if follow_up_jobs:
        parts.append(f"清盘后探测={follow_up_jobs}任务/{follow_up_rows}条/失败{follow_up_errors}")
    if pending_rows is not None:
        parts.append(f"待重抓/核对={_as_int(pending_rows)}")
    label = "部分完成" if status == "partial_success" or _as_int(failed_jobs) or follow_up_errors else "完成"
    print(f"[ann] {label}：{'，'.join(parts)}", flush=True)
    if _as_int(failed_jobs) or follow_up_errors:
        print("[ann] 提醒：有窗口本次未抓完，后续审计会保留为待抓项，下次运行会继续重试。", flush=True)

    summary_path = outputs.get("summary") if outputs else None
    if summary_path is not None:
        print(f"[ann] 摘要：{summary_path}", flush=True)


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


if __name__ == "__main__":
    raise SystemExit(main())
