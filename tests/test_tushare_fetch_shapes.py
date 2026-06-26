from __future__ import annotations

import pandas as pd

from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource


class FakeTushareClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], str | None]] = []

    def query(self, api_name: str, params: dict[str, str] | None = None, fields: str | None = None) -> list[dict[str, object]]:
        self.calls.append((api_name, dict(params or {}), fields))
        if api_name == "fund_daily":
            return [
                {
                    "ts_code": "510300.SH",
                    "trade_date": params["trade_date"],
                    "open": 5.0,
                    "high": 5.1,
                    "low": 4.9,
                    "close": 5.0,
                    "pre_close": 4.9,
                    "change": 0.1,
                    "pct_chg": 2.0,
                    "vol": 10.0,
                    "amount": 50.0,
                },
                {
                    "ts_code": "512000.SH",
                    "trade_date": params["trade_date"],
                    "open": 1.0,
                    "high": 1.1,
                    "low": 0.9,
                    "close": 1.0,
                    "pre_close": 0.9,
                    "change": 0.1,
                    "pct_chg": 11.1,
                    "vol": 20.0,
                    "amount": 20.0,
                },
            ]
        if api_name == "fund_share":
            return [
                {"ts_code": "510300.SH", "trade_date": params["trade_date"], "fd_share": 1000.0},
                {"ts_code": "512000.SH", "trade_date": params["trade_date"], "fd_share": 2000.0},
            ]
        return []


def test_get_etf_daily_by_trade_dates_uses_cross_section_cache(tmp_path) -> None:
    client = FakeTushareClient()
    source = TushareEtfSource(client=client, cache=CacheStore(tmp_path / "cache"))

    first = source.get_etf_daily_by_trade_dates(["510300.SH"], [pd.Timestamp("2026-06-25")])
    second = source.get_etf_daily_by_trade_dates(["510300.SH"], [pd.Timestamp("2026-06-25")])

    assert len(first) == 1
    assert len(second) == 1
    assert first.loc[0, "fund_code"] == "510300.SH"
    assert first.loc[0, "amount"] == 50.0
    assert second.loc[0, "amount"] == 50.0
    assert client.calls == [
        (
            "fund_daily",
            {"trade_date": "20260625"},
            "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount",
        )
    ]


def test_get_etf_share_by_trade_dates_tags_source_and_filters_codes(tmp_path) -> None:
    client = FakeTushareClient()
    source = TushareEtfSource(client=client, cache=CacheStore(tmp_path / "cache"))

    shares = source.get_etf_share_by_trade_dates(["510300.SH"], ["2026-06-25"])

    assert len(shares) == 1
    assert shares.loc[0, "fund_code"] == "510300.SH"
    assert shares.loc[0, "shares"] == 1000.0
    assert shares.loc[0, "source"] == "tushare:fund_share"
