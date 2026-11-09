from unittest.mock import Mock

import pandas as pd
import pytest

from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.ifind_etf_source import IFindDateUnavailableError, _listed_shares, _quote_rows
from etf_flow_monitor.data.schemas import ACTUAL_SHARE_UNIT
from etf_flow_monitor.data.tushare_etf_source import IncompleteMarketCoverageError, TushareEtfSource
from tools.update_flow_cache import DualSourceNoDataError, _refresh_dual_source


def test_ifind_previous_day_column_is_never_used_for_target_date():
    payload = {"tables": [{"table": {
        "基金代码": ["510300.SH"],
        "基金@场内流通份额[20261102]": [41401387700],
    }}]}
    with pytest.raises(IFindDateUnavailableError, match="20261102"):
        _listed_shares(payload, ["510300.SH"], "20261103")


def test_ifind_accepts_only_exact_date_requested_codes_and_positive_shares():
    payload = {"tables": [{"table": {
        "基金代码": ["510300.SH", "159915.SZ", "159001.SZ"],
        "基金@场内流通份额[20261102]": [41401387700, 100, 100],
        "基金@场内流通份额[20261103]": [41410000000, None, 100],
    }}]}
    assert _listed_shares(payload, ["510300.SH", "159915.SZ"], "20261103") == {
        "510300.SH": 41410000000,
    }


def test_ifind_quotes_convert_amount_and_volume_for_exact_date():
    payload = {"tables": [{
        "thscode": "510300.SH",
        "time": ["2026-11-02"],
        "table": {
            "open": [4.084], "high": [4.12], "low": [4.065], "close": [4.085],
            "preClose": [4.065], "change": [0.02], "changeRatio": [0.49],
            "volume": [2951453077], "amount": [12066215573],
        },
    }]}
    assert _quote_rows(payload, ["510300.SH"], "2026-11-03") == []
    row, = _quote_rows(payload, ["510300.SH"], "2026-11-02")
    assert row["volume"] == 29514530.77
    assert row["amount"] == 12066215.573


@pytest.mark.parametrize("available", ["tushare", "ifind", "cache", "neither"])
def test_dual_source_share_refresh(available, tmp_path):
    day = pd.Timestamp("2026-11-02")
    key = "20261102"
    codes = ["510300.SH", "159915.SZ"]
    basic = pd.DataFrame({"fund_code": codes, "list_date": pd.NaT, "delist_date": pd.NaT})
    shares = pd.DataFrame({
        "fund_code": codes, "trade_date": day, "shares": [100, 200],
        "share_unit": ACTUAL_SHARE_UNIT,
    })
    source = Mock(source_name="tushare", cache=CacheStore(tmp_path))
    source.fetch_share_snapshot.return_value = (
        shares.assign(source="tushare:fund_share") if available == "tushare" else shares.iloc[:0]
    )
    if available == "cache":
        source.cache.save_daily_cross_section("tushare", "etf_share", key, shares.assign(source="ifind:listed_shares"))
    ifind = Mock()
    ifind.fetch_snapshot.return_value = (
        shares.assign(source="ifind:listed_shares") if available == "ifind" else shares.iloc[:0]
    )
    get_ifind = Mock(return_value=ifind)
    args = (source, get_ifind, "etf_share", codes, basic, day, Mock())
    if available == "neither":
        with pytest.raises(DualSourceNoDataError):
            _refresh_dual_source(*args)
        assert source.cache.load_daily_cross_section("tushare", "etf_share", key) is None
        return

    _refresh_dual_source(*args)
    cached = source.cache.load_daily_cross_section("tushare", "etf_share", key)
    assert cached["source"].tolist() == [
        "tushare:fund_share" if available == "tushare" else "ifind:listed_shares"
    ] * 2
    assert get_ifind.called == (available == "ifind")


def test_date_labelled_market_copy_rejected_until_revised(tmp_path):
    day = pd.Timestamp("2026-11-09")
    prior = pd.Timestamp("2026-11-06")
    key = "20261109"
    codes = [f"{510000 + i}.SH" for i in range(60)] + [f"{159000 + i}.SZ" for i in range(60)]
    basic = pd.DataFrame({"fund_code": codes, "list_date": pd.NaT, "delist_date": pd.NaT})
    previous = pd.DataFrame({
        "fund_code": codes, "trade_date": prior, "shares": range(100, 220),
        "source": "tushare:fund_share", "share_unit": ACTUAL_SHARE_UNIT,
    })
    # Different order, a missing code and a weekend between the official sessions.
    stale = previous.iloc[:0:-1].assign(trade_date=day, source="ifind:listed_shares")
    cache = CacheStore(tmp_path)
    cache.save_calendar("tushare", "SSE", pd.DataFrame({
        "cal_date": [day], "is_open": [1], "pretrade_date": [prior],
    }))
    cache.save_daily_cross_section("tushare", "etf_share", "20261106", previous)
    primary = Mock(source_name="tushare", cache=cache)
    primary.fetch_share_snapshot.return_value = previous.iloc[:0]
    fallback = Mock()
    fallback.fetch_snapshot.return_value = stale
    args = (primary, lambda: fallback, "etf_share", codes, basic, day, Mock())
    with pytest.raises(DualSourceNoDataError, match="119 只可比 ETF 全部相同"):
        _refresh_dual_source(*args)
    assert cache.load_daily_cross_section("tushare", "etf_share", key) is None

    cache.save_daily_cross_section("tushare", "etf_share", key, stale)
    reader = TushareEtfSource(None, cache)
    with pytest.raises(IncompleteMarketCoverageError, match="20261106"):
        reader.get_etf_share_by_trade_dates(
            codes[:1], [day], cache_only=True, columns=["fund_code", "trade_date", "shares"],
        )

    fresh = stale.astype({"shares": float})
    fresh.loc[fresh.index[0], "shares"] += 0.0001  # one underlying share
    fallback.fetch_snapshot.return_value = fresh
    _refresh_dual_source(*args)
    recovered = reader.get_etf_share_by_trade_dates(codes, [day], cache_only=True).set_index("fund_code")
    assert recovered.at[fresh.iloc[0]["fund_code"], "shares"] == fresh.iloc[0]["shares"]
