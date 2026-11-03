"""Compare iFinD and Tushare listed-share snapshots for one trade date."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from etf_flow_monitor.config import load_config
from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.category_map import category_map_codes, load_category_map
from etf_flow_monitor.data.ifind_etf_source import BATCH_SIZE, IFindDateUnavailableError, IFindEtfSource
from etf_flow_monitor.data.schemas import ACTUAL_SHARE_UNIT, normalize_etf_basic_frame
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource
from etf_flow_monitor.run_ledger import RunLedger, make_log_dir
from etf_flow_monitor.utils.io import format_tushare_date, write_user_csv


SHANGHAI = ZoneInfo("Asia/Shanghai")


def compare(args: argparse.Namespace, config, ledger: RunLedger) -> int:
    cache = CacheStore(config.cache_dir)
    date_key = format_tushare_date(args.trade_date)
    if not date_key:
        raise ValueError("Invalid trade date")
    date = pd.Timestamp(date_key)
    calendar = cache.load_calendar("tushare", config.calendar_exchange)
    if calendar is None or not (
        pd.to_datetime(calendar["cal_date"]).eq(date) & calendar["is_open"].eq(1)
    ).any():
        raise ValueError(f"{date_key} is not an open date in the cached exchange calendar")
    basic = normalize_etf_basic_frame(cache.load_static_frame("tushare", "etf_basic", config.etf_market))
    if basic.empty:
        raise RuntimeError("Tushare ETF basic cache is missing")
    active = basic.loc[
        (basic["list_date"].isna() | basic["list_date"].le(date))
        & (basic["delist_date"].isna() | basic["delist_date"].gt(date)),
        "fund_code",
    ]
    category_codes = category_map_codes(load_category_map(config.category_map_path))
    codes = sorted(set(active).intersection(category_codes) if category_codes else set(active))
    if args.codes:
        selected = {code.strip().upper() for code in args.codes.split(",")}
        if selected.difference(codes):
            raise ValueError("Some requested codes are outside the active ETF universe")
        codes = sorted(selected)
    if not codes:
        raise RuntimeError("No ETF codes selected")

    errors = []
    tushare_observed_at = None
    if args.tushare_cache:
        tushare = cache.load_daily_cross_section("tushare", "etf_share", date_key)
    else:
        ledger.progress("tushare_fetch", date_key)
        tushare = None
        try:
            source = TushareEtfSource.from_runtime(
                cache_dir=config.cache_dir,
                search_dirs=[str(Path(args.config).resolve().parent), str(Path(__file__).resolve().parents[1])],
            )
            tushare = source.fetch_share_snapshot(date)
            tushare_observed_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
        except (RuntimeError, ValueError, OSError) as exc:
            errors.append(f"Tushare: {type(exc).__name__}")
    comparison = pd.DataFrame({"fund_code": codes}).set_index("fund_code")
    comparison["trade_date"] = date.date()
    comparison["tushare_shares"] = float("nan")
    if tushare is not None:
        eligible = tushare.loc[
            tushare["share_unit"].eq(ACTUAL_SHARE_UNIT)
            & pd.to_datetime(tushare["trade_date"]).eq(date)
            & tushare["source"].eq("tushare:fund_share")
        ].set_index("fund_code")
        values = pd.to_numeric(eligible["shares"], errors="coerce")
        comparison["tushare_shares"] = values.where(values.gt(0) & values.lt(float("inf"))) * 10000

    observed_start = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    ifind_shares = {}
    stale_batches = 0
    stale_messages = set()
    try:
        source = IFindEtfSource(cache)
        for start in range(0, len(codes), BATCH_SIZE):
            ledger.progress("ifind_fetch", date_key, done=start, total=len(codes))
            try:
                ifind_shares.update(source.get_listed_shares(codes[start:start + BATCH_SIZE], date_key))
            except IFindDateUnavailableError as exc:
                stale_batches += 1
                stale_messages.add(str(exc))
    except (RuntimeError, ValueError) as exc:
        errors.append(str(exc))

    comparison["ifind_shares"] = pd.Series(ifind_shares, dtype=float)
    comparison["ifind_minus_tushare_shares"] = comparison["ifind_shares"] - comparison["tushare_shares"]
    tushare_present = comparison["tushare_shares"].notna()
    ifind_present = comparison["ifind_shares"].notna()
    comparison["status"] = "missing_both"
    comparison.loc[~tushare_present & ifind_present, "status"] = "ifind_only"
    comparison.loc[tushare_present & ~ifind_present, "status"] = "tushare_only"
    comparison.loc[tushare_present & ifind_present, "status"] = "different"
    comparison.loc[comparison["ifind_minus_tushare_shares"].abs().le(1), "status"] = "match"

    path = config.output_dir / "flow_monitor" / date_key / f"{ledger.log_dir.name}.csv"
    write_user_csv(path, comparison.reset_index())
    ledger.record_outputs(comparison=str(path))
    stats = {
        "trade_date": date_key,
        "requested": len(codes),
        "tushare_mode": "cache" if args.tushare_cache else "live",
        "tushare_observed": int(tushare_present.sum()),
        "tushare_observed_at": tushare_observed_at,
        "ifind_observed": int(ifind_present.sum()),
        "ifind_stale_batches": stale_batches,
        "ifind_stale_details": sorted(stale_messages),
        "ifind_started_at": observed_start,
        "ifind_finished_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        **comparison["status"].value_counts().to_dict(),
    }
    ledger.record_stats(stats)
    print(stats, flush=True)
    if errors or not ifind_present.all() or not tushare_present.all():
        ledger.progress("degraded", "; ".join(errors) or "At least one source lacks eligible rows")
        return 2
    ledger.progress("compared", date_key)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--codes", help="Optional comma-separated ETF sample")
    parser.add_argument("--tushare-cache", action="store_true", help="Compare against the existing Tushare share slice")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    log_dir = make_log_dir(config.output_dir, prefix="share_compare")
    log_dir = log_dir.with_name(f"{log_dir.name}_{uuid4().hex[:8]}")
    ledger = RunLedger(log_dir=log_dir, argv=sys.argv[1:], config_path=config_path)
    try:
        result = compare(args, config, ledger)
        ledger.finish(exit_code=result, status="success" if result == 0 else "degraded")
        return result
    except Exception as exc:
        ledger.finish(exit_code=1, status="failed", error=str(exc))
        print(f"[share_compare] {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
