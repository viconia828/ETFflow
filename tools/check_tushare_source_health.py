"""Run a small Tushare source health check for ETF monitor inputs."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import load_category_map  # noqa: E402
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.utils.calendar import current_shanghai_date, normalize_date_input, resolve_monitor_market_date, trading_calendar_from_frame  # noqa: E402
from etf_flow_monitor.utils.io import write_json  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Tushare ETF source availability and key fields.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", default="")
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--refresh", action="store_true", help="Bypass caches for checked source reads.")
    parser.add_argument("--output", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
    requested_date = _parse_requested_date(args.trade_date) or pd.Timestamp(current_shanghai_date()).normalize()
    output_path = _output_path(args.output)

    payload: dict[str, Any] = {
        "schema_version": "tushare_source_health_v1",
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config_path": str(config_path),
        "request_date": requested_date.strftime("%Y-%m-%d"),
        "checks": {},
        "sample_codes": [],
    }
    exit_code = 0

    try:
        calendar_frame = source.get_calendar(
            requested_date - pd.Timedelta(days=450),
            requested_date + pd.Timedelta(days=10),
            exchange=config.calendar_exchange,
            refresh=args.refresh,
        )
        calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
        resolved_date, calendar_mode = resolve_monitor_market_date(
            calendar,
            requested_date.date(),
            explicit_request=bool(str(args.trade_date or "").strip()),
        )
        trade_date = pd.Timestamp(resolved_date).normalize()
        payload["market_date"] = trade_date.strftime("%Y-%m-%d")
        payload["calendar_mode"] = calendar_mode
        payload["checks"]["trade_cal"] = _ok(rows=len(calendar_frame), message="official calendar loaded")
    except Exception as exc:  # noqa: BLE001
        payload["market_date"] = ""
        payload["checks"]["trade_cal"] = _fail(exc)
        trade_date = requested_date
        exit_code = 1

    try:
        basic = source.get_etf_basic(market=config.etf_market, refresh=args.refresh)
        payload["checks"]["fund_basic"] = _ok(rows=len(basic), message="fund_basic loaded")
    except Exception as exc:  # noqa: BLE001
        basic = pd.DataFrame()
        payload["checks"]["fund_basic"] = _fail(exc)
        exit_code = 1

    sample_codes = _sample_codes(config.category_map_path, basic, max(args.sample_size, 1))
    payload["sample_codes"] = sample_codes
    start_date = trade_date - pd.Timedelta(days=10)

    try:
        daily = source.get_etf_daily(sample_codes, start_date, trade_date, refresh=args.refresh)
        payload["checks"]["fund_daily"] = _ok(
            rows=len(daily),
            message="fund_daily loaded",
            latest_trade_date=_latest_date_text(daily, "trade_date"),
            non_null_amount_rows=int(pd.to_numeric(daily.get("amount"), errors="coerce").notna().sum()) if not daily.empty else 0,
        )
    except Exception as exc:  # noqa: BLE001
        payload["checks"]["fund_daily"] = _fail(exc)
        exit_code = 1

    try:
        shares = source.get_etf_share(sample_codes, start_date, trade_date, refresh=args.refresh)
        share_values = pd.to_numeric(shares.get("shares"), errors="coerce") if not shares.empty else pd.Series(dtype="float64")
        payload["checks"]["fund_share"] = _ok(
            rows=len(shares),
            message="fund_share loaded",
            latest_trade_date=_latest_date_text(shares, "trade_date"),
            min_shares=float(share_values.min()) if not share_values.empty else None,
            max_shares=float(share_values.max()) if not share_values.empty else None,
        )
    except Exception as exc:  # noqa: BLE001
        payload["checks"]["fund_share"] = _fail(exc)
        exit_code = 1

    payload["cache_stats"] = source.cache.snapshot_stats() if source.cache is not None else {}
    payload["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["status"] = "success" if exit_code == 0 else "failed"
    write_json(output_path, payload)
    print(f"status: {payload['status']}")
    print(f"output: {output_path}")
    return exit_code


def _sample_codes(category_map_path: Path, basic: pd.DataFrame, sample_size: int) -> list[str]:
    category_map = load_category_map(category_map_path)
    if not category_map.empty:
        codes = category_map["fund_code"].dropna().astype(str).str.upper().drop_duplicates().head(sample_size).tolist()
        if codes:
            return codes
    if basic is None or basic.empty or "fund_code" not in basic.columns:
        return []
    return basic["fund_code"].dropna().astype(str).str.upper().drop_duplicates().head(sample_size).tolist()


def _parse_requested_date(value: str | None) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    return pd.Timestamp(normalize_date_input(text, field_name="trade_date")).normalize()


def _output_path(raw_path: str) -> Path:
    if raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else PROJECT_ROOT / path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "outputs" / "source_health" / f"tushare_source_health_{stamp}.json"


def _ok(**payload: Any) -> dict[str, Any]:
    return {"status": "ok", **payload}


def _fail(exc: Exception) -> dict[str, str]:
    return {"status": "failed", "error_type": type(exc).__name__, "message": str(exc)}


def _latest_date_text(frame: pd.DataFrame, column: str) -> str:
    if frame is None or frame.empty or column not in frame.columns:
        return ""
    dates = pd.to_datetime(frame[column], errors="coerce")
    if dates.dropna().empty:
        return ""
    return dates.max().strftime("%Y-%m-%d")


if __name__ == "__main__":
    raise SystemExit(main())
