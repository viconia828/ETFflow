"""Build ETF flow dashboards for every trading day in a requested date range."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys
import threading
import time

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.cli import (  # noqa: E402
    DAILY_REPORT_COLUMNS,
    SHARE_REPORT_COLUMNS,
    _apply_lifecycle_flow_adjustments,
    _cache_coverage_error,
    _flow_stats,
    _load_category_metadata,
    _open_trade_dates,
    _report_history_start_date,
    _source_frame_stats,
)
from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import apply_category_map, category_map_codes, category_map_stats, load_category_map  # noqa: E402
from etf_flow_monitor.data.cross_border_fill import fill_cross_border_previous_values  # noqa: E402
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.monitor.flow_metrics import (  # noqa: E402
    TUSHARE_DAILY_AMOUNT_UNIT_MULTIPLIER,
    TUSHARE_FD_SHARE_UNIT_MULTIPLIER,
    build_flow_snapshot,
    build_market_summary,
    select_alert_rows,
)
from etf_flow_monitor.monitor.html_dashboard import write_dashboard_html  # noqa: E402
from etf_flow_monitor.monitor.report import write_markdown_report  # noqa: E402
from etf_flow_monitor.run_ledger import RunLedger, make_log_dir  # noqa: E402
from etf_flow_monitor.utils.calendar import normalize_date_input, trading_calendar_from_frame  # noqa: E402
from etf_flow_monitor.utils.io import format_tushare_date  # noqa: E402
from etf_flow_monitor.utils.logger import configure_logging  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build ETF flow dashboards for a trading-date range.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--start-date", required=True, help="Range start YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, help="Range end YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--cache-only", action="store_true", help="Use local cache only and never fetch remote source rows.")
    parser.add_argument("--refresh", action="store_true", help="Refresh source rows when remote fetching is enabled.")
    parser.add_argument("--heartbeat-seconds", type=int, default=10)
    parser.add_argument(
        "--detail-output",
        choices=("today", "full"),
        default="today",
        help="CSV flow_snapshot detail output. 'today' writes only report-day rows; dashboard still uses full history.",
    )
    return parser


@dataclass
class _HeartbeatState:
    total: int
    done: int = 0
    stage: str = "starting"
    trade_date: str = ""
    started_at: float = 0.0


class _Heartbeat:
    def __init__(self, seconds: int, state: _HeartbeatState) -> None:
        self.seconds = max(int(seconds), 1)
        self.state = state
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.state.started_at = time.monotonic()
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop_event.wait(self.seconds):
            elapsed = int(time.monotonic() - self.state.started_at)
            print(
                f"[range heartbeat] stage={self.state.stage} "
                f"done={self.state.done}/{self.state.total} current={self.state.trade_date or '-'} elapsed={elapsed}s",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    configure_logging("INFO")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    log_dir = make_log_dir(config.output_dir, prefix="flow_monitor_range")
    ledger = RunLedger(log_dir=log_dir, argv=raw_argv, config_path=config_path)
    run_stats: dict[str, object] = {
        "mode": "range",
        "detail_output": args.detail_output,
        "daily_read_columns": DAILY_REPORT_COLUMNS,
        "share_read_columns": SHARE_REPORT_COLUMNS,
    }

    try:
        requested_start = pd.Timestamp(normalize_date_input(args.start_date, field_name="start_date")).normalize()
        requested_end = pd.Timestamp(normalize_date_input(args.end_date, field_name="end_date")).normalize()
        if requested_end < requested_start:
            requested_start, requested_end = requested_end, requested_start

        source = (
            TushareEtfSource.from_cache(cache_dir=config.cache_dir)
            if args.cache_only
            else TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
        )
        calendar_start = requested_start - pd.DateOffset(months=13) - pd.Timedelta(days=45)
        calendar_end = requested_end + pd.Timedelta(days=10)
        calendar_frame = source.get_calendar(
            calendar_start,
            calendar_end,
            exchange=config.calendar_exchange,
            refresh=args.refresh and not args.cache_only,
            cache_only=args.cache_only,
        )
        if calendar_frame.empty:
            return _finish(ledger, run_stats, 1, "failed", "本地官方交易日历不足，无法生成区间报表。")
        calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
        report_dates = _unique_timestamps(calendar.get_trading_days(requested_start.date(), requested_end.date()))
        if not report_dates:
            return _finish(ledger, run_stats, 1, "failed", "输入区间内没有交易日。")

        history_starts = {trade_date: _report_history_start_date(calendar, trade_date) for trade_date in report_dates}
        source_start = min(history_starts.values())
        source_end = max(report_dates)
        source_trade_dates = _open_trade_dates(calendar_frame, source_start, source_end)
        naive_source_date_reads = sum(
            len(_open_trade_dates(calendar_frame, history_starts[trade_date], trade_date)) for trade_date in report_dates
        )
        run_stats.update(
            {
                "requested_start": requested_start.strftime("%Y-%m-%d"),
                "requested_end": requested_end.strftime("%Y-%m-%d"),
                "report_trade_dates": int(len(report_dates)),
                "source_start": source_start.strftime("%Y-%m-%d"),
                "source_end": source_end.strftime("%Y-%m-%d"),
                "source_trade_dates": int(len(source_trade_dates)),
                "naive_cross_section_date_reads": int(naive_source_date_reads * 2),
                "batched_cross_section_date_reads": int(len(source_trade_dates) * 2),
                "estimated_cross_section_date_read_savings": int(max(naive_source_date_reads - len(source_trade_dates), 0) * 2),
            }
        )
        _print_preflight(run_stats)
        ledger.progress("preflight", f"reports={len(report_dates)} source_dates={len(source_trade_dates)}", stats=run_stats)

        if args.cache_only:
            coverage_message = _cache_coverage_error(config.cache_dir, source.source_name, source_trade_dates, source_end)
            if coverage_message:
                return _finish(ledger, run_stats, 1, "failed", coverage_message)

        category_map = load_category_map(config.category_map_path)
        category_metadata = _load_category_metadata(config.category_map_path)
        category_codes = category_map_codes(category_map)
        basic = source.get_etf_basic(market=config.etf_market, refresh=args.refresh and not args.cache_only, cache_only=args.cache_only)
        if basic.empty:
            return _finish(ledger, run_stats, 1, "failed", "本地 ETF 基础信息缓存为空，无法生成区间报表。")
        basic_codes = basic["fund_code"].dropna().astype(str).str.upper().drop_duplicates().tolist()
        codes = [code for code in category_codes if code in set(basic_codes)] if category_codes else basic_codes
        run_stats.update(
            {
                "category_map_rows": int(len(category_map)),
                "fund_basic_rows": int(len(basic)),
                "fund_basic_funds": int(len(basic_codes)),
                "monitor_universe_funds": int(len(codes)),
                "category_map_codes_missing_in_basic": int(len(set(category_codes) - set(basic_codes))),
            }
        )

        daily, shares = _load_source_frames_with_progress(
            source=source,
            codes=codes,
            source_trade_dates=source_trade_dates,
            refresh=args.refresh,
            cache_only=args.cache_only,
            heartbeat_seconds=args.heartbeat_seconds,
            ledger=ledger,
        )
        daily, shares, flow_all, summary_all = _precompute_metrics_with_progress(
            daily=daily,
            shares=shares,
            basic=basic,
            category_map=category_map,
            config=config,
            run_stats=run_stats,
            heartbeat_seconds=args.heartbeat_seconds,
            ledger=ledger,
            source_trade_dates=source_trade_dates,
        )
        run_stats.update(
            _source_frame_stats(
                daily=daily,
                shares=shares,
                requested_codes=codes,
                trade_date=source_end,
                listing_metadata_frames=[basic, category_metadata],
            )
        )
        run_stats.update(source.cache.snapshot_stats() if source.cache is not None else {})
        run_stats.update(
            {
                "flow_estimate_unit": "yuan",
                "share_field_unit": "tushare_fd_share_10k_shares",
                "share_unit_multiplier": int(TUSHARE_FD_SHARE_UNIT_MULTIPLIER),
                "amount_unit_multiplier": int(TUSHARE_DAILY_AMOUNT_UNIT_MULTIPLIER),
                "amount_unit": "yuan",
                "source_amount_unit": "tushare_fund_daily_amount_1000_yuan",
                "flow_price_source": "fund_daily_close; money_fund_quote_gt_10_divided_by_100",
            }
        )
        run_stats.update(category_map_stats(flow_all))
        run_stats.update(_flow_stats(flow_all))

        state = _HeartbeatState(total=len(report_dates), stage="render_reports")
        heartbeat = _Heartbeat(args.heartbeat_seconds, state)
        generated: list[str] = []
        flow_snapshot_rows_written = 0
        heartbeat.start()
        try:
            for trade_date in report_dates:
                state.trade_date = format_tushare_date(trade_date)
                ledger.progress("render_report", state.trade_date, done=state.done, total=state.total)
                rows_written = _write_one_report(
                    config=config,
                    trade_date=trade_date,
                    history_start=history_starts[trade_date],
                    flow_all=flow_all,
                    summary_all=summary_all,
                    detail_output=args.detail_output,
                )
                flow_snapshot_rows_written += rows_written
                state.done += 1
                generated.append(format_tushare_date(trade_date))
                print(f"[range] generated {state.done}/{state.total}: {state.trade_date}", flush=True)
        finally:
            heartbeat.stop()

        run_stats["generated_reports"] = int(len(generated))
        run_stats["generated_first"] = generated[0] if generated else ""
        run_stats["generated_last"] = generated[-1] if generated else ""
        run_stats["flow_snapshot_rows_written"] = int(flow_snapshot_rows_written)
        ledger.record_stats(run_stats)
        ledger.record_outputs(
            report_root=str(config.output_dir / "flow_monitor"),
            first_report=str(config.output_dir / "flow_monitor" / generated[0]) if generated else "",
            last_report=str(config.output_dir / "flow_monitor" / generated[-1]) if generated else "",
        )
        ledger.finish(exit_code=0, status="success")
        print(f"[range] finished: generated={len(generated)} first={generated[0]} last={generated[-1]}", flush=True)
        print(f"[range] run_ledger={ledger.path}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        ledger.finish(exit_code=1, status="failed", error=str(exc))
        print(f"[range] failed: {exc}", file=sys.stderr, flush=True)
        return 1


def _load_source_frames_with_progress(
    *,
    source: TushareEtfSource,
    codes: list[str],
    source_trade_dates: list[pd.Timestamp],
    refresh: bool,
    cache_only: bool,
    heartbeat_seconds: int,
    ledger: RunLedger,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    state = _HeartbeatState(total=max(len(source_trade_dates) * 2, 1), stage="load_fund_daily")
    heartbeat = _Heartbeat(heartbeat_seconds, state)
    daily_frames: list[pd.DataFrame] = []
    share_frames: list[pd.DataFrame] = []
    print(
        f"[range] preprocessing: loading source frames dates={len(source_trade_dates)} heartbeat={heartbeat_seconds}s",
        flush=True,
    )
    ledger.progress("load_source_frames", f"source_dates={len(source_trade_dates)}", done=0, total=state.total)
    heartbeat.start()
    try:
        for trade_date in source_trade_dates:
            key = format_tushare_date(trade_date)
            state.stage = "load_fund_daily"
            state.trade_date = key
            daily_frames.append(
                source.get_etf_daily_by_trade_dates(
                    codes,
                    [trade_date],
                    refresh=refresh,
                    cache_only=cache_only,
                    columns=DAILY_REPORT_COLUMNS,
                )
            )
            state.done += 1
            _record_periodic_progress(ledger, state)
        print(f"[range] preprocessing: fund_daily loaded {len(source_trade_dates)} dates", flush=True)
        for trade_date in source_trade_dates:
            key = format_tushare_date(trade_date)
            state.stage = "load_fund_share"
            state.trade_date = key
            share_frames.append(
                source.get_etf_share_by_trade_dates(
                    codes,
                    [trade_date],
                    refresh=refresh,
                    cache_only=cache_only,
                    columns=SHARE_REPORT_COLUMNS,
                )
            )
            state.done += 1
            _record_periodic_progress(ledger, state)
        print(f"[range] preprocessing: fund_share loaded {len(source_trade_dates)} dates", flush=True)
    finally:
        heartbeat.stop()
    return _concat_frames(daily_frames), _concat_frames(share_frames)


def _precompute_metrics_with_progress(
    *,
    daily: pd.DataFrame,
    shares: pd.DataFrame,
    basic: pd.DataFrame,
    category_map: pd.DataFrame,
    config,
    run_stats: dict[str, object],
    heartbeat_seconds: int,
    ledger: RunLedger,
    source_trade_dates: list[pd.Timestamp],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    state = _HeartbeatState(total=5, stage="cross_border_fill")
    heartbeat = _Heartbeat(heartbeat_seconds, state)
    print(f"[range] preprocessing: computing metrics heartbeat={heartbeat_seconds}s", flush=True)
    heartbeat.start()
    try:
        ledger.progress("cross_border_fill", done=state.done, total=state.total)
        daily, shares, fill_stats = fill_cross_border_previous_values(daily, shares, category_map, source_trade_dates)
        run_stats.update(fill_stats)
        state.done = 1

        state.stage = "build_flow_snapshot"
        ledger.progress("build_flow_snapshot", done=state.done, total=state.total)
        flow_all = build_flow_snapshot(daily, shares, basic)
        state.done = 2

        state.stage = "apply_lifecycle_adjustments"
        ledger.progress("apply_lifecycle_adjustments", done=state.done, total=state.total)
        flow_all = _apply_lifecycle_flow_adjustments(flow_all, config.lifecycle_flow_adjustments_path, run_stats)
        state.done = 3

        state.stage = "apply_category_map"
        ledger.progress("apply_category_map", done=state.done, total=state.total)
        flow_all = apply_category_map(flow_all, category_map)
        state.done = 4

        state.stage = "build_market_summary"
        ledger.progress("build_market_summary", done=state.done, total=state.total)
        summary_all = build_market_summary(flow_all)
        state.done = 5
    finally:
        heartbeat.stop()
    print("[range] preprocessing: metrics ready", flush=True)
    return daily, shares, flow_all, summary_all


def _record_periodic_progress(ledger: RunLedger, state: _HeartbeatState) -> None:
    if state.done == state.total or state.done % 25 == 0:
        ledger.progress(state.stage, state.trade_date, done=state.done, total=state.total)


def _concat_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in frames if frame is not None]
    if not valid:
        return pd.DataFrame()
    return pd.concat(valid, ignore_index=True)


def _write_one_report(
    *,
    config,
    trade_date: pd.Timestamp,
    history_start: pd.Timestamp,
    flow_all: pd.DataFrame,
    summary_all: pd.DataFrame,
    detail_output: str,
) -> int:
    flow_window = _date_window(flow_all, "trade_date", history_start, trade_date)
    summary_window = _date_window(summary_all, "trade_date", history_start, trade_date)
    flow_today = _exact_date(flow_window, "trade_date", trade_date)
    alerts = select_alert_rows(
        flow_today,
        min_amount=config.min_amount_for_alert,
        min_abs_flow=config.min_abs_flow_for_alert,
        max_rows=config.max_report_rows,
    )
    output_dir = config.output_dir / "flow_monitor" / format_tushare_date(trade_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    flow_path = output_dir / "etf_flow_snapshot.csv"
    summary_path = output_dir / "market_summary.csv"
    report_path = output_dir / "etf_flow_report.md"
    dashboard_path = output_dir / "etf_flow_dashboard.html"
    flow_to_write = flow_window if detail_output == "full" else flow_today
    flow_to_write.to_csv(flow_path, index=False, encoding="utf-8-sig")
    summary_window.to_csv(summary_path, index=False, encoding="utf-8-sig")
    write_markdown_report(
        report_path,
        title=f"ETF Flow Monitor {trade_date.date()}",
        summary=summary_window.tail(10),
        alerts=alerts,
        notes=[
            "Flow estimate = Tushare fd_share change (10k-share units) × 10,000 × flow price.",
            "For money-market ETFs quoted near 100, flow price uses close / 100; other ETFs use on-exchange close.",
            "Tushare fund_daily.amount is normalized from thousand-yuan units to yuan.",
            "Range mode writes only report-day rows to etf_flow_snapshot.csv by default; dashboard uses full history.",
        ],
    )
    write_dashboard_html(
        dashboard_path,
        trade_date=trade_date,
        summary=summary_window,
        flow=flow_window,
        notes=[
            "金额 = Tushare fd_share 逐日变化（万份）× 10,000 × 资金流价格；普通 ETF 使用场内收盘价，100 元附近报价的货币 ETF 使用收盘价 / 100。",
            "成交额 = Tushare fund_daily.amount × 1,000，统一换算为元后再在页面显示为亿元。",
        ],
    )
    return int(len(flow_to_write))


def _date_window(frame: pd.DataFrame, column: str, start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.DataFrame() if frame is None else frame.copy()
    dates = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    start_key = pd.Timestamp(start_date).normalize()
    end_key = pd.Timestamp(end_date).normalize()
    return frame.loc[dates.ge(start_key) & dates.le(end_key)].copy().reset_index(drop=True)


def _exact_date(frame: pd.DataFrame, column: str, trade_date: pd.Timestamp) -> pd.DataFrame:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.DataFrame() if frame is None else frame.copy()
    dates = pd.to_datetime(frame[column], errors="coerce").dt.normalize()
    return frame.loc[dates.eq(pd.Timestamp(trade_date).normalize())].copy().reset_index(drop=True)


def _unique_timestamps(values: list[object]) -> list[pd.Timestamp]:
    ordered: list[pd.Timestamp] = []
    seen: set[str] = set()
    for value in values:
        timestamp = pd.Timestamp(value).normalize()
        key = timestamp.strftime("%Y%m%d")
        if key not in seen:
            seen.add(key)
            ordered.append(timestamp)
    return ordered


def _print_preflight(stats: dict[str, object]) -> None:
    print("[range] P2 preflight:", flush=True)
    print(
        f"[range] reports={stats['report_trade_dates']} source_dates={stats['source_trade_dates']} "
        f"source_window={stats['source_start']}..{stats['source_end']}",
        flush=True,
    )
    print(
        f"[range] aggregate reuse: naive_date_reads={stats['naive_cross_section_date_reads']} "
        f"batched_date_reads={stats['batched_cross_section_date_reads']} "
        f"estimated_saved={stats['estimated_cross_section_date_read_savings']}",
        flush=True,
    )
    print(
        f"[range] usecols: daily={','.join(DAILY_REPORT_COLUMNS)} share={','.join(SHARE_REPORT_COLUMNS)}",
        flush=True,
    )
    print(
        f"[range] detail_output={stats['detail_output']} "
        "(today=CSV only report-day rows; dashboard still full 12M history)",
        flush=True,
    )


def _finish(ledger: RunLedger, stats: dict[str, object], exit_code: int, status: str, message: str) -> int:
    payload = dict(stats)
    payload["message"] = message
    ledger.record_stats(payload)
    ledger.finish(exit_code=exit_code, status=status, error=message if exit_code else "")
    print(f"[range] {message}", flush=True)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
