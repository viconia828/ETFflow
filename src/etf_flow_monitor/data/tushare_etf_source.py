"""Tushare-backed ETF data source for the starter flow monitor."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.schemas import (
    normalize_calendar_frame,
    normalize_etf_basic_frame,
    normalize_etf_daily_frame,
    normalize_etf_share_frame,
)
from etf_flow_monitor.data.tushare_http import TushareHttpClient, load_tushare_runtime_config
from etf_flow_monitor.utils.io import format_tushare_date, merge_frames


class TushareEtfSource:
    """Thin source layer around Tushare endpoints used by the monitor."""

    source_name = "tushare"

    def __init__(self, client: TushareHttpClient | None, cache: CacheStore | None = None) -> None:
        self.client = client
        self.cache = cache

    @classmethod
    def from_runtime(cls, *, cache_dir: str | Path = "data/cache", search_dirs: list[str] | None = None) -> "TushareEtfSource":
        runtime = load_tushare_runtime_config(search_dirs=search_dirs or [str(Path.cwd())])
        client = TushareHttpClient(**runtime)
        return cls(client=client, cache=CacheStore(cache_dir))

    @classmethod
    def from_cache(cls, *, cache_dir: str | Path = "data/cache") -> "TushareEtfSource":
        return cls(client=None, cache=CacheStore(cache_dir))

    def get_calendar(
        self,
        start_date: object,
        end_date: object,
        *,
        exchange: str = "SSE",
        refresh: bool = False,
        cache_only: bool = False,
    ) -> pd.DataFrame:
        cached = None if refresh or self.cache is None else self.cache.load_calendar(self.source_name, exchange)
        start_key = pd.Timestamp(start_date).normalize()
        end_key = pd.Timestamp(end_date).normalize()
        if _covers_date_range(cached, "cal_date", start_key, end_key):
            return _filter_date_range(cached, "cal_date", start_key, end_key)
        if cache_only:
            return _filter_date_range(cached, "cal_date", start_key, end_key)

        rows = self.client.query(
            "trade_cal",
            params={"exchange": exchange, "start_date": format_tushare_date(start_key), "end_date": format_tushare_date(end_key)},
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        fresh = normalize_calendar_frame(rows)
        merged = merge_frames(cached, fresh, key_columns=("exchange", "cal_date"), sort_columns=("exchange", "cal_date"))
        if self.cache is not None:
            self.cache.save_calendar(self.source_name, exchange, merged)
        return _filter_date_range(merged, "cal_date", start_key, end_key)

    def get_etf_basic(self, *, market: str = "E", refresh: bool = False, cache_only: bool = False) -> pd.DataFrame:
        cached = None if refresh or self.cache is None else self.cache.load_static_frame(self.source_name, "etf_basic", market)
        if cached is not None and not cached.empty:
            return normalize_etf_basic_frame(cached)
        if cache_only:
            return normalize_etf_basic_frame(cached)
        rows = self.client.query(
            "fund_basic",
            params={"market": market},
            fields="ts_code,name,market,fund_type,management,custodian,list_date,delist_date,status,benchmark",
        )
        fresh = normalize_etf_basic_frame(pd.DataFrame(rows))
        if self.cache is not None:
            self.cache.save_static_frame(self.source_name, "etf_basic", market, fresh)
        return fresh

    def get_etf_daily(self, codes: list[str], start_date: object, end_date: object, *, refresh: bool = False) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        start_key = pd.Timestamp(start_date).normalize()
        end_key = pd.Timestamp(end_date).normalize()
        for code in _unique_codes(codes):
            cached = None if refresh or self.cache is None else self.cache.load_time_series(self.source_name, "etf_daily", code)
            if _covers_date_range(cached, "trade_date", start_key, end_key):
                frames.append(_filter_date_range(cached, "trade_date", start_key, end_key))
                continue
            rows = self.client.query(
                "fund_daily",
                params={"ts_code": code, "start_date": format_tushare_date(start_key), "end_date": format_tushare_date(end_key)},
                fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
            )
            fresh = normalize_etf_daily_frame(pd.DataFrame(rows))
            merged = merge_frames(cached, fresh, key_columns=("fund_code", "trade_date"), sort_columns=("fund_code", "trade_date"))
            if self.cache is not None:
                self.cache.save_time_series(self.source_name, "etf_daily", code, merged)
            frames.append(_filter_date_range(merged, "trade_date", start_key, end_key))
        if not frames:
            return normalize_etf_daily_frame(None)
        return normalize_etf_daily_frame(pd.concat(frames, ignore_index=True))

    def get_etf_daily_by_trade_dates(
        self,
        codes: list[str],
        trade_dates: list[object],
        *,
        refresh: bool = False,
        cache_only: bool = False,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        code_set = set(_unique_codes(codes))
        for trade_date in _unique_trade_dates(trade_dates):
            cached = None if refresh or self.cache is None else self.cache.load_daily_cross_section(self.source_name, "etf_daily", trade_date)
            if cached is None:
                if cache_only:
                    frames.append(normalize_etf_daily_frame(None))
                    continue
                rows = self.client.query(
                    "fund_daily",
                    params={"trade_date": trade_date},
                    fields="ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
                )
                frame = normalize_etf_daily_frame(pd.DataFrame(rows))
                if self.cache is not None:
                    self.cache.save_daily_cross_section(self.source_name, "etf_daily", trade_date, frame)
            else:
                frame = normalize_etf_daily_frame(cached)
            if code_set and not frame.empty:
                frame = frame.loc[frame["fund_code"].astype(str).str.upper().isin(code_set)].copy()
            frames.append(frame)
        if not frames:
            return normalize_etf_daily_frame(None)
        return normalize_etf_daily_frame(pd.concat(frames, ignore_index=True))

    def get_etf_share(self, codes: list[str], start_date: object, end_date: object, *, refresh: bool = False) -> pd.DataFrame:
        """Load ETF share outstanding data when the local Tushare account supports it.

        Tushare field availability varies by account and endpoint. Keep this method isolated
        so the project can swap in Wind, Eastmoney, exchange files, or manual share files.
        """

        frames: list[pd.DataFrame] = []
        start_key = pd.Timestamp(start_date).normalize()
        end_key = pd.Timestamp(end_date).normalize()
        for code in _unique_codes(codes):
            cached = None if refresh or self.cache is None else self.cache.load_time_series(self.source_name, "etf_share", code)
            if _covers_date_range(cached, "trade_date", start_key, end_key):
                frames.append(_filter_date_range(cached, "trade_date", start_key, end_key))
                continue
            rows = self.client.query(
                "fund_share",
                params={"ts_code": code, "start_date": format_tushare_date(start_key), "end_date": format_tushare_date(end_key)},
                fields="ts_code,trade_date,fd_share",
            )
            fresh = normalize_etf_share_frame(pd.DataFrame(rows).assign(source="tushare:fund_share"))
            merged = merge_frames(cached, fresh, key_columns=("fund_code", "trade_date"), sort_columns=("fund_code", "trade_date"))
            if self.cache is not None:
                self.cache.save_time_series(self.source_name, "etf_share", code, merged)
            frames.append(_filter_date_range(merged, "trade_date", start_key, end_key))
        if not frames:
            return normalize_etf_share_frame(None)
        return normalize_etf_share_frame(pd.concat(frames, ignore_index=True))

    def get_etf_share_by_trade_dates(
        self,
        codes: list[str],
        trade_dates: list[object],
        *,
        refresh: bool = False,
        cache_only: bool = False,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        code_set = set(_unique_codes(codes))
        for trade_date in _unique_trade_dates(trade_dates):
            cached = None if refresh or self.cache is None else self.cache.load_daily_cross_section(self.source_name, "etf_share", trade_date)
            if cached is None:
                if cache_only:
                    frames.append(normalize_etf_share_frame(None))
                    continue
                rows = self.client.query(
                    "fund_share",
                    params={"trade_date": trade_date},
                    fields="ts_code,trade_date,fd_share",
                )
                frame = normalize_etf_share_frame(pd.DataFrame(rows).assign(source="tushare:fund_share"))
                if self.cache is not None:
                    self.cache.save_daily_cross_section(self.source_name, "etf_share", trade_date, frame)
            else:
                frame = normalize_etf_share_frame(cached)
            if code_set and not frame.empty:
                frame = frame.loc[frame["fund_code"].astype(str).str.upper().isin(code_set)].copy()
            frames.append(frame)
        if not frames:
            return normalize_etf_share_frame(None)
        return normalize_etf_share_frame(pd.concat(frames, ignore_index=True))


def _unique_codes(codes: list[str]) -> list[str]:
    ordered: list[str] = []
    for code in codes:
        normalized = str(code or "").strip().upper()
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _unique_trade_dates(trade_dates: list[object]) -> list[str]:
    ordered: list[str] = []
    for trade_date in trade_dates:
        normalized = format_tushare_date(pd.Timestamp(trade_date).normalize())
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered


def _covers_date_range(frame: pd.DataFrame | None, column: str, start_key: pd.Timestamp, end_key: pd.Timestamp) -> bool:
    if frame is None or frame.empty or column not in frame.columns:
        return False
    dates = pd.to_datetime(frame[column], errors="coerce")
    return bool(dates.le(start_key).any() and dates.ge(end_key).any())


def _filter_date_range(frame: pd.DataFrame | None, column: str, start_key: pd.Timestamp, end_key: pd.Timestamp) -> pd.DataFrame:
    if frame is None or frame.empty or column not in frame.columns:
        return pd.DataFrame()
    dates = pd.to_datetime(frame[column], errors="coerce")
    return frame.loc[dates.ge(start_key) & dates.le(end_key)].copy().reset_index(drop=True)
