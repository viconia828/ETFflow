"""Command-line entrypoint for the ETF flow monitor starter."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

from etf_flow_monitor.config import load_config
from etf_flow_monitor.data.category_map import apply_category_map, category_map_codes, category_map_stats, load_category_map
from etf_flow_monitor.data.cross_border_fill import fill_cross_border_previous_values
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource
from etf_flow_monitor.monitor.flow_metrics import (
    TUSHARE_DAILY_AMOUNT_UNIT_MULTIPLIER,
    TUSHARE_FD_SHARE_UNIT_MULTIPLIER,
    build_flow_snapshot,
    build_market_summary,
    select_alert_rows,
)
from etf_flow_monitor.monitor.html_dashboard import write_dashboard_html
from etf_flow_monitor.monitor.report import write_markdown_report
from etf_flow_monitor.run_ledger import RunLedger, make_log_dir
from etf_flow_monitor.utils.calendar import current_shanghai_date, normalize_date_input, trading_calendar_from_frame
from etf_flow_monitor.utils.io import format_tushare_date
from etf_flow_monitor.utils.logger import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Whole-market ETF flow monitor starter.")
    parser.add_argument("--config", default="config.example.txt")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--dry-run", action="store_true", help="Create run ledger and report from empty frames without remote fetch.")
    parser.add_argument("--refresh", action="store_true", help="Bypass existing caches for source reads.")
    parser.add_argument("--cache-only", action="store_true", help="Use local cache only and never fetch remote source rows.")
    return parser


def _parse_requested_date(value: str | None) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    return pd.Timestamp(normalize_date_input(text, field_name="trade_date")).normalize()


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)
    configure_logging("INFO")
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    log_dir = make_log_dir(config.output_dir)
    ledger = RunLedger(log_dir=log_dir, argv=raw_argv, config_path=config_path)
    run_stats: dict[str, object] = {}
    category_map = load_category_map(config.category_map_path)
    category_codes = category_map_codes(category_map)
    run_stats["category_map_rows"] = int(len(category_map))

    try:
        explicit_trade_date = bool(str(args.trade_date or "").strip())
        requested_date = _parse_requested_date(args.trade_date) or pd.Timestamp(current_shanghai_date()).normalize()
        calendar_payload: dict[str, str | bool] = {
            "request_date": requested_date.strftime("%Y-%m-%d"),
            "market_date": requested_date.strftime("%Y-%m-%d"),
            "calendar_mode": "dry_run_no_calendar" if args.dry_run else "official_calendar_pending",
            "mapped": False,
        }

        if args.dry_run:
            trade_date = requested_date
        else:
            source = (
                TushareEtfSource.from_cache(cache_dir=config.cache_dir)
                if args.cache_only
                else TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(Path.cwd())])
            )
            cache_interval = _complete_cross_section_cache_interval(config.cache_dir, source.source_name)
            use_latest_local_cache_date = False
            if args.cache_only and not explicit_trade_date:
                if not cache_interval["latest"]:
                    return _finish_skipped(
                        ledger,
                        run_stats,
                        "本地缓存为空，无法在 cache-only 模式生成报表。",
                    )
                requested_date = _parse_cache_date(cache_interval["latest"])
                use_latest_local_cache_date = True
            calendar_start = requested_date - pd.Timedelta(days=max(config.lookback_days + 30, 450))
            calendar_end = requested_date + pd.Timedelta(days=10)
            calendar_frame = source.get_calendar(
                calendar_start,
                calendar_end,
                exchange=config.calendar_exchange,
                refresh=args.refresh and not args.cache_only,
                cache_only=args.cache_only,
            )
            if calendar_frame.empty:
                return _finish_skipped(
                    ledger,
                    run_stats,
                    f"本地官方交易日历不足，无法解析 {requested_date.date()} 的市场日期。",
                )
            calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
            if explicit_trade_date:
                market_date = pd.Timestamp(calendar.resolve_request_date_market_date(requested_date.date())).normalize()
                calendar_mode = "explicit_request_date"
            elif use_latest_local_cache_date:
                market_date = pd.Timestamp(calendar.resolve_request_date_market_date(requested_date.date())).normalize()
                calendar_mode = "local_cache_latest_market_date"
            else:
                market_date = pd.Timestamp(calendar.resolve_market_date(requested_date.date())).normalize()
                calendar_mode = "latest_available_market_date"
            trade_date = market_date
            calendar_payload = {
                "request_date": requested_date.strftime("%Y-%m-%d"),
                "market_date": trade_date.strftime("%Y-%m-%d"),
                "calendar_mode": calendar_mode,
                "exchange": config.calendar_exchange,
                "mapped": bool(trade_date != requested_date),
            }

        start_date = trade_date - pd.Timedelta(days=max(config.lookback_days, 20))
        ledger.progress("load_inputs", f"trade_date={trade_date.date()}", calendar=calendar_payload)

        if args.dry_run:
            basic = pd.DataFrame()
            daily = pd.DataFrame()
            shares = pd.DataFrame()
        else:
            fetch_trade_dates = _open_trade_dates(calendar_frame, start_date, trade_date)
            if args.cache_only:
                coverage_message = _cache_coverage_error(config.cache_dir, source.source_name, fetch_trade_dates, trade_date)
                if coverage_message:
                    return _finish_skipped(ledger, run_stats, coverage_message)
            basic = source.get_etf_basic(market=config.etf_market, refresh=args.refresh and not args.cache_only, cache_only=args.cache_only)
            if basic.empty:
                return _finish_skipped(ledger, run_stats, "本地 ETF 基础信息缓存为空，无法生成报表。")
            basic_codes = basic["fund_code"].dropna().astype(str).str.upper().drop_duplicates().tolist()
            codes = [code for code in category_codes if code in set(basic_codes)] if category_codes else basic_codes
            run_stats.update(
                {
                    "fund_basic_rows": int(len(basic)),
                    "fund_basic_funds": int(len(basic_codes)),
                    "monitor_universe_funds": int(len(codes)),
                    "category_map_codes_missing_in_basic": int(len(set(category_codes) - set(basic_codes))),
                }
            )
            run_stats["fetch_shape"] = "daily_cross_section"
            run_stats["fetch_trade_dates"] = int(len(fetch_trade_dates))
            try:
                daily = source.get_etf_daily_by_trade_dates(codes, fetch_trade_dates, refresh=args.refresh, cache_only=args.cache_only)
            except Exception as exc:  # noqa: BLE001
                if args.cache_only:
                    return _finish_skipped(ledger, run_stats, f"本地行情缓存读取失败：{exc}")
                ledger.progress("daily_cross_section_unavailable", str(exc))
                run_stats["fetch_shape"] = "per_code_time_series_fallback"
                daily = source.get_etf_daily(codes, start_date, trade_date, refresh=args.refresh)
            try:
                if run_stats.get("fetch_shape") == "daily_cross_section":
                    try:
                        shares = source.get_etf_share_by_trade_dates(codes, fetch_trade_dates, refresh=args.refresh, cache_only=args.cache_only)
                    except Exception as exc:  # noqa: BLE001
                        if args.cache_only:
                            return _finish_skipped(ledger, run_stats, f"本地份额缓存读取失败：{exc}")
                        ledger.progress("share_cross_section_unavailable", str(exc))
                        shares = source.get_etf_share(codes, start_date, trade_date, refresh=args.refresh)
                else:
                    shares = source.get_etf_share(codes, start_date, trade_date, refresh=args.refresh)
            except Exception as exc:  # noqa: BLE001
                ledger.progress("share_data_unavailable", str(exc))
                shares = pd.DataFrame()
            daily, shares, fill_stats = fill_cross_border_previous_values(daily, shares, category_map, fetch_trade_dates)
            run_stats.update(fill_stats)
            run_stats.update(_source_frame_stats(daily=daily, shares=shares, requested_codes=codes))
            run_stats.update(source.cache.snapshot_stats() if source.cache is not None else {})

        ledger.progress("build_metrics")
        flow = build_flow_snapshot(daily, shares, basic)
        flow = apply_category_map(flow, category_map)
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
        run_stats.update(category_map_stats(flow))
        run_stats.update(_flow_stats(flow))
        flow_today = flow.loc[pd.to_datetime(flow.get("trade_date"), errors="coerce").eq(trade_date)].copy() if not flow.empty else flow
        summary = build_market_summary(flow)
        alerts = select_alert_rows(
            flow_today,
            min_amount=config.min_amount_for_alert,
            min_abs_flow=config.min_abs_flow_for_alert,
            max_rows=config.max_report_rows,
        )

        output_dir = config.output_dir / "flow_monitor" / trade_date.strftime("%Y%m%d")
        output_dir.mkdir(parents=True, exist_ok=True)
        flow_path = output_dir / "etf_flow_snapshot.csv"
        summary_path = output_dir / "market_summary.csv"
        report_path = output_dir / "etf_flow_report.md"
        dashboard_path = output_dir / "etf_flow_dashboard.html"
        flow.to_csv(flow_path, index=False, encoding="utf-8-sig")
        summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
        write_markdown_report(
            report_path,
            title=f"ETF Flow Monitor {trade_date.date()}",
            summary=summary.tail(10),
            alerts=alerts,
            notes=[
                "Flow estimate = Tushare fd_share change (10k-share units) × 10,000 × flow price.",
                "For money-market ETFs quoted near 100, flow price uses close / 100; other ETFs use on-exchange close.",
                "Tushare fund_daily.amount is normalized from thousand-yuan units to yuan.",
                "When share data is unavailable, the report still ranks activity by amount but net flow is zero.",
            ],
        )
        write_dashboard_html(
            dashboard_path,
            trade_date=trade_date,
            summary=summary,
            flow=flow,
            notes=[
                "金额 = Tushare fd_share 逐日变化（万份）× 10,000 × 资金流价格；普通 ETF 使用场内收盘价，100 元附近报价的货币 ETF 使用收盘价 / 100。",
                "成交额 = Tushare fund_daily.amount × 1,000，统一换算为元后再在页面显示为亿元。",
                "本文件为单日静态快照，样式、数据和脚本均已内嵌，可直接转发分享。",
            ],
        )
        ledger.record_stats(run_stats)
        ledger.record_outputs(flow_snapshot=str(flow_path), market_summary=str(summary_path), report=str(report_path), dashboard=str(dashboard_path))
        ledger.finish(exit_code=0, status="success")
        print(f"report: {report_path}", flush=True)
        print(f"dashboard: {dashboard_path}", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        ledger.finish(exit_code=1, status="failed", error=str(exc))
        print(f"ETF flow monitor failed: {exc}", file=sys.stderr, flush=True)
        return 1

def _open_trade_dates(calendar_frame: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp) -> list[pd.Timestamp]:
    if calendar_frame is None or calendar_frame.empty:
        raise RuntimeError("Official trading calendar is empty; cannot build fetch date list.")
    required = {"cal_date", "is_open"}
    missing = required - set(calendar_frame.columns)
    if missing:
        raise RuntimeError(f"Official trading calendar missing columns: {', '.join(sorted(missing))}")
    working = calendar_frame.copy()
    working["cal_date"] = pd.to_datetime(working["cal_date"], errors="coerce").dt.normalize()
    working["is_open"] = pd.to_numeric(working["is_open"], errors="coerce").fillna(0).astype(int)
    start_key = pd.Timestamp(start_date).normalize()
    end_key = pd.Timestamp(end_date).normalize()
    mask = working["cal_date"].ge(start_key) & working["cal_date"].le(end_key) & working["is_open"].eq(1)
    dates = working.loc[mask, "cal_date"].dropna().drop_duplicates().sort_values(kind="stable").tolist()
    if not dates:
        raise RuntimeError(f"Official trading calendar has no open dates between {start_key.date()} and {end_key.date()}.")
    return [pd.Timestamp(value).normalize() for value in dates]


def _cache_coverage_error(cache_dir: Path, source_name: str, trade_dates: list[pd.Timestamp], trade_date: pd.Timestamp) -> str:
    daily_missing = _missing_cross_section_keys(cache_dir, source_name, "etf_daily", trade_dates)
    share_missing = _missing_cross_section_keys(cache_dir, source_name, "etf_share", trade_dates)
    if not daily_missing and not share_missing:
        return ""
    interval = _complete_cross_section_cache_interval(cache_dir, source_name)
    interval_text = _format_cache_interval(interval)
    target_key = format_tushare_date(trade_date)
    return (
        f"本地完整缓存区间为 {interval_text}，无法生成 {target_key} 报表："
        f"统计区间缺行情缓存 {len(daily_missing)} 个交易日、份额缓存 {len(share_missing)} 个交易日。"
    )


def _complete_cross_section_cache_interval(cache_dir: Path, source_name: str) -> dict[str, str]:
    daily_keys = set(_cached_cross_section_keys(cache_dir, source_name, "etf_daily"))
    share_keys = set(_cached_cross_section_keys(cache_dir, source_name, "etf_share"))
    complete_keys = sorted(daily_keys & share_keys)
    return {
        "earliest": complete_keys[0] if complete_keys else "",
        "latest": complete_keys[-1] if complete_keys else "",
        "daily_latest": max(daily_keys) if daily_keys else "",
        "share_latest": max(share_keys) if share_keys else "",
    }


def _cached_cross_section_keys(cache_dir: Path, source_name: str, dataset_name: str) -> list[str]:
    directory = cache_dir / source_name / "daily_cross_section" / dataset_name
    if not directory.exists():
        return []
    return sorted(path.stem for path in directory.glob("*.csv") if len(path.stem) == 8 and path.stem.isdigit())


def _missing_cross_section_keys(cache_dir: Path, source_name: str, dataset_name: str, trade_dates: list[pd.Timestamp]) -> list[str]:
    cached = set(_cached_cross_section_keys(cache_dir, source_name, dataset_name))
    required = [format_tushare_date(trade_date) for trade_date in trade_dates]
    return [trade_date for trade_date in required if trade_date not in cached]


def _parse_cache_date(value: str) -> pd.Timestamp:
    return pd.to_datetime(str(value), format="%Y%m%d", errors="raise").normalize()


def _format_cache_interval(interval: dict[str, str]) -> str:
    earliest = interval.get("earliest", "")
    latest = interval.get("latest", "")
    if not earliest or not latest:
        daily_latest = interval.get("daily_latest", "") or "none"
        share_latest = interval.get("share_latest", "") or "none"
        return f"none（daily 截至 {daily_latest}，share 截至 {share_latest}）"
    return f"{earliest} 至 {latest}"


def _finish_skipped(ledger: RunLedger, stats: dict[str, object], message: str) -> int:
    payload = dict(stats)
    payload["skip_reason"] = message
    ledger.record_stats(payload)
    ledger.finish(exit_code=0, status="skipped")
    print(f"[report] {message}", flush=True)
    return 0


def _source_frame_stats(*, daily: pd.DataFrame, shares: pd.DataFrame, requested_codes: list[str]) -> dict[str, int]:
    requested_count = len(set(str(code).upper() for code in requested_codes))
    daily_funds = _nunique_code(daily)
    share_funds = _nunique_code(shares)
    return {
        "fund_daily_rows": int(len(daily)),
        "fund_daily_funds": daily_funds,
        "fund_daily_missing_funds": max(requested_count - daily_funds, 0),
        "fund_share_rows": int(len(shares)),
        "fund_share_funds": share_funds,
        "fund_share_missing_funds": max(requested_count - share_funds, 0),
    }


def _flow_stats(flow: pd.DataFrame) -> dict[str, int]:
    return {
        "flow_rows": int(len(flow)),
        "flow_funds": _nunique_code(flow),
    }


def _nunique_code(frame: pd.DataFrame) -> int:
    if frame is None or frame.empty or "fund_code" not in frame.columns:
        return 0
    return int(frame["fund_code"].dropna().astype(str).str.upper().nunique())


if __name__ == "__main__":
    raise SystemExit(main())
