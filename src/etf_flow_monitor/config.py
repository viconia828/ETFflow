"""Simple key=value config loader for the starter project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FlowMonitorConfig:
    cache_dir: Path = Path("data/cache")
    output_dir: Path = Path("outputs")
    source_name: str = "tushare"
    calendar_exchange: str = "SSE"
    etf_market: str = "E"
    category_map_path: Path = Path("data/local_reference/etf_category_map.csv")
    lookback_days: int = 370
    max_report_rows: int = 30
    min_amount_for_alert: float = 100_000_000.0
    min_abs_flow_for_alert: float = 50_000_000.0


def load_config(path: str | Path | None = None) -> FlowMonitorConfig:
    if path is None:
        return FlowMonitorConfig()
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config not found: {config_path}")
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return FlowMonitorConfig(
        cache_dir=Path(values.get("cache_dir", "data/cache")),
        output_dir=Path(values.get("output_dir", "outputs")),
        source_name=values.get("source_name", "tushare"),
        calendar_exchange=values.get("calendar_exchange", "SSE"),
        etf_market=values.get("etf_market", "E"),
        category_map_path=Path(values.get("category_map_path", "data/local_reference/etf_category_map.csv")),
        lookback_days=int(values.get("lookback_days", "370")),
        max_report_rows=int(values.get("max_report_rows", "30")),
        min_amount_for_alert=float(values.get("min_amount_for_alert", "100000000")),
        min_abs_flow_for_alert=float(values.get("min_abs_flow_for_alert", "50000000")),
    )
