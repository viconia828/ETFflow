from __future__ import annotations

import pandas as pd

from etf_flow_monitor.monitor.html_dashboard import write_dashboard_html


def test_write_dashboard_html_is_self_contained(tmp_path) -> None:
    summary = pd.DataFrame(
        [
            {
                "trade_date": "2026-06-24",
                "fund_count": 2,
                "total_amount": 300_000_000,
                "estimated_net_flow": -100_000_000,
                "inflow_count": 1,
                "outflow_count": 1,
            },
            {
                "trade_date": "2026-06-25",
                "fund_count": 2,
                "total_amount": 500_000_000,
                "estimated_net_flow": 200_000_000,
                "inflow_count": 1,
                "outflow_count": 1,
            },
        ]
    )
    flow = pd.DataFrame(
        [
            {
                "fund_code": "510300.SH",
                "trade_date": "2026-06-25",
                "name": "沪深300ETF",
                "pct_change": 0.5,
                "amount": 300_000_000,
                "estimated_net_flow": 180_000_000,
            },
            {
                "fund_code": "511010.SH",
                "trade_date": "2026-06-25",
                "name": "国债ETF",
                "pct_change": -0.1,
                "amount": 200_000_000,
                "estimated_net_flow": -80_000_000,
            },
        ]
    )

    output = tmp_path / "etf_flow_dashboard.html"
    write_dashboard_html(output, trade_date="20260625", summary=summary, flow=flow)

    html = output.read_text(encoding="utf-8")
    assert "<style>" in html
    assert "<script>" in html
    assert "data-view-target=\"trend\"" in html
    assert "data-view-target=\"rotation\"" in html
    assert "data-view-target=\"detail\"" in html
    assert "data-view-panel=\"detail\"" in html
    assert "data-period-controls" in html
    assert "name === \"detail\"" in html
    assert "区间累计" in html
    assert "最新" not in html
    assert "2026-06-25 当日估算净额" in html
    assert "请求日期估算净额" not in html
    assert "data-period-net-text=" in html
    assert "data-header-card=\"period-net\"" in html
    assert "流出转流入" in html
    assert "区间净流入" in html
    assert "6月全量方向" in html
    assert "3月与前一等长交易日区间比较" in html
    assert "ETF 资金轮动" in html
    assert "金额（亿元）" in html
    assert "流入前十" in html
    assert "流出前十" in html
    assert "今日 ETF 明细" in html
    assert "口径说明" in html
    assert html.count("今日 ETF 明细") == 1
    assert html.count("口径说明") == 1
    assert "分类维护" not in html
    assert "宽基" in html
    assert "债券" in html
    assert "https://" not in html
    assert "http://" not in html
