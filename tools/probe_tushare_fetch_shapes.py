"""Probe Tushare ETF endpoint fetch shapes for batching decisions."""

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
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.utils.io import format_tushare_date  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe Tushare fund_daily/fund_share batch fetch shapes.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", required=True, help="Trade date in YYYYMMDD or YYYY-MM-DD format.")
    parser.add_argument("--sample-code", default="510300.SH", help="Code used for per-code comparison.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
    trade_date = format_tushare_date(pd.Timestamp(args.trade_date).normalize())
    sample_code = str(args.sample_code).strip().upper()

    checks: dict[str, object] = {
        "schema_version": "tushare_fetch_shape_probe_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "sample_code": sample_code,
        "checks": {},
    }
    for api_name, fields in (
        ("fund_daily", "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"),
        ("fund_share", "ts_code,trade_date,fd_share"),
    ):
        checks["checks"][api_name] = {
            "by_trade_date": _probe(source, api_name, {"trade_date": trade_date}, fields),
            "by_code_and_trade_date": _probe(source, api_name, {"ts_code": sample_code, "trade_date": trade_date}, fields),
        }

    output_path = _output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def _probe(source: TushareEtfSource, api_name: str, params: dict[str, str], fields: str) -> dict[str, object]:
    try:
        rows = source.client.query(api_name, params=params, fields=fields)
        frame = pd.DataFrame(rows)
        return {
            "ok": True,
            "rows": int(len(frame)),
            "columns": list(frame.columns),
            "unique_codes": int(frame["ts_code"].astype(str).str.upper().nunique()) if "ts_code" in frame.columns else 0,
            "sample": frame.head(5).to_dict(orient="records"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _output_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "outputs" / "source_health" / f"tushare_fetch_shape_probe_{stamp}.json"


if __name__ == "__main__":
    raise SystemExit(main())
