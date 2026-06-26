"""Update daily cross-section caches for ETF flow monitor inputs."""

from __future__ import annotations

import argparse
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

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.data.category_map import category_map_codes, load_category_map  # noqa: E402
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource  # noqa: E402
from etf_flow_monitor.utils.calendar import current_shanghai_date, normalize_date_input, trading_calendar_from_frame  # noqa: E402
from etf_flow_monitor.utils.io import format_tushare_date  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update Tushare ETF daily cross-section caches.")
    parser.add_argument("--config", default="config.example.txt")
    parser.add_argument("--trade-date", default="", help="Target request date. Blank means latest available market date.")
    parser.add_argument("--lookback-days", type=int, default=None, help="Override config lookback_days.")
    parser.add_argument("--heartbeat-seconds", type=int, default=15)
    parser.add_argument("--refresh", action="store_true", help="Refresh all target dates even when cache files exist.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])
    lookback_days = int(args.lookback_days if args.lookback_days is not None else config.lookback_days)

    requested_date = _parse_requested_date(args.trade_date) or pd.Timestamp(current_shanghai_date()).normalize()
    calendar_start = requested_date - pd.Timedelta(days=max(lookback_days + 30, 450))
    calendar_end = requested_date + pd.Timedelta(days=10)
    try:
        calendar_frame = source.get_calendar(calendar_start, calendar_end, exchange=config.calendar_exchange, refresh=False)
    except Exception as exc:  # noqa: BLE001
        _print_update_failure(config.cache_dir, source.source_name, exc)
        return 0
    calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
    if str(args.trade_date or "").strip():
        market_date = pd.Timestamp(calendar.resolve_request_date_market_date(requested_date.date())).normalize()
        calendar_mode = "explicit_request_date"
    else:
        market_date = pd.Timestamp(calendar.resolve_market_date(requested_date.date())).normalize()
        calendar_mode = "latest_available_market_date"

    start_date = market_date - pd.Timedelta(days=max(lookback_days, 20))
    trade_dates = _open_trade_dates(calendar_frame, start_date, market_date)
    target_key = format_tushare_date(market_date)
    daily_cutoff = _latest_cached_trade_date(config.cache_dir, source.source_name, "etf_daily")
    share_cutoff = _latest_cached_trade_date(config.cache_dir, source.source_name, "etf_share")
    current_cutoff = _min_date_text(daily_cutoff, share_cutoff)

    print(f"[cache] request_date={requested_date.date()} market_date={market_date.date()} mode={calendar_mode}", flush=True)
    print(f"[cache] current_cutoff={current_cutoff or 'none'} daily_cutoff={daily_cutoff or 'none'} share_cutoff={share_cutoff or 'none'}", flush=True)

    missing_daily = _missing_dates(config.cache_dir, source.source_name, "etf_daily", trade_dates, refresh=args.refresh)
    missing_share = _missing_dates(config.cache_dir, source.source_name, "etf_share", trade_dates, refresh=args.refresh)
    total_missing = len(missing_daily) + len(missing_share)
    if total_missing == 0:
        print(f"[cache] 已更新至最新 {target_key}，开始生成页面。", flush=True)
        return 0

    print(f"[cache] 未更新至最新 {target_key}，正在更新：daily 缺 {len(missing_daily)} 天，share 缺 {len(missing_share)} 天。", flush=True)
    category_map = load_category_map(config.category_map_path)
    category_codes = category_map_codes(category_map)
    try:
        basic = source.get_etf_basic(market=config.etf_market, refresh=False)
    except Exception as exc:  # noqa: BLE001
        _print_update_failure(config.cache_dir, source.source_name, exc)
        return 0
    basic_codes = set(basic["fund_code"].dropna().astype(str).str.upper().tolist()) if "fund_code" in basic.columns else set()
    codes = [code for code in category_codes if code in basic_codes] if category_codes else sorted(basic_codes)
    state = _HeartbeatState(total=total_missing)

    heartbeat = _Heartbeat(args.heartbeat_seconds, state)
    heartbeat.start()
    try:
        for trade_date in missing_daily:
            state.stage = "fund_daily"
            state.trade_date = trade_date
            source.get_etf_daily_by_trade_dates(codes, [trade_date], refresh=True)
            state.done += 1
        for trade_date in missing_share:
            state.stage = "fund_share"
            state.trade_date = trade_date
            source.get_etf_share_by_trade_dates(codes, [trade_date], refresh=True)
            state.done += 1
    except Exception as exc:  # noqa: BLE001
        _print_update_failure(config.cache_dir, source.source_name, exc)
        return 0
    finally:
        heartbeat.stop()
    print(f"[cache] 更新完成：target={target_key} total_updated={state.done}/{state.total}", flush=True)
    print(f"[cache] cache_stats={source.cache.snapshot_stats() if source.cache is not None else {}}", flush=True)
    return 0


def _parse_requested_date(value: str | None) -> pd.Timestamp | None:
    text = str(value or "").strip()
    if not text:
        return None
    return pd.Timestamp(normalize_date_input(text, field_name="trade_date")).normalize()


def _open_trade_dates(calendar_frame: pd.DataFrame, start_date: pd.Timestamp, end_date: pd.Timestamp) -> list[pd.Timestamp]:
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


def _missing_dates(cache_dir: Path, source_name: str, dataset_name: str, trade_dates: list[pd.Timestamp], *, refresh: bool) -> list[pd.Timestamp]:
    if refresh:
        return list(trade_dates)
    return [trade_date for trade_date in trade_dates if not _daily_cross_section_path(cache_dir, source_name, dataset_name, trade_date).exists()]


def _latest_cached_trade_date(cache_dir: Path, source_name: str, dataset_name: str) -> str:
    directory = cache_dir / source_name / "daily_cross_section" / dataset_name
    if not directory.exists():
        return ""
    keys = sorted(path.stem for path in directory.glob("*.csv") if len(path.stem) == 8 and path.stem.isdigit())
    return keys[-1] if keys else ""


def _earliest_cached_trade_date(cache_dir: Path, source_name: str, dataset_name: str) -> str:
    directory = cache_dir / source_name / "daily_cross_section" / dataset_name
    if not directory.exists():
        return ""
    keys = sorted(path.stem for path in directory.glob("*.csv") if len(path.stem) == 8 and path.stem.isdigit())
    return keys[0] if keys else ""


def _daily_cross_section_path(cache_dir: Path, source_name: str, dataset_name: str, trade_date: object) -> Path:
    return cache_dir / source_name / "daily_cross_section" / dataset_name / f"{format_tushare_date(trade_date)}.csv"


def _min_date_text(left: str, right: str) -> str:
    if not left or not right:
        return ""
    return min(left, right)


def _max_date_text(left: str, right: str) -> str:
    if not left or not right:
        return ""
    return max(left, right)


def _print_update_failure(cache_dir: Path, source_name: str, exc: Exception) -> None:
    daily_start = _earliest_cached_trade_date(cache_dir, source_name, "etf_daily")
    share_start = _earliest_cached_trade_date(cache_dir, source_name, "etf_share")
    daily_end = _latest_cached_trade_date(cache_dir, source_name, "etf_daily")
    share_end = _latest_cached_trade_date(cache_dir, source_name, "etf_share")
    complete_start = _max_date_text(daily_start, share_start)
    complete_end = _min_date_text(daily_end, share_end)
    if complete_start and complete_end:
        interval = f"{complete_start} 至 {complete_end}"
    else:
        interval = f"none（daily 截至 {daily_end or 'none'}，share 截至 {share_end or 'none'}）"
    print(f"[cache] 更新失败：{exc}", flush=True)
    print(f"[cache] 本地完整缓存区间：{interval}", flush=True)
    print("[cache] 将继续尝试使用本地缓存生成；超出本地统计区间的报表会跳过。", flush=True)


class _HeartbeatState:
    def __init__(self, total: int) -> None:
        self.total = int(total)
        self.done = 0
        self.stage = "starting"
        self.trade_date: object = ""
        self.started_at = time.monotonic()


class _Heartbeat:
    def __init__(self, interval_seconds: int, state: _HeartbeatState) -> None:
        self.interval_seconds = max(int(interval_seconds), 1)
        self.state = state
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="flow-cache-heartbeat", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            elapsed = int(time.monotonic() - self.state.started_at)
            trade_date = format_tushare_date(self.state.trade_date) if self.state.trade_date else ""
            print(
                f"[cache heartbeat] {datetime.now().strftime('%H:%M:%S')} "
                f"stage={self.state.stage} date={trade_date} progress={self.state.done}/{self.state.total} elapsed={elapsed}s",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
