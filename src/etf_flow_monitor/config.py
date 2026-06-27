"""Simple key=value config loader for the ETF flow monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from etf_flow_monitor.utils.calendar import normalize_date_input


@dataclass(frozen=True, slots=True)
class FlowMonitorConfig:
    cache_dir: Path = Path("data/cache")
    output_dir: Path = Path("outputs")
    source_name: str = "tushare"
    calendar_exchange: str = "SSE"
    etf_market: str = "E"
    category_map_path: Path = Path("data/local_reference/etf_category_map.csv")
    lifecycle_events_path: Path = Path("data/local_reference/etf_lifecycle_events.csv")
    announcement_file_path: Path = Path("data/local_reference/etf_announcements.csv")
    announcement_source: str = "cninfo"
    announcement_api_name: str = "anns_d"
    announcement_sleep_seconds: float = 0.2
    lifecycle_status_path: Path = Path("data/local_reference/etf_lifecycle_status.json")
    lifecycle_request_plan_path: Path = Path("data/local_reference/etf_lifecycle_announcement_requests.csv")
    lifecycle_observation_plan_path: Path = Path("data/local_reference/etf_lifecycle_observation_jumps.csv")
    lifecycle_pending_confirmations_path: Path = Path("data/local_reference/etf_lifecycle_pending_confirmations.csv")
    lifecycle_manual_confirmations_path: Path = Path("data/local_reference/etf_lifecycle_manual_confirmations.csv")
    lifecycle_flow_adjustments_path: Path = Path("data/local_reference/etf_lifecycle_flow_adjustments.csv")
    lifecycle_announcement_window_days: int = 5
    lifecycle_no_announcement_retry_window_days: int = 10
    lifecycle_min_share_change_pct: float = 0.50
    lifecycle_high_suspicion_min_listing_days: int = 60
    lifecycle_integer_ratio_tolerance: float = 0.03
    lifecycle_high_suspicion_positive_min_pct: float = 2.00
    lifecycle_high_suspicion_negative_max_pct: float = -0.50
    local_cache_start_date: date = date(2026, 1, 1)
    pages_repo_url: str = ""
    pages_branch: str = "gh-pages"
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
        lifecycle_events_path=Path(values.get("lifecycle_events_path", "data/local_reference/etf_lifecycle_events.csv")),
        announcement_file_path=Path(values.get("announcement_file_path", "data/local_reference/etf_announcements.csv")),
        announcement_source=values.get("announcement_source", "cninfo").strip().lower() or "cninfo",
        announcement_api_name=values.get("announcement_api_name", "anns_d").strip() or "anns_d",
        announcement_sleep_seconds=float(values.get("announcement_sleep_seconds", "0.2")),
        lifecycle_status_path=Path(values.get("lifecycle_status_path", "data/local_reference/etf_lifecycle_status.json")),
        lifecycle_request_plan_path=Path(
            values.get("lifecycle_request_plan_path", "data/local_reference/etf_lifecycle_announcement_requests.csv")
        ),
        lifecycle_observation_plan_path=Path(
            values.get("lifecycle_observation_plan_path", "data/local_reference/etf_lifecycle_observation_jumps.csv")
        ),
        lifecycle_pending_confirmations_path=Path(
            values.get("lifecycle_pending_confirmations_path", "data/local_reference/etf_lifecycle_pending_confirmations.csv")
        ),
        lifecycle_manual_confirmations_path=Path(
            values.get("lifecycle_manual_confirmations_path", "data/local_reference/etf_lifecycle_manual_confirmations.csv")
        ),
        lifecycle_flow_adjustments_path=Path(
            values.get("lifecycle_flow_adjustments_path", "data/local_reference/etf_lifecycle_flow_adjustments.csv")
        ),
        lifecycle_announcement_window_days=int(values.get("lifecycle_announcement_window_days", "5")),
        lifecycle_no_announcement_retry_window_days=int(values.get("lifecycle_no_announcement_retry_window_days", "10")),
        lifecycle_min_share_change_pct=float(values.get("lifecycle_min_share_change_pct", "0.50")),
        lifecycle_high_suspicion_min_listing_days=int(values.get("lifecycle_high_suspicion_min_listing_days", "60")),
        lifecycle_integer_ratio_tolerance=float(values.get("lifecycle_integer_ratio_tolerance", "0.03")),
        lifecycle_high_suspicion_positive_min_pct=float(values.get("lifecycle_high_suspicion_positive_min_pct", "2.00")),
        lifecycle_high_suspicion_negative_max_pct=float(values.get("lifecycle_high_suspicion_negative_max_pct", "-0.50")),
        local_cache_start_date=normalize_date_input(values.get("local_cache_start_date", "20260101"), field_name="local_cache_start_date"),
        pages_repo_url=values.get("pages_repo_url", "").strip(),
        pages_branch=values.get("pages_branch", "gh-pages").strip() or "gh-pages",
        lookback_days=int(values.get("lookback_days", "370")),
        max_report_rows=int(values.get("max_report_rows", "30")),
        min_amount_for_alert=float(values.get("min_amount_for_alert", "100000000")),
        min_abs_flow_for_alert=float(values.get("min_abs_flow_for_alert", "50000000")),
    )
