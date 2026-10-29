from pathlib import Path
import pandas as pd
import pytest

from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.schemas import ACTUAL_SHARE_UNIT
from etf_flow_monitor.data.share_units import normalize_fetched_shares, ShareDataQualityError
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource, IncompleteMarketCoverageError
from tools.update_flow_cache import _missing_dates_for_update


def test_force_refresh_replaces_existing_data_and_preserves_raw_response(tmp_path):
    cache = CacheStore(tmp_path / "cache")
    code = "511880.SH"
    day = "20261026"
    cache.save_daily_cross_section("tushare", "etf_share", day, pd.DataFrame([
        {"fund_code": code, "trade_date": "2026-10-26", "shares": 12059914, "source": "tushare:fund_share"}
    ]))
    class Client:
        def query(self, *args, **kwargs):
            return [{"ts_code": code, "trade_date": day, "fd_share": 120599.14}]
    dates = [pd.Timestamp(day)]
    _, refresh_dates, _ = _missing_dates_for_update(daily_keys={day}, share_keys={day}, trade_dates=dates, refresh=True, full_check=False)
    result = TushareEtfSource(Client(), cache).get_etf_share_by_trade_dates([code], refresh_dates, refresh=True)
    assert result.shares.tolist() == [120599.14]
    assert result.share_unit.tolist() == [ACTUAL_SHARE_UNIT]
    reread = TushareEtfSource.from_cache(cache_dir=tmp_path / "cache").get_etf_share_by_trade_dates([code], dates, cache_only=True)
    assert reread.shares.tolist() == [120599.14]
    assert len(list((tmp_path / "cache/tushare/raw/fund_share").rglob("*.json"))) == 1


def test_recent_share_refresh_includes_existing_dates_and_holes():
    dates = list(pd.to_datetime(["20261023", "20261026", "20261027", "20261028"]))
    keys = {"20261023", "20261027", "20261028"}
    daily, shares, _ = _missing_dates_for_update(daily_keys=keys, share_keys=keys, trade_dates=dates, refresh=False, full_check=False, recent_share_days=2)
    assert daily == [pd.Timestamp("20261026")]
    assert shares == list(pd.to_datetime(["20261026", "20261027", "20261028"]))


def test_full_check_does_not_claim_to_refresh_existing_rows():
    dates = [pd.Timestamp("20261026")]
    daily, shares, mode = _missing_dates_for_update(daily_keys={"20261026"}, share_keys={"20261026"}, trade_dates=dates, refresh=False, full_check=True)
    assert (daily, shares, mode) == ([], [], "full_check")


def test_failed_atomic_save_keeps_previous_cache(tmp_path, monkeypatch):
    cache = CacheStore(tmp_path)
    cache.save_daily_cross_section("tushare", "etf_share", "20261026", pd.DataFrame({"shares": [1]}))
    path = tmp_path / "tushare/daily_cross_section/etf_share/20261026.csv"
    before = path.read_bytes()
    def fail(self, destination, *args, **kwargs):
        Path(destination).write_text("partial")
        raise OSError("disk interrupted")
    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
    with pytest.raises(OSError):
        cache.save_daily_cross_section("tushare", "etf_share", "20261026", pd.DataFrame({"shares": [2]}))
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_sz_history_requires_evidence_and_uses_exact_date_override():
    raw = [{"ts_code": "159001.SZ", "trade_date": "20240902", "fd_share": 104755.56}]
    with pytest.raises(ShareDataQualityError):
        normalize_fetched_shares(raw, pd.DataFrame())
    evidence = pd.DataFrame([{"fund_code": "159001.SZ", "trade_date": pd.Timestamp("20240902"), "shares": 1227.6827, "source": "ifind:verified_history", "share_unit": ACTUAL_SHARE_UNIT}])
    result = normalize_fetched_shares(raw, evidence)
    assert result.shares.tolist() == [1227.6827]
    assert result.source.tolist() == ["ifind:verified_history"]
    assert raw[0]["fd_share"] == 104755.56


def test_capped_daily_response_retries_documented_exchange_filters():
    class Client:
        def query(self, api, params, fields):
            market = params.get("market")
            if market:
                return [{"ts_code": "510300.SH" if market == "SH" else "159915.SZ", "trade_date": "20230331", "fd_share": 123}]
            return [{"ts_code": str(i) + ".OF", "trade_date": "20230331", "fd_share": 1} for i in range(2000)]
    result = TushareEtfSource(Client())._query_share_rows({"trade_date": "20230331"})
    assert {r["ts_code"] for r in result} == {"510300.SH", "159915.SZ"}


def test_capped_single_code_request_refuses_silent_truncation():
    class Client:
        def query(self, *args, **kwargs):
            return [{}] * 2000
    with pytest.raises(IncompleteMarketCoverageError):
        TushareEtfSource(Client())._query_share_rows({"ts_code": "511880.SH"})


def test_verified_history_supplies_missing_source_rows_without_cross_code_leakage():
    evidence = pd.DataFrame([
        {"fund_code": code, "trade_date": pd.Timestamp(day), "shares": 1200, "source": "ifind:verified_history", "share_unit": ACTUAL_SHARE_UNIT}
        for code in ["159001.SZ", "159003.SZ"] for day in ["20250320", "20250321", "20250324"]
    ])
    raw = [{"ts_code": "510300.SH", "trade_date": "20250321", "fd_share": 100}]
    cross_section = normalize_fetched_shares(raw, evidence, request={"trade_date": "20250321"})
    assert set(cross_section.fund_code) == {"510300.SH", "159001.SZ", "159003.SZ"}
    assert cross_section.trade_date.eq(pd.Timestamp("20250321")).all()
    code_history = normalize_fetched_shares([], evidence, request={"ts_code": "159001.SZ", "start_date": "20250321", "end_date": "20250324"})
    assert set(code_history.fund_code) == {"159001.SZ"}
    assert list(code_history.trade_date) == list(pd.to_datetime(["20250321", "20250324"]))
    ordinary = normalize_fetched_shares([], evidence, request={"ts_code": "510300.SH", "start_date": "20250321", "end_date": "20250324"})
    assert ordinary.empty


def test_source_warmup_uses_official_dates_and_stops_at_cache_holes(tmp_path):
    from etf_flow_monitor.cli import _source_warmup_start_date
    calendar = pd.DataFrame({"cal_date": pd.to_datetime(["20240920", "20240923", "20240924", "20240925"]), "is_open": [1, 0, 1, 1]})
    assert _source_warmup_start_date(calendar, pd.Timestamp("20240925")) == pd.Timestamp("20240920")
    cache = CacheStore(tmp_path)
    for dataset in ["etf_daily", "etf_share"]:
        cache.save_daily_cross_section("tushare", dataset, "20240924", pd.DataFrame({"x":[1]}))
    assert _source_warmup_start_date(calendar, pd.Timestamp("20240925"), cache_dir=tmp_path) == pd.Timestamp("20240924")


def test_cross_border_warmup_restores_first_period_flow():
    from etf_flow_monitor.data.cross_border_fill import fill_cross_border_previous_values
    from etf_flow_monitor.monitor.flow_metrics import build_flow_snapshot
    code = "513100.SH"
    dates = list(pd.to_datetime(["20240924", "20240925", "20240926"]))
    daily = pd.DataFrame([{"fund_code":code,"trade_date":day,"close":1,"amount":1} for day in dates])
    shares = pd.DataFrame([{"fund_code":code,"trade_date":dates[i],"shares":value,"share_unit":ACTUAL_SHARE_UNIT} for i,value in [(0,100),(2,110)]])
    category = pd.DataFrame([{"fund_code":code,"category":"海外"}])
    daily, shares, _ = fill_cross_border_previous_values(daily,shares,category,dates)
    flow = build_flow_snapshot(daily,shares,pd.DataFrame())
    assert flow.loc[flow.trade_date.eq(dates[-1]),'estimated_net_flow'].iloc[0] == 100000
