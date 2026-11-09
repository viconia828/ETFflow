"""Markdown report rendering for ETF flow snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd


def exchange_share_notes(flow_today: pd.DataFrame) -> list[str]:
    sources = set(flow_today.get("source", pd.Series(dtype=str)).dropna())
    notes = []
    if sources.intersection({"sse:etf_scale", "sse:money_scale"}):
        notes.append("沪市当日份额取自上交所公布的清算后基金份额。")
    if "szse:etf_scale" in sources:
        notes.append("深市当日份额取自深交所基金规模表。晚间披露值供参考，最终以次日早间更新为准。")
    return notes


def write_markdown_report(
    path: Path,
    *,
    title: str,
    summary: pd.DataFrame,
    alerts: pd.DataFrame,
    notes: Iterable[str] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    lines.extend(["## Market Summary", ""])
    lines.extend(_table_lines(summary))
    lines.extend(["", "## ETF Alerts", ""])
    lines.extend(_table_lines(alerts))
    note_lines = [str(item).strip() for item in notes if str(item).strip()]
    if note_lines:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {item}" for item in note_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _table_lines(frame: pd.DataFrame) -> list[str]:
    if frame is None or frame.empty:
        return ["No rows."]
    working = frame.copy()
    for column in working.columns:
        if pd.api.types.is_datetime64_any_dtype(working[column]):
            working[column] = working[column].dt.strftime("%Y-%m-%d")
    columns = [str(column) for column in working.columns]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for _, row in working.iterrows():
        lines.append("| " + " | ".join(_format_cell(row.get(column)) for column in working.columns) + " |")
    return lines


def _format_cell(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value).replace("|", "/")
