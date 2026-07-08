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
from etf_flow_monitor.data.tushare_etf_source import IncompleteMarketCoverageError, TushareEtfSource  # noqa: E402
from etf_flow_monitor.utils.calendar import (  # noqa: E402
    current_shanghai_date,
    normalize_date_input,
    resolve_monitor_market_date,
    trading_calendar_from_frame,
)
from etf_flow_monitor.utils.io import format_tushare_date  # noqa: E402


INCOMPLETE_MARKET_COVERAGE_EXIT_CODE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update Tushare ETF daily cross-section caches.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", default="", help="Target request date. Blank means latest available market date.")
    parser.add_argument("--lookback-days", type=int, default=None, help="Legacy override; extends the configured cache window when larger.")
    parser.add_argument("--heartbeat-seconds", type=int, default=15)
    parser.add_argument("--refresh", action="store_true", help="Refresh all target dates even when cache files exist.")
    parser.add_argument("--full-check", action="store_true", help="Scan every required trading date instead of using the fast interval check.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    source = TushareEtfSource.from_runtime(cache_dir=config.cache_dir, search_dirs=[str(config_path.parent), str(PROJECT_ROOT)])

    requested_date = _parse_requested_date(args.trade_date) or pd.Timestamp(current_shanghai_date()).normalize()
    configured_source_start = _source_start_for_usable_date(config.local_cache_start_date)
    if args.lookback_days is not None:
        configured_source_start = min(configured_source_start, requested_date - pd.Timedelta(days=max(int(args.lookback_days), 20)))
    calendar_start = min(configured_source_start - pd.Timedelta(days=30), requested_date - pd.Timedelta(days=30))
    calendar_end = requested_date + pd.Timedelta(days=10)
    cached_calendar = source.cache.load_calendar(source.source_name, config.calendar_exchange) if source.cache is not None else None
    calendar_status = _calendar_refresh_status(cached_calendar, calendar_start, calendar_end)
    print(
        f"[cache] calendar auto-refresh: cached_tail={calendar_status['cached_tail']} "
        f"required_tail={calendar_status['required_tail']} action={calendar_status['action']}",
        flush=True,
    )
    try:
        calendar_frame = source.get_calendar(calendar_start, calendar_end, exchange=config.calendar_exchange, refresh=False)
    except Exception as exc:  # noqa: BLE001
        _print_update_failure(config.cache_dir, source.source_name, exc)
        return 0
    calendar = trading_calendar_from_frame(calendar_frame, exchange=config.calendar_exchange)
    if str(args.trade_date or "").strip():
        resolved_date, calendar_mode = resolve_monitor_market_date(
            calendar,
            requested_date.date(),
            explicit_request=True,
        )
        market_date = pd.Timestamp(resolved_date).normalize()
    else:
        resolved_date, calendar_mode = resolve_monitor_market_date(
            calendar,
            requested_date.date(),
            explicit_request=False,
        )
        market_date = pd.Timestamp(resolved_date).normalize()

    source_start = _previous_trading_day_or_same(calendar, configured_source_start)
    if source_start > market_date:
        source_start = market_date
    trade_dates = _open_trade_dates(calendar_frame, source_start, market_date)
    target_key = format_tushare_date(market_date)
    daily_keys = _cached_cross_section_key_set(config.cache_dir, source.source_name, "etf_daily")
    share_keys = _cached_cross_section_key_set(config.cache_dir, source.source_name, "etf_share")
    cache_interval = _complete_cache_interval_from_keys(daily_keys, share_keys)

    print(f"[cache] request_date={requested_date.date()} market_date={market_date.date()} mode={calendar_mode}", flush=True)
    print(
        f"[cache] usable_start={config.local_cache_start_date.isoformat()} "
        f"source_start={source_start.date()} target={target_key}",
        flush=True,
    )
    print(
        f"[cache] complete_interval={_format_complete_interval(cache_interval)} "
        f"daily_latest={cache_interval['daily_latest'] or 'none'} share_latest={cache_interval['share_latest'] or 'none'}",
        flush=True,
    )

    missing_daily, missing_share, check_mode = _missing_dates_for_update(
        daily_keys=daily_keys,
        share_keys=share_keys,
        trade_dates=trade_dates,
        refresh=args.refresh,
        full_check=args.full_check,
    )
    print(f"[cache] check_mode={check_mode}", flush=True)
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
    except IncompleteMarketCoverageError as exc:
        print('[cache] daily cross-section incomplete: ' + str(exc), flush=True)
        print('[cache] no incomplete cache file was written; BAT will skip report generation for this run.', flush=True)
        return INCOMPLETE_MARKET_COVERAGE_EXIT_CODE
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


def _calendar_refresh_status(cached_calendar: pd.DataFrame | None, start_date: pd.Timestamp, end_date: pd.Timestamp) -> dict[str, str]:
    required_tail = format_tushare_date(pd.Timestamp(end_date).normalize())
    cached_tail = _calendar_tail_text(cached_calendar)
    action = "cached" if _calendar_covers_date_range(cached_calendar, start_date, end_date) else "fetch"
    return {
        "cached_tail": cached_tail or "none",
        "required_tail": required_tail,
        "action": action,
    }


def _calendar_tail_text(frame: pd.DataFrame | None) -> str:
    if frame is None or frame.empty or "cal_date" not in frame.columns:
        return ""
    dates = pd.to_datetime(frame["cal_date"], errors="coerce").dropna()
    if dates.empty:
        return ""
    return format_tushare_date(dates.max())


def _calendar_covers_date_range(frame: pd.DataFrame | None, start_date: pd.Timestamp, end_date: pd.Timestamp) -> bool:
    if frame is None or frame.empty or "cal_date" not in frame.columns:
        return False
    dates = pd.to_datetime(frame["cal_date"], errors="coerce").dropna()
    if dates.empty:
        return False
    start_key = pd.Timestamp(start_date).normalize()
    end_key = pd.Timestamp(end_date).normalize()
    return bool(dates.le(start_key).any() and dates.ge(end_key).any())


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


def _source_start_for_usable_date(usable_start_date: object) -> pd.Timestamp:
    return pd.Timestamp(usable_start_date).normalize() - pd.DateOffset(months=12)


def _previous_trading_day_or_same(calendar: object, start_date: pd.Timestamp) -> pd.Timestamp:
    start_key = pd.Timestamp(start_date).normalize()
    try:
        resolved = pd.Timestamp(calendar.resolve_request_date_market_date(start_key.date())).normalize()
        if resolved < start_key:
            return resolved
        return pd.Timestamp(calendar.shift_trade_date(resolved.date(), -1)).normalize()
    except Exception:  # noqa: BLE001
        return start_key


def _missing_dates_for_update(
    *,
    daily_keys: set[str],
    share_keys: set[str],
    trade_dates: list[pd.Timestamp],
    refresh: bool,
    full_check: bool,
) -> tuple[list[pd.Timestamp], list[pd.Timestamp], str]:
    if refresh:
        candidates = list(trade_dates)
        mode = "refresh_all"
    elif full_check:
        candidates = list(trade_dates)
        mode = "full_check"
    else:
        candidates, mode = _fast_missing_candidate_dates(daily_keys, share_keys, trade_dates)

    missing_daily = [trade_date for trade_date in candidates if format_tushare_date(trade_date) not in daily_keys]
    missing_share = [trade_date for trade_date in candidates if format_tushare_date(trade_date) not in share_keys]
    return missing_daily, missing_share, mode


def _fast_missing_candidate_dates(
    daily_keys: set[str],
    share_keys: set[str],
    trade_dates: list[pd.Timestamp],
) -> tuple[list[pd.Timestamp], str]:
    if not trade_dates:
        return [], "empty_required_dates"
    required = [(format_tushare_date(trade_date), trade_date) for trade_date in trade_dates]
    start_key = required[0][0]
    end_key = required[-1][0]
    complete_keys = sorted(daily_keys & share_keys)
    if not complete_keys:
        return list(trade_dates), "empty_cache_full_range"
    complete_start = complete_keys[0]
    complete_end = complete_keys[-1]
    if complete_start <= start_key and complete_end >= end_key:
        return [], "fast_interval_covered"

    candidates: list[pd.Timestamp] = []
    if complete_start > start_key:
        candidates.extend(trade_date for key, trade_date in required if key < complete_start)
    if complete_end < end_key:
        candidates.extend(trade_date for key, trade_date in required if key > complete_end)
    if not candidates:
        candidates = list(trade_dates)
        return candidates, "fast_interval_inconclusive_full_range"
    return candidates, "fast_interval_edges"


def _cached_cross_section_key_set(cache_dir: Path, source_name: str, dataset_name: str) -> set[str]:
    directory = cache_dir / source_name / "daily_cross_section" / dataset_name
    if not directory.exists():
        return set()
    return {path.stem for path in directory.glob("*.csv") if len(path.stem) == 8 and path.stem.isdigit()}


def _complete_cache_interval_from_keys(daily_keys: set[str], share_keys: set[str]) -> dict[str, str]:
    complete_keys = sorted(daily_keys & share_keys)
    return {
        "earliest": complete_keys[0] if complete_keys else "",
        "latest": complete_keys[-1] if complete_keys else "",
        "daily_latest": max(daily_keys) if daily_keys else "",
        "share_latest": max(share_keys) if share_keys else "",
    }


def _format_complete_interval(interval: dict[str, str]) -> str:
    earliest = interval.get("earliest", "")
    latest = interval.get("latest", "")
    if not earliest or not latest:
        return "none"
    return f"{earliest} 至 {latest}"


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
