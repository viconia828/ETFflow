"""Self-contained HTML dashboard rendering for ETF flow snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Iterable

import pandas as pd


@dataclass(frozen=True, slots=True)
class PeriodSpec:
    key: str
    label: str
    start: pd.Timestamp
    compare_previous: bool = False


DIRECTION_NOTE = (
    "方向口径：最近1日、1周、2周、1月、3月与前一等长交易日区间比较，显示流出转流入、流入转流出、"
    "净流入/净流出扩大或缩小；6月、今年来、12月不做增量比较，只显示该区间全量净流入或净流出。"
)


def write_dashboard_html(
    path: Path,
    *,
    trade_date: object,
    summary: pd.DataFrame,
    flow: pd.DataFrame,
    title: str = "全市场 ETF 净流入走势",
    notes: Iterable[str] = (),
) -> None:
    """Write a shareable one-file HTML dashboard."""

    path.parent.mkdir(parents=True, exist_ok=True)
    date_key = pd.Timestamp(trade_date).normalize()
    summary_frame = _prepare_summary(summary)
    flow_frame = _prepare_flow(flow)
    periods = _period_specs(date_key)
    default_period = "6M"
    html = _render_document(
        title=title,
        trade_date=date_key,
        summary=summary_frame,
        flow=flow_frame,
        periods=periods,
        default_period=default_period,
        notes=notes,
    )
    path.write_text(html, encoding="utf-8")


def _render_document(
    *,
    title: str,
    trade_date: pd.Timestamp,
    summary: pd.DataFrame,
    flow: pd.DataFrame,
    periods: list[PeriodSpec],
    default_period: str,
    notes: Iterable[str],
) -> str:
    period_models = {item.key: _build_period_model(summary, flow, trade_date, item) for item in periods}
    header_model = period_models[default_period]
    period_buttons = "\n".join(
        _render_period_button(item, period_models[item.key], is_active=item.key == default_period)
        for item in periods
    )
    trend_panels = "\n".join(
        _render_trend_period(
            period_models[item.key],
            is_active=item.key == default_period,
        )
        for item in periods
    )
    rotation_panels = "\n".join(
        _render_rotation_period(
            period_models[item.key],
            is_active=item.key == default_period,
        )
        for item in periods
    )
    detail_panels = "\n".join(
        _render_detail_period(
            period_models[item.key],
            is_active=item.key == default_period,
        )
        for item in periods
    )
    note_lines = [str(item).strip() for item in notes if str(item).strip()]
    if not note_lines:
        note_lines = [
            "金额 = Tushare fd_share 逐日变化（万份）× 10,000 × 资金流价格；普通 ETF 使用场内收盘价，100 元附近报价的货币 ETF 使用收盘价 / 100。",
            "成交额 = Tushare fund_daily.amount × 1,000，统一换算为元后再在页面显示为亿元。",
        ]
    if DIRECTION_NOTE not in note_lines:
        note_lines.append(DIRECTION_NOTE)
    notes_html = "\n".join(f"<li>{escape(item)}</li>" for item in note_lines)
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} {trade_date.strftime("%Y-%m-%d")}</title>
  <style>
{_stylesheet()}
  </style>
</head>
<body>
  <main class="dashboard-shell">
    <header class="hero">
      <div>
        <h1>{escape(title)}</h1>
        <p>截至 {trade_date.strftime("%Y-%m-%d")} · 生成时间 {escape(generated_at)}</p>
      </div>
      <div class="hero-actions" aria-label="页面操作">
        <button class="icon-button" type="button" title="打印或保存为 PDF" onclick="window.print()"><span class="camera-icon"></span></button>
        <button class="icon-button" type="button" title="关闭窗口" onclick="window.close()"><span class="close-icon"></span></button>
      </div>
    </header>

    <section class="metric-strip" aria-label="核心指标">
      {_metric_card("区间累计", header_model["period_net_yi"], "亿元", f"{header_model['period'].label}累计估算净额", header_key="period-net")}
      {_metric_card("当日", header_model["daily_net_yi"], "亿元", header_model["daily_caption"], header_key="daily-net")}
      {_text_card("当前方向", header_model["direction"], header_model["direction_class"], header_model["direction_caption"], header_key="direction")}
      {_text_card("最大去向", header_model["destination"], _value_class(header_model["destination_value_yi"]), f"{header_model['period'].label}最大去向", header_key="destination")}
    </section>

    <nav class="view-tabs" aria-label="视图切换">
      <button class="view-button is-active" type="button" data-view-target="trend">走势</button>
      <button class="view-button" type="button" data-view-target="rotation">轮动</button>
      <button class="view-button" type="button" data-view-target="detail">明细</button>
    </nav>

    <section class="control-row" aria-label="区间">
      <div class="period-buttons">{period_buttons}</div>
    </section>

    <section class="view-panel is-active" data-view-panel="trend">
      <div class="trend-view-layout">
        <div class="trend-panel-stack">{trend_panels}</div>
        {_render_trend_static_summary(periods, period_models)}
      </div>
      <section class="notes-section">
        <h2>口径说明</h2>
        <ul>{notes_html}</ul>
      </section>
    </section>

    <section class="view-panel" data-view-panel="rotation">
      {rotation_panels}
    </section>

    <section class="view-panel detail-view" data-view-panel="detail">
      {detail_panels}
    </section>
  </main>
  <script>
{_script()}
  </script>
</body>
</html>
"""


def _build_period_model(summary: pd.DataFrame, flow: pd.DataFrame, trade_date: pd.Timestamp, period: PeriodSpec) -> dict:
    summary_period = _filter_period(summary, period.start, trade_date)
    flow_period = _filter_period(flow, period.start, trade_date)
    summary_exact = summary.loc[summary["trade_date"].eq(trade_date)] if not summary.empty else summary
    daily_net = float(summary_exact["estimated_net_flow"].sum()) if not summary_exact.empty else 0.0
    period_net = float(summary_period["estimated_net_flow"].sum()) if not summary_period.empty else 0.0
    trend_rows = _trend_rows(summary_period)
    category_rows = _category_rows(flow_period)
    inflow_total = sum(item["value_yi"] for item in category_rows if item["value_yi"] > 0)
    outflow_total = abs(sum(item["value_yi"] for item in category_rows if item["value_yi"] < 0))
    net_yi = inflow_total - outflow_total
    destination, destination_value = _largest_destination(category_rows)
    direction, direction_class, direction_caption = _direction_model(
        summary=summary,
        summary_period=summary_period,
        period=period,
        period_net=period_net,
    )
    return {
        "period": period,
        "summary": summary_period,
        "flow": flow_period,
        "trend_rows": trend_rows,
        "category_rows": category_rows,
        "detail_flow": _detail_rows(flow_period),
        "daily_net_yi": _to_yi(daily_net),
        "period_net_yi": _to_yi(period_net),
        "inflow_total_yi": inflow_total,
        "outflow_total_yi": outflow_total,
        "net_yi": net_yi,
        "destination": destination,
        "destination_value_yi": destination_value,
        "direction": direction,
        "direction_class": direction_class,
        "direction_caption": direction_caption,
        "daily_caption": f"{trade_date.strftime('%Y-%m-%d')} 当日估算净额",
        "insights": _insights(category_rows, inflow_total, outflow_total, net_yi),
    }


def _render_trend_period(model: dict, *, is_active: bool) -> str:
    period = model["period"]
    active_class = " is-active" if is_active else ""
    return f"""
      <div class="period-panel{active_class}" data-period-panel="{escape(period.key)}">
        <article class="chart-card trend-card">
          <div class="section-title">
            <div>
              <h2>滚动 {escape(period.label)} 净额</h2>
              <p class="section-caption">金额单位：亿元，负值代表资金净流出</p>
            </div>
          </div>
          {_render_trend_svg(model["trend_rows"])}
          <div class="chart-footnote">金额 = 逐日份额变动 × 资金流价格累计；切换到“轮动”查看分类去向。</div>
        </article>
      </div>
"""


def _render_trend_static_summary(periods: list[PeriodSpec], period_models: dict[str, dict]) -> str:
    rows = []
    for period in periods:
        model = period_models[period.key]
        value = float(model["period_net_yi"])
        value_class = _value_class(value)
        direction = "净流入" if value > 0 else "净流出" if value < 0 else "持平"
        rows.append(
            '<div class="trend-summary-row">'
            f'<span class="trend-summary-label">{escape(period.label)}</span>'
            f'<span class="trend-summary-direction {value_class}">{escape(direction)}</span>'
            f'<strong class="trend-summary-value {value_class}">{escape(_signed_number(value))}</strong>'
            "</div>"
        )
    return f"""
        <aside class="trend-summary-card" aria-label="区间流入流出静态展示">
          <h2>区间流向</h2>
          <p class="section-caption">单位：亿元</p>
          <div class="trend-summary-list">{"".join(rows)}</div>
        </aside>
"""


def _render_rotation_period(model: dict, *, is_active: bool) -> str:
    period = model["period"]
    active_class = " is-active" if is_active else ""
    return f"""
      <div class="period-panel{active_class}" data-period-panel="{escape(period.key)}">
        <article class="chart-card">
          <div class="rotation-header">
            <div>
              <h2>ETF 资金轮动（单位：亿元）</h2>
              <p class="section-caption">{escape(period.label)} 分类净额，未映射品种归入“其他”</p>
            </div>
            <div class="rotation-stats">
              {_compact_stat("流出合计", -model["outflow_total_yi"])}
              {_compact_stat("流入合计", model["inflow_total_yi"])}
              {_compact_stat("净流入", model["net_yi"])}
              {_compact_label("最大去向", model["destination"], model["destination_value_yi"])}
            </div>
          </div>
          {_render_rotation_chart(model["category_rows"])}
        </article>
        <section class="insight-card">
          <h2>解读</h2>
          {_render_insights(model["insights"])}
        </section>
      </div>
"""


def _render_detail_period(model: dict, *, is_active: bool) -> str:
    period = model["period"]
    active_class = " is-active" if is_active else ""
    return f"""
      <div class="period-panel{active_class}" data-period-panel="{escape(period.key)}">
        <section class="detail-section">
          <div class="section-title">
            <h2>{escape(period.label)} ETF 明细</h2>
            <p class="section-caption">按区间累计估算净额排序，流入与流出各保留前十</p>
          </div>
          {_render_detail_table(model["detail_flow"])}
        </section>
      </div>
"""


def _render_period_button(period: PeriodSpec, model: dict, *, is_active: bool) -> str:
    active_class = " is-active" if is_active else ""
    destination_class = _value_class(model["destination_value_yi"])
    return (
        f'<button class="period-button{active_class}" type="button" data-period-target="{escape(period.key)}" '
        f'data-period-net-text="{escape(_signed_number(model["period_net_yi"]))}" '
        f'data-period-net-class="{escape(_value_class(model["period_net_yi"]))}" '
        f'data-period-caption="{escape(period.label)}累计估算净额" '
        f'data-daily-net-text="{escape(_signed_number(model["daily_net_yi"]))}" '
        f'data-daily-net-class="{escape(_value_class(model["daily_net_yi"]))}" '
        f'data-daily-caption="{escape(str(model["daily_caption"]))}" '
        f'data-direction-text="{escape(str(model["direction"]))}" '
        f'data-direction-class="{escape(str(model["direction_class"]))}" '
        f'data-direction-caption="{escape(str(model["direction_caption"]))}" '
        f'data-destination-text="{escape(str(model["destination"]))}" '
        f'data-destination-class="{escape(destination_class)}" '
        f'data-destination-caption="{escape(period.label)}最大去向">'
        f"{escape(period.label)}</button>"
    )


def _render_trend_svg(rows: list[dict[str, object]]) -> str:
    width = 1000
    height = 390
    left = 108
    right = 28
    top = 34
    bottom = 48
    plot_width = width - left - right
    plot_height = height - top - bottom
    if not rows:
        return f"""
          <svg class="trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="暂无趋势数据">
            <rect class="chart-bg" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" rx="4"></rect>
            <text class="empty-label" x="{width / 2:.1f}" y="{height / 2:.1f}" text-anchor="middle">暂无数据</text>
          </svg>
"""

    values = [float(item["cum_yi"]) for item in rows]
    min_y = min(0.0, min(values))
    max_y = max(0.0, max(values))
    if min_y == max_y:
        pad = max(abs(min_y), 1.0)
        min_y -= pad
        max_y += pad
    else:
        pad = (max_y - min_y) * 0.12
        min_y -= pad
        max_y += pad

    def x_at(index: int) -> float:
        if len(rows) == 1:
            return left + plot_width
        return left + plot_width * index / (len(rows) - 1)

    def y_at(value: float) -> float:
        return top + (max_y - value) / (max_y - min_y) * plot_height

    points = [(x_at(index), y_at(float(item["cum_yi"]))) for index, item in enumerate(rows)]
    line_path = " ".join(("M" if index == 0 else "L") + f" {x:.2f} {y:.2f}" for index, (x, y) in enumerate(points))
    zero_y = y_at(0.0)
    area_path = f"M {points[0][0]:.2f} {zero_y:.2f} " + " ".join(f"L {x:.2f} {y:.2f}" for x, y in points)
    area_path += f" L {points[-1][0]:.2f} {zero_y:.2f} Z"
    tick_values = [min_y + (max_y - min_y) * idx / 4 for idx in range(5)]
    grid = "\n".join(
        f'<line class="grid-line" x1="{left}" y1="{y_at(value):.2f}" x2="{left + plot_width}" y2="{y_at(value):.2f}"></line>'
        f'<text class="axis-label y-label" x="{left - 16}" y="{y_at(value) + 4:.2f}" text-anchor="end">{escape(_axis_text(value))}</text>'
        for value in tick_values
    )
    label_indexes = _label_indexes(len(rows), 5)
    x_labels = "\n".join(
        f'<text class="axis-label" x="{x_at(index):.2f}" y="{height - 15}" text-anchor="middle">{escape(str(rows[index]["label"]))}</text>'
        for index in label_indexes
    )
    last_x, last_y = points[-1]
    return f"""
          <svg class="trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="ETF 净流入趋势图">
            <defs>
              <linearGradient id="trendAreaGradient" x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stop-color="#21c8d8" stop-opacity="0.34"></stop>
                <stop offset="100%" stop-color="#21c8d8" stop-opacity="0.02"></stop>
              </linearGradient>
            </defs>
            <rect class="chart-bg" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" rx="4"></rect>
            {grid}
            <line class="zero-line" x1="{left}" y1="{zero_y:.2f}" x2="{left + plot_width}" y2="{zero_y:.2f}"></line>
            <path class="trend-area" d="{area_path}"></path>
            <path class="trend-line" d="{line_path}"></path>
            <circle class="trend-point" cx="{last_x:.2f}" cy="{last_y:.2f}" r="5"></circle>
            {x_labels}
          </svg>
"""


def _render_rotation_chart(rows: list[dict[str, object]]) -> str:
    if not rows:
        return '<div class="empty-state">暂无分类轮动数据</div>'
    negative_rows = [item for item in rows if float(item["value_yi"]) < 0][:8]
    positive_rows = [item for item in rows if float(item["value_yi"]) > 0][:8]
    max_abs = max([abs(float(item["value_yi"])) for item in rows] + [1.0])
    return f"""
          <div class="rotation-grid">
            <div class="rotation-side rotation-side-left">
              {_rotation_rows(negative_rows, max_abs, is_positive=False)}
            </div>
            <div class="rotation-axis"></div>
            <div class="rotation-side rotation-side-right">
              {_rotation_rows(positive_rows, max_abs, is_positive=True)}
            </div>
          </div>
"""


def _rotation_rows(rows: list[dict[str, object]], max_abs: float, *, is_positive: bool) -> str:
    if not rows:
        return '<div class="empty-side">无明显流入</div>' if is_positive else '<div class="empty-side">无明显流出</div>'
    side = "positive" if is_positive else "negative"
    parts = []
    for item in rows:
        value = float(item["value_yi"])
        width = max(2.0, min(100.0, abs(value) / max_abs * 100))
        label = escape(str(item["category"]))
        value_text = escape(_signed_number(value))
        parts.append(
            f'<div class="rotation-row {side}">'
            f'<span class="rotation-label">{label}</span>'
            f'<span class="rotation-value">{value_text}</span>'
            f'<span class="rotation-track"><span class="rotation-bar" style="width: {width:.2f}%"></span></span>'
            f"</div>"
        )
    return "\n".join(parts)


def _render_detail_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return '<div class="empty-state">暂无区间 ETF 明细数据</div>'
    working = frame.copy()
    working["estimated_net_flow"] = pd.to_numeric(working.get("estimated_net_flow"), errors="coerce").fillna(0.0)
    working["amount"] = pd.to_numeric(working.get("amount"), errors="coerce").fillna(0.0)
    inflow = working.loc[working["estimated_net_flow"].gt(0)].sort_values(
        ["estimated_net_flow", "amount"],
        ascending=[False, False],
        kind="stable",
    ).head(10)
    outflow = working.loc[working["estimated_net_flow"].lt(0)].sort_values(
        ["estimated_net_flow", "amount"],
        ascending=[True, False],
        kind="stable",
    ).head(10)
    return (
        '<div class="detail-leaderboards">'
        + _render_detail_rank_table("流入前十", inflow, "暂无流入 ETF")
        + _render_detail_rank_table("流出前十", outflow, "暂无流出 ETF")
        + "</div>"
    )


def _render_detail_rank_table(title: str, frame: pd.DataFrame, empty_message: str) -> str:
    if frame.empty:
        return f"""
        <div class="detail-board">
          <h3>{escape(title)}</h3>
          <div class="empty-state">{escape(empty_message)}</div>
        </div>
"""

    rows = []
    for rank, (_, row) in enumerate(frame.iterrows(), start=1):
        flow_yi = _to_yi(row.get("estimated_net_flow", 0.0))
        amount_yi = _to_yi(row.get("amount", 0.0))
        pct_change = row.get("pct_change", 0.0)
        try:
            pct_number = float(pct_change)
            pct_text = "" if pd.isna(pct_number) else f"{pct_number:+.2f}%"
        except (TypeError, ValueError):
            pct_text = ""
        flow_class = _value_class(flow_yi)
        category_label = _category_display(row)
        rows.append(
            "<tr>"
            f'<td class="rank-cell">{rank}</td>'
            f"<td>{escape(str(row.get('fund_code', '')))}</td>"
            f"<td>{escape(str(row.get('name', '') or ''))}</td>"
            f'<td class="category-cell">{escape(category_label)}</td>'
            f"<td>{escape(pct_text)}</td>"
            f"<td>{escape(_plain_number(amount_yi))}</td>"
            f'<td class="{flow_class}">{escape(_signed_number(flow_yi))}</td>'
            "</tr>"
        )
    return f"""
      <div class="detail-board">
        <h3>{escape(title)}</h3>
        <div class="table-wrap">
        <table>
          <thead>
            <tr><th>#</th><th>代码</th><th>名称</th><th>分类</th><th>区间涨跌幅</th><th>成交额（亿）</th><th>估算净额（亿）</th></tr>
          </thead>
          <tbody>{"".join(rows)}</tbody>
        </table>
        </div>
      </div>
"""


def _category_display(row: pd.Series) -> str:
    category = _category_label(row)
    subcategory = str(row.get("subcategory", "") or "").strip()
    if subcategory and subcategory != category:
        return f"{category} / {subcategory}"
    return category


def _category_label(row: pd.Series) -> str:
    category = str(row.get("category", "") or "").strip() or "其他"
    return category


def _render_insights(items: list[str]) -> str:
    if not items:
        return '<div class="empty-state">暂无足够数据生成解读</div>'
    return '<ol class="insight-list">' + "".join(f"<li><span>{index}</span>{escape(item)}</li>" for index, item in enumerate(items, start=1)) + "</ol>"


def _metric_card(label: str, value: float, unit: str, caption: str, *, header_key: str | None = None) -> str:
    value_class = _value_class(value)
    article_attrs = f' data-header-card="{escape(header_key)}"' if header_key else ""
    value_attrs = " data-header-value" if header_key else ""
    caption_attrs = " data-header-caption" if header_key else ""
    return (
        f'<article class="metric-card"{article_attrs}>'
        f"<span>{escape(label)}</span>"
        f'<strong class="{value_class}"{value_attrs}>{escape(_signed_number(value))}</strong>'
        f"<em>{escape(unit)}</em>"
        f"<small{caption_attrs}>{escape(caption)}</small>"
        "</article>"
    )


def _text_card(label: str, value: str, value_class: str, caption: str = "自动识别", *, header_key: str | None = None) -> str:
    article_attrs = f' data-header-card="{escape(header_key)}"' if header_key else ""
    value_attrs = " data-header-value" if header_key else ""
    caption_attrs = " data-header-caption" if header_key else ""
    return (
        f'<article class="metric-card"{article_attrs}>'
        f"<span>{escape(label)}</span>"
        f'<strong class="{escape(value_class)}"{value_attrs}>{escape(value)}</strong>'
        f"<small{caption_attrs}>{escape(caption)}</small>"
        "</article>"
    )


def _compact_stat(label: str, value: float) -> str:
    return f'<div class="compact-stat"><span>{escape(label)}</span><strong class="{_value_class(value)}">{escape(_signed_number(value))}</strong></div>'


def _compact_label(label: str, value: str, signed_value: float) -> str:
    return f'<div class="compact-stat"><span>{escape(label)}</span><strong class="{_value_class(signed_value)}">{escape(value)}</strong></div>'


def _prepare_summary(frame: pd.DataFrame | None) -> pd.DataFrame:
    columns = ["trade_date", "fund_count", "total_amount", "estimated_net_flow", "inflow_count", "outflow_count"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.copy()
    for column in columns:
        if column not in working.columns:
            working[column] = pd.NA
    working = working[columns].copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce").dt.normalize()
    for column in ("fund_count", "total_amount", "estimated_net_flow", "inflow_count", "outflow_count"):
        working[column] = pd.to_numeric(working[column], errors="coerce").fillna(0.0)
    return working[working["trade_date"].notna()].sort_values("trade_date", kind="stable").reset_index(drop=True)


def _prepare_flow(frame: pd.DataFrame | None) -> pd.DataFrame:
    columns = [
        "fund_code",
        "trade_date",
        "name",
        "close",
        "pct_change",
        "amount",
        "estimated_net_flow",
        "category",
        "subcategory",
        "review_note",
        "category_source",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    working = frame.copy()
    for column in columns:
        if column not in working.columns:
            working[column] = pd.NA
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce").dt.normalize()
    for column in ("close", "pct_change"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    for column in ("amount", "estimated_net_flow"):
        working[column] = pd.to_numeric(working[column], errors="coerce").fillna(0.0)
    working["fund_code"] = working["fund_code"].fillna("").astype(str).str.upper()
    working["name"] = working["name"].fillna("").astype(str)
    working["subcategory"] = working["subcategory"].fillna("").astype(str)
    working["review_note"] = working["review_note"].fillna("").astype(str)
    working["category_source"] = working["category_source"].fillna("").astype(str)
    categories: list[str] = []
    for code, name, category in zip(working["fund_code"], working["name"], working["category"], strict=False):
        if not pd.isna(category):
            text = str(category).strip()
            if text and text.lower() not in {"nan", "<na>", "none"}:
                categories.append(text)
                continue
        categories.append(_infer_category(code, name))
    working["category"] = categories
    return working[working["trade_date"].notna()].sort_values(["trade_date", "fund_code"], kind="stable").reset_index(drop=True)


def _filter_period(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    return frame.loc[dates.ge(start) & dates.le(end)].copy().reset_index(drop=True)


def _trend_rows(summary: pd.DataFrame) -> list[dict[str, object]]:
    if summary.empty:
        return []
    working = summary.sort_values("trade_date", kind="stable").copy()
    working["daily_yi"] = working["estimated_net_flow"].map(_to_yi)
    working["cum_yi"] = working["daily_yi"].cumsum()
    return [
        {
            "date": row.trade_date,
            "label": pd.Timestamp(row.trade_date).strftime("%m-%d"),
            "daily_yi": float(row.daily_yi),
            "cum_yi": float(row.cum_yi),
        }
        for row in working.itertuples(index=False)
    ]


def _category_rows(flow: pd.DataFrame) -> list[dict[str, object]]:
    if flow.empty:
        return []
    grouped = (
        flow.groupby("category", as_index=False)
        .agg(estimated_net_flow=("estimated_net_flow", "sum"), amount=("amount", "sum"), fund_count=("fund_code", "nunique"))
        .copy()
    )
    grouped["value_yi"] = grouped["estimated_net_flow"].map(_to_yi)
    grouped = grouped.loc[grouped["value_yi"].abs().gt(0)]
    if grouped.empty:
        return []
    negative = grouped.loc[grouped["value_yi"].lt(0)].sort_values("value_yi", kind="stable")
    positive = grouped.loc[grouped["value_yi"].gt(0)].sort_values("value_yi", ascending=False, kind="stable")
    ordered = pd.concat([negative, positive], ignore_index=True)
    return [
        {
            "category": str(row.category),
            "value_yi": float(row.value_yi),
            "amount_yi": _to_yi(row.amount),
            "fund_count": int(row.fund_count),
        }
        for row in ordered.itertuples(index=False)
    ]


def _detail_rows(flow: pd.DataFrame) -> pd.DataFrame:
    if flow.empty:
        return flow.copy()
    working = flow.sort_values(["trade_date", "fund_code"], kind="stable").copy()
    working["estimated_net_flow"] = pd.to_numeric(working.get("estimated_net_flow"), errors="coerce").fillna(0.0)
    working["amount"] = pd.to_numeric(working.get("amount"), errors="coerce").fillna(0.0)
    working["close"] = pd.to_numeric(working.get("close"), errors="coerce")
    working["pct_change"] = pd.to_numeric(working.get("pct_change"), errors="coerce")
    grouped = (
        working.groupby("fund_code", as_index=False, sort=False)
        .agg(
            trade_date=("trade_date", "max"),
            name=("name", _last_non_empty),
            pct_change=("pct_change", _period_pct_change),
            pct_change_count=("pct_change", "count"),
            first_close=("close", "first"),
            last_close=("close", "last"),
            amount=("amount", "sum"),
            estimated_net_flow=("estimated_net_flow", "sum"),
            category=("category", _last_non_empty),
            subcategory=("subcategory", _last_non_empty),
        )
        .copy()
    )
    fallback_mask = grouped["pct_change_count"].eq(0) & grouped["first_close"].gt(0) & grouped["last_close"].gt(0)
    grouped.loc[fallback_mask, "pct_change"] = (
        grouped.loc[fallback_mask, "last_close"] / grouped.loc[fallback_mask, "first_close"] - 1.0
    ) * 100.0
    grouped = grouped.drop(columns=["pct_change_count", "first_close", "last_close"])
    return grouped.sort_values(["estimated_net_flow", "amount"], ascending=[False, False], kind="stable").reset_index(drop=True)


def _period_pct_change(values: pd.Series) -> float:
    daily = pd.to_numeric(values, errors="coerce").dropna()
    if daily.empty:
        return float("nan")
    compounded = (daily / 100.0 + 1.0).prod() - 1.0
    return float(compounded * 100.0)


def _last_non_empty(values: pd.Series) -> str:
    for value in reversed(values.tolist()):
        if pd.isna(value):
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "<na>", "none"}:
            return text
    return ""


def _period_specs(end: pd.Timestamp) -> list[PeriodSpec]:
    return [
        PeriodSpec("1D", "最近1日", end, True),
        PeriodSpec("1W", "1周", end - pd.Timedelta(days=7), True),
        PeriodSpec("2W", "2周", end - pd.Timedelta(days=14), True),
        PeriodSpec("1M", "1月", end - pd.DateOffset(months=1), True),
        PeriodSpec("3M", "3月", end - pd.DateOffset(months=3), True),
        PeriodSpec("6M", "6月", end - pd.DateOffset(months=6)),
        PeriodSpec("YTD", "今年来", pd.Timestamp(year=end.year, month=1, day=1)),
        PeriodSpec("12M", "12月", end - pd.DateOffset(months=12)),
    ]


def _infer_category(code: str, name: str) -> str:
    text = f"{code} {name}".upper()
    rules = [
        ("债券", ("债", "国债", "政金", "信用", "转债", "短融", "城投")),
        ("货币", ("货币", "现金", "保证金", "银华日利", "添益", "理财")),
        ("港股", ("港股", "港股通", "沪港深", "恒生", "恒指", "H股", "香港")),
        ("海外", ("QDII", "跨境", "海外", "中概", "纳指", "纳斯达克", "标普", "日经", "德国", "法国", "印度", "沙特", "巴西", "韩国", "新兴亚洲", "东南亚")),
        ("科技", ("科技", "芯片", "半导体", "人工智能", "AI", "软件", "计算机", "通信", "电子", "互联网", "数字")),
        ("新能源制造", ("新能源", "电池", "光伏", "锂", "稀土", "机器人", "智能车", "汽车", "军工", "机械", "装备", "制造")),
        ("商品", ("黄金", "白银", "有色", "商品", "能源化工", "豆粕", "煤炭", "钢铁", "石油", "原油", "油气", "铜")),
        ("金融地产", ("金融", "银行", "证券", "保险", "地产", "房地产")),
        ("红利价值", ("红利", "价值", "低波", "央企", "股息", "高股息")),
        ("医药", ("医药", "医疗", "创新药", "生物", "疫苗", "中药")),
        ("宽基", ("沪深300", "中证500", "中证1000", "中证2000", "创业板", "科创50", "上证50", "A500", "中证A50", "深证100", "MSCI")),
    ]
    for category, keywords in rules:
        if any(keyword.upper() in text for keyword in keywords):
            return category
    return "其他"


def _largest_destination(rows: list[dict[str, object]]) -> tuple[str, float]:
    positive_rows = [item for item in rows if float(item["value_yi"]) > 0]
    if positive_rows:
        winner = max(positive_rows, key=lambda item: float(item["value_yi"]))
        return str(winner["category"]), float(winner["value_yi"])
    if rows:
        winner = max(rows, key=lambda item: abs(float(item["value_yi"])))
        return str(winner["category"]), float(winner["value_yi"])
    return "暂无", 0.0


def _direction_model(
    *,
    summary: pd.DataFrame,
    summary_period: pd.DataFrame,
    period: PeriodSpec,
    period_net: float,
) -> tuple[str, str, str]:
    current_yi = _to_yi(period_net)
    if not period.compare_previous:
        label, value_class = _absolute_direction(current_yi)
        return label, value_class, f"{period.label}全量方向"

    previous_net = _previous_equal_period_net(summary, summary_period)
    if previous_net is None:
        label, value_class = _absolute_direction(current_yi)
        return label, value_class, f"{period.label}暂无上一等长区间"

    label, value_class = _comparison_direction(current_yi, _to_yi(previous_net))
    return label, value_class, f"{period.label}较上一等长区间"


def _previous_equal_period_net(summary: pd.DataFrame, summary_period: pd.DataFrame) -> float | None:
    if summary.empty or summary_period.empty:
        return None
    current_dates = pd.to_datetime(summary_period["trade_date"], errors="coerce").dropna().dt.normalize().drop_duplicates().sort_values()
    if current_dates.empty:
        return None
    current_count = len(current_dates)
    current_start = current_dates.iloc[0]
    working = summary.copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce").dt.normalize()
    previous = working.loc[working["trade_date"].lt(current_start)].sort_values("trade_date", kind="stable").tail(current_count)
    if len(previous) < current_count:
        return None
    return float(pd.to_numeric(previous["estimated_net_flow"], errors="coerce").fillna(0.0).sum())


def _absolute_direction(value_yi: float) -> tuple[str, str]:
    sign = _flow_sign(value_yi)
    if sign > 0:
        return "区间净流入", "flow-positive"
    if sign < 0:
        return "区间净流出", "flow-negative"
    return "区间震荡", "flow-neutral"


def _comparison_direction(current_yi: float, previous_yi: float) -> tuple[str, str]:
    current_sign = _flow_sign(current_yi)
    previous_sign = _flow_sign(previous_yi)
    threshold = 0.01
    if current_sign > 0 and previous_sign < 0:
        return "流出转流入", "flow-positive"
    if current_sign < 0 and previous_sign > 0:
        return "流入转流出", "flow-negative"
    if current_sign > 0:
        if previous_sign <= 0 or current_yi > previous_yi + threshold:
            return "净流入扩大", "flow-positive"
        if current_yi < previous_yi - threshold:
            return "净流入缩小", "flow-negative"
        return "净流入持平", "flow-neutral"
    if current_sign < 0:
        if previous_sign >= 0 or abs(current_yi) > abs(previous_yi) + threshold:
            return "净流出扩大", "flow-negative"
        if abs(current_yi) < abs(previous_yi) - threshold:
            return "净流出缩小", "flow-positive"
        return "净流出持平", "flow-neutral"
    if previous_sign > 0:
        return "净流入缩小", "flow-negative"
    if previous_sign < 0:
        return "净流出缩小", "flow-positive"
    return "资金震荡", "flow-neutral"


def _flow_sign(value_yi: float) -> int:
    try:
        value = float(value_yi)
    except (TypeError, ValueError):
        value = 0.0
    if value > 0.01:
        return 1
    if value < -0.01:
        return -1
    return 0


def _insights(rows: list[dict[str, object]], inflow_total: float, outflow_total: float, net_yi: float) -> list[str]:
    if not rows:
        return []
    total_abs = inflow_total + outflow_total
    items: list[str] = []
    if total_abs > 0:
        if outflow_total >= inflow_total:
            items.append(f"主要流出侧合计占分类资金轮动约 {outflow_total / total_abs:.0%}。")
        else:
            items.append(f"主要流入侧合计占分类资金轮动约 {inflow_total / total_abs:.0%}。")
    destination, destination_value = _largest_destination(rows)
    if destination != "暂无":
        verb = "承接" if destination_value > 0 else "流出"
        items.append(f"{destination}{verb}最明显，区间估算净额为 {_signed_number(destination_value)} 亿元。")
        if destination in {"债券", "货币"} and destination_value > 0:
            items.append("防御资产吸收主要资金，短期风险偏好偏谨慎。")
        elif destination in {"科技", "新能源制造"} and destination_value > 0:
            items.append("成长方向承接较强，需要结合成交额和持续性再确认。")
        elif destination == "宽基" and destination_value > 0:
            items.append("宽基承接增强，指数型资金可能是主要增量来源。")
    if len(items) < 3:
        items.append(f"区间全市场 ETF 估算净额为 {_signed_number(net_yi)} 亿元。")
    return items[:3]


def _to_yi(value: object) -> float:
    try:
        return float(value) / 100_000_000.0
    except (TypeError, ValueError):
        return 0.0


def _signed_number(value: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number) >= 1000:
        text = f"{number:,.0f}"
    elif abs(number) >= 100:
        text = f"{number:,.1f}"
    else:
        text = f"{number:,.2f}"
    return text if number < 0 else f"+{text}" if number > 0 else "0.00"


def _plain_number(value: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    if abs(number) >= 100:
        return f"{number:,.1f}"
    return f"{number:,.2f}"


def _axis_text(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.0f}"
    return f"{value:,.1f}"


def _value_class(value: float) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number > 0:
        return "flow-positive"
    if number < 0:
        return "flow-negative"
    return "flow-neutral"


def _label_indexes(length: int, count: int) -> list[int]:
    if length <= 0:
        return []
    if length <= count:
        return list(range(length))
    indexes = {round((length - 1) * idx / (count - 1)) for idx in range(count)}
    return sorted(int(item) for item in indexes)


def _stylesheet() -> str:
    return r"""
:root {
  color-scheme: dark;
  --bg: #070b10;
  --panel: #111418;
  --panel-soft: #151922;
  --line: #29313b;
  --line-soft: #1f2630;
  --text: #f3f6fb;
  --muted: #9aa4b2;
  --cyan: #26c6da;
  --cyan-soft: rgba(38, 198, 218, 0.18);
  --positive: #f05266;
  --negative: #31c48d;
  --neutral: #b7c0cc;
  --shadow: 0 18px 60px rgba(0, 0, 0, 0.35);
}
* { box-sizing: border-box; }
html, body { min-height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif;
  letter-spacing: 0;
}
button, table { font: inherit; }
.dashboard-shell {
  width: min(1600px, calc(100vw - 32px));
  margin: 16px auto;
  padding: 18px;
  border: 1px solid var(--line-soft);
  background: #090d13;
}
.hero {
  min-height: 142px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  padding: 34px 38px;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
.hero h1 {
  margin: 0;
  font-size: clamp(30px, 3.2vw, 46px);
  line-height: 1.05;
  font-weight: 900;
}
.hero p {
  margin: 14px 0 0;
  color: var(--muted);
  font-size: 20px;
  font-weight: 700;
}
.hero-actions { display: flex; gap: 14px; }
.icon-button {
  width: 54px;
  height: 54px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  color: var(--text);
  background: #191d27;
  cursor: pointer;
}
.icon-button:hover { border-color: var(--cyan); color: var(--cyan); }
.camera-icon, .close-icon {
  position: relative;
  display: block;
  width: 24px;
  height: 20px;
}
.camera-icon::before {
  content: "";
  position: absolute;
  inset: 4px 1px 1px;
  border: 3px solid currentColor;
  border-radius: 5px;
}
.camera-icon::after {
  content: "";
  position: absolute;
  width: 7px;
  height: 7px;
  left: 8px;
  top: 9px;
  border: 2px solid currentColor;
  border-radius: 50%;
}
.close-icon::before, .close-icon::after {
  content: "";
  position: absolute;
  left: 11px;
  top: 1px;
  width: 3px;
  height: 24px;
  background: currentColor;
  border-radius: 2px;
}
.close-icon::before { transform: rotate(45deg); }
.close-icon::after { transform: rotate(-45deg); }
.metric-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin: 18px 0;
}
.metric-card {
  min-height: 116px;
  padding: 22px 24px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.metric-card span, .compact-stat span {
  display: block;
  color: var(--muted);
  font-weight: 800;
}
.metric-card strong {
  display: inline-block;
  margin-top: 10px;
  font-size: clamp(27px, 2.8vw, 42px);
  line-height: 1;
  font-weight: 900;
}
.metric-card em {
  margin-left: 8px;
  color: var(--muted);
  font-style: normal;
  font-weight: 800;
}
.metric-card small {
  display: block;
  margin-top: 12px;
  color: #6f7b8a;
  font-size: 13px;
}
.view-tabs, .control-row {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 16px;
  margin: 14px 0;
}
.view-tabs { justify-content: flex-start; }
.view-button, .period-button {
  height: 50px;
  padding: 0 22px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #171b24;
  color: var(--muted);
  font-weight: 900;
  cursor: pointer;
}
.view-button.is-active, .period-button.is-active {
  background: var(--cyan);
  color: #ffffff;
  border-color: #38d2e0;
  box-shadow: 0 0 0 1px rgba(38, 198, 218, 0.24), 0 8px 28px rgba(38, 198, 218, 0.16);
}
.period-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.view-panel, .period-panel { display: none; }
.view-panel.is-active, .period-panel.is-active { display: block; }
.trend-view-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 310px;
  gap: 18px;
  align-items: stretch;
}
.trend-panel-stack {
  display: grid;
  min-width: 0;
}
.trend-panel-stack > .period-panel.is-active { height: 100%; }
.chart-card, .insight-card, .detail-section, .notes-section, .trend-summary-card {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  box-shadow: var(--shadow);
}
.chart-card { padding: 26px 30px; }
.trend-card {
  box-sizing: border-box;
  height: 100%;
  min-height: 520px;
}
.section-title {
  display: block;
  margin-bottom: 0;
}
.section-title h2, .rotation-header h2, .insight-card h2, .notes-section h2, .trend-summary-card h2 {
  margin: 0;
  font-size: 28px;
  line-height: 1.1;
  font-weight: 900;
}
.section-caption {
  display: block;
  margin: 8px 0 18px;
  color: var(--muted);
  font-size: 14px;
  font-weight: 900;
}
.trend-svg {
  display: block;
  width: 100%;
  min-height: 360px;
}
.chart-bg { fill: #101419; }
.grid-line { stroke: #1b2430; stroke-width: 1; }
.zero-line { stroke: #42505f; stroke-width: 1.5; }
.trend-area { fill: url(#trendAreaGradient); }
.trend-line {
  fill: none;
  stroke: #27c7d8;
  stroke-width: 7;
  stroke-linejoin: round;
  stroke-linecap: round;
  filter: drop-shadow(0 0 7px rgba(39, 199, 216, 0.5));
}
.trend-point { fill: #27c7d8; stroke: #d9fbff; stroke-width: 2; }
.axis-label, .empty-label {
  fill: var(--muted);
  font-size: 18px;
  font-weight: 800;
}
.y-label { font-family: Consolas, "Cascadia Mono", monospace; }
.chart-footnote {
  margin-top: 12px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: 17px;
  font-weight: 800;
}
.trend-summary-card {
  box-sizing: border-box;
  position: sticky;
  top: 16px;
  padding: 24px;
}
.trend-summary-list {
  display: grid;
  gap: 8px;
}
.trend-summary-row {
  display: grid;
  grid-template-columns: 72px 58px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 9px 0;
  border-top: 1px solid var(--line-soft);
}
.trend-summary-row:first-child { border-top: 0; }
.trend-summary-label {
  color: var(--text);
  font-size: 15px;
  font-weight: 900;
}
.trend-summary-direction {
  font-size: 13px;
  font-weight: 900;
}
.trend-summary-value {
  min-width: 0;
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 20px;
  line-height: 1;
  font-weight: 900;
  text-align: right;
  white-space: nowrap;
}
.rotation-header {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 28px;
}
.rotation-stats {
  display: grid;
  grid-template-columns: repeat(4, auto);
  gap: 34px;
  align-items: start;
}
.compact-stat strong {
  display: block;
  margin-top: 8px;
  font-size: 30px;
  line-height: 1;
  font-weight: 900;
}
.rotation-grid {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 1px minmax(0, 1fr);
  gap: 26px;
  min-height: 480px;
  padding: 22px 30px;
}
.rotation-axis {
  width: 1px;
  min-height: 100%;
  background: var(--line);
}
.rotation-side {
  display: grid;
  align-content: center;
  gap: 24px;
}
.rotation-row {
  display: grid;
  grid-template-columns: 145px 90px minmax(110px, 1fr);
  align-items: center;
  gap: 14px;
  min-height: 38px;
}
.rotation-side-left .rotation-row {
  grid-template-columns: 145px 90px minmax(110px, 1fr);
}
.rotation-side-left .rotation-track {
  justify-content: flex-end;
}
.rotation-label {
  color: var(--text);
  font-size: 20px;
  font-weight: 900;
  text-align: right;
}
.rotation-value {
  font-family: Consolas, "Cascadia Mono", monospace;
  font-size: 20px;
  font-weight: 900;
}
.rotation-row.negative .rotation-value { color: var(--negative); }
.rotation-row.positive .rotation-value { color: var(--positive); }
.rotation-track {
  height: 28px;
  display: flex;
  align-items: center;
}
.rotation-bar {
  height: 100%;
  display: block;
  border-radius: 8px;
  opacity: 0.82;
}
.negative .rotation-bar { background: linear-gradient(90deg, #187a5e, var(--negative)); }
.positive .rotation-bar { background: linear-gradient(90deg, var(--positive), #a92538); }
.empty-side, .empty-state {
  color: var(--muted);
  font-size: 18px;
  font-weight: 800;
}
.insight-card {
  margin-top: 26px;
  padding: 28px 30px;
}
.insight-list {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 28px;
  margin: 22px 0 0;
  padding: 0;
  list-style: none;
}
.insight-list li {
  display: grid;
  grid-template-columns: 46px minmax(0, 1fr);
  gap: 14px;
  color: var(--muted);
  font-size: 18px;
  font-weight: 800;
  line-height: 1.45;
}
.insight-list span {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: 50%;
  color: var(--cyan);
  background: var(--cyan-soft);
  font-size: 20px;
  font-weight: 900;
}
.detail-section, .notes-section {
  margin-top: 26px;
  padding: 26px 30px;
}
.detail-view .detail-section:first-child { margin-top: 0; }
.detail-leaderboards {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}
.detail-board {
  min-width: 0;
}
.detail-board + .detail-board {
  padding-left: 24px;
  border-left: 1px solid var(--line-soft);
}
.detail-board h3 {
  margin: 0 0 14px;
  color: var(--text);
  font-size: 20px;
  font-weight: 900;
}
.table-wrap { overflow-x: auto; }
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 860px;
}
.detail-board table { min-width: 760px; }
th, td {
  padding: 14px 12px;
  border-bottom: 1px solid var(--line-soft);
  text-align: left;
  white-space: nowrap;
}
th {
  color: var(--muted);
  font-size: 14px;
  font-weight: 900;
}
td {
  color: var(--text);
  font-size: 15px;
  font-weight: 700;
}
.rank-cell {
  color: var(--muted);
  font-family: Consolas, "Cascadia Mono", monospace;
  font-weight: 900;
}
.notes-section ul {
  margin: 14px 0 0;
  padding-left: 20px;
  color: var(--muted);
  font-weight: 800;
  line-height: 1.7;
}
.flow-positive { color: var(--positive); }
.flow-negative { color: var(--negative); }
.flow-neutral { color: var(--neutral); }
@media (max-width: 1100px) {
  .dashboard-shell { width: min(100vw, 100%); margin: 0; padding: 12px; }
  .hero, .rotation-header, .control-row { flex-direction: column; align-items: stretch; }
  .metric-strip, .trend-view-layout, .insight-list { grid-template-columns: 1fr; }
  .trend-summary-card { position: static; }
  .rotation-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rotation-grid { grid-template-columns: 1fr; gap: 18px; padding: 8px 0; }
  .rotation-axis { display: none; }
  .rotation-row, .rotation-side-left .rotation-row { grid-template-columns: 105px 82px minmax(90px, 1fr); }
  .rotation-label { font-size: 16px; }
  .period-button, .view-button { height: 44px; padding: 0 14px; }
  .detail-leaderboards { grid-template-columns: 1fr; }
  .detail-board + .detail-board { padding-left: 0; border-left: 0; padding-top: 18px; border-top: 1px solid var(--line-soft); }
}
@media print {
  body { background: #ffffff; color: #111111; }
  .dashboard-shell { width: 100%; margin: 0; border: 0; background: #ffffff; }
  .icon-button, .view-tabs, .control-row { display: none; }
  .view-panel, .period-panel { display: block; page-break-inside: avoid; }
}
"""


def _script() -> str:
    return r"""
(function () {
  var toneClasses = ["flow-positive", "flow-negative", "flow-neutral"];
  function setTone(element, className) {
    if (!element) { return; }
    toneClasses.forEach(function (name) { element.classList.remove(name); });
    if (className) { element.classList.add(className); }
  }
  function updateHeader(button) {
    if (!button) { return; }
    var fields = {
      "period-net": {
        text: button.getAttribute("data-period-net-text"),
        className: button.getAttribute("data-period-net-class"),
        caption: button.getAttribute("data-period-caption")
      },
      "daily-net": {
        text: button.getAttribute("data-daily-net-text"),
        className: button.getAttribute("data-daily-net-class"),
        caption: button.getAttribute("data-daily-caption")
      },
      "direction": {
        text: button.getAttribute("data-direction-text"),
        className: button.getAttribute("data-direction-class"),
        caption: button.getAttribute("data-direction-caption")
      },
      "destination": {
        text: button.getAttribute("data-destination-text"),
        className: button.getAttribute("data-destination-class"),
        caption: button.getAttribute("data-destination-caption")
      }
    };
    Object.keys(fields).forEach(function (key) {
      var card = document.querySelector('[data-header-card="' + key + '"]');
      if (!card) { return; }
      var valueNode = card.querySelector("[data-header-value]");
      var captionNode = card.querySelector("[data-header-caption]");
      if (valueNode && fields[key].text !== null) {
        valueNode.textContent = fields[key].text;
        setTone(valueNode, fields[key].className);
      }
      if (captionNode && fields[key].caption !== null) {
        captionNode.textContent = fields[key].caption;
      }
    });
  }
  function setView(name) {
    document.querySelectorAll("[data-view-target]").forEach(function (button) {
      button.classList.toggle("is-active", button.getAttribute("data-view-target") === name);
    });
    document.querySelectorAll("[data-view-panel]").forEach(function (panel) {
      panel.classList.toggle("is-active", panel.getAttribute("data-view-panel") === name);
    });
  }
  function setPeriod(key) {
    document.querySelectorAll("[data-period-target]").forEach(function (button) {
      button.classList.toggle("is-active", button.getAttribute("data-period-target") === key);
    });
    document.querySelectorAll("[data-period-panel]").forEach(function (panel) {
      panel.classList.toggle("is-active", panel.getAttribute("data-period-panel") === key);
    });
    updateHeader(document.querySelector('[data-period-target="' + key + '"]'));
  }
  document.querySelectorAll("[data-view-target]").forEach(function (button) {
    button.addEventListener("click", function () { setView(button.getAttribute("data-view-target")); });
  });
  document.querySelectorAll("[data-period-target]").forEach(function (button) {
    button.addEventListener("click", function () { setPeriod(button.getAttribute("data-period-target")); });
  });
  updateHeader(document.querySelector("[data-period-target].is-active"));
})();
"""
