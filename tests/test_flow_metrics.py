from __future__ import annotations

import pandas as pd

from etf_flow_monitor.monitor.flow_metrics import build_flow_snapshot, build_market_summary


def test_build_flow_snapshot_uses_share_change() -> None:
    daily = pd.DataFrame(
        [
            {"ts_code": "510300.SH", "trade_date": "20260102", "close": 4.0, "vol": 1, "amount": 100},
            {"ts_code": "510300.SH", "trade_date": "20260103", "close": 5.0, "vol": 1, "amount": 200},
        ]
    )
    shares = pd.DataFrame(
        [
            {"ts_code": "510300.SH", "trade_date": "20260102", "fd_share": 1000, "source": "test"},
            {"ts_code": "510300.SH", "trade_date": "20260103", "fd_share": 1200, "source": "test"},
        ]
    )

    flow = build_flow_snapshot(daily, shares)

    last = flow.iloc[-1]
    assert last["share_change"] == 200
    assert last["estimated_net_flow"] == 10_000_000
    assert last["flow_direction"] == "inflow"


def test_build_flow_snapshot_divides_money_fund_quote_by_100() -> None:
    daily = pd.DataFrame(
        [
            {"ts_code": "511880.SH", "trade_date": "20260624", "close": 100.544, "vol": 1, "amount": 21020910.821},
            {"ts_code": "511880.SH", "trade_date": "20260625", "close": 100.545, "vol": 1, "amount": 17248103.242},
        ]
    )
    shares = pd.DataFrame(
        [
            {"ts_code": "511880.SH", "trade_date": "20260624", "fd_share": 8_373_466.0, "source": "test"},
            {"ts_code": "511880.SH", "trade_date": "20260625", "fd_share": 8_250_213.0, "source": "test"},
        ]
    )
    basic = pd.DataFrame([{"fund_code": "511880.SH", "name": "银华货币ETF-A", "fund_type": "货币型"}])

    flow = build_flow_snapshot(daily, shares, basic)

    last = flow.iloc[-1]
    assert last["share_change"] == -123_253
    assert round(last["flow_price"], 5) == 1.00545
    assert round(last["amount"], 2) == 17_248_103_242.00
    assert round(last["estimated_net_flow"], 2) == -1_239_247_288.50
    assert last["flow_direction"] == "outflow"


def test_build_flow_snapshot_keeps_bond_fund_quote_unscaled() -> None:
    daily = pd.DataFrame(
        [
            {"ts_code": "511360.SH", "trade_date": "20260624", "close": 113.586, "vol": 1, "amount": 27376805.627},
            {"ts_code": "511360.SH", "trade_date": "20260625", "close": 113.586, "vol": 1, "amount": 22682714.551},
        ]
    )
    shares = pd.DataFrame(
        [
            {"ts_code": "511360.SH", "trade_date": "20260624", "fd_share": 72_015.66, "source": "test"},
            {"ts_code": "511360.SH", "trade_date": "20260625", "fd_share": 72_012.46, "source": "test"},
        ]
    )
    basic = pd.DataFrame([{"fund_code": "511360.SH", "name": "海富通中证短融ETF", "fund_type": "债券型"}])

    flow = build_flow_snapshot(daily, shares, basic)

    last = flow.iloc[-1]
    assert round(last["share_change"], 2) == -3.20
    assert round(last["flow_price"], 3) == 113.586
    assert round(last["estimated_net_flow"], 2) == -3_634_752.00
    assert last["flow_direction"] == "outflow"


def test_build_market_summary_counts_directions() -> None:
    flow = pd.DataFrame(
        [
            {"fund_code": "A", "trade_date": pd.Timestamp("2026-01-03"), "amount": 100, "estimated_net_flow": 10, "flow_direction": "inflow"},
            {"fund_code": "B", "trade_date": pd.Timestamp("2026-01-03"), "amount": 200, "estimated_net_flow": -20, "flow_direction": "outflow"},
        ]
    )

    summary = build_market_summary(flow)

    assert summary.loc[0, "fund_count"] == 2
    assert summary.loc[0, "total_amount"] == 300
    assert summary.loc[0, "estimated_net_flow"] == -10
    assert summary.loc[0, "inflow_count"] == 1
    assert summary.loc[0, "outflow_count"] == 1
