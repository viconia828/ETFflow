from __future__ import annotations

from datetime import date
import os
import stat

import pandas as pd

from etf_flow_monitor.cli import _report_history_start_date
from etf_flow_monitor.config import load_config
from etf_flow_monitor.utils.calendar import trading_calendar_from_frame
from etf_flow_monitor.utils.io import parse_excel_friendly_date
from etf_flow_monitor.utils.proxy import proxy_bypass_env
from tools.publish_pages import infer_pages_url, infer_trade_key, remove_tree, resolve_dashboard_path, resolve_publish_repo_url, stage_dashboard
from tools.update_flow_cache import (
    _calendar_refresh_status,
    _missing_dates_for_update,
    _previous_trading_day_or_same,
    _source_start_for_usable_date,
)


def test_config_local_cache_start_date_default_and_override(tmp_path) -> None:
    default_config = load_config()
    assert default_config.local_cache_start_date == date(2026, 1, 1)
    assert default_config.pages_repo_url == ""
    assert default_config.pages_branch == "gh-pages"
    assert default_config.lifecycle_events_path.as_posix() == "data/local_reference/etf_lifecycle_events.csv"
    assert default_config.announcement_file_path.as_posix() == "data/local_reference/etf_announcements.csv"
    assert default_config.announcement_source == "cninfo"
    assert default_config.announcement_api_name == "anns_d"
    assert default_config.announcement_sleep_seconds == 0.2
    assert default_config.lifecycle_status_path.as_posix() == "data/local_reference/etf_lifecycle_status.json"
    assert default_config.lifecycle_request_plan_path.as_posix() == "data/local_reference/etf_lifecycle_announcement_requests.csv"
    assert default_config.lifecycle_observation_plan_path.as_posix() == "data/local_reference/etf_lifecycle_observation_jumps.csv"
    assert default_config.lifecycle_pending_confirmations_path.as_posix() == "data/local_reference/etf_lifecycle_pending_confirmations.csv"
    assert default_config.lifecycle_manual_confirmations_path.as_posix() == "data/local_reference/etf_lifecycle_manual_confirmations.csv"
    assert default_config.lifecycle_flow_adjustments_path.as_posix() == "data/local_reference/etf_lifecycle_flow_adjustments.csv"
    assert default_config.lifecycle_announcement_window_days == 5
    assert default_config.lifecycle_no_announcement_retry_window_days == 10
    assert default_config.lifecycle_min_share_change_pct == 0.50
    assert default_config.lifecycle_high_suspicion_min_listing_days == 60
    assert default_config.lifecycle_integer_ratio_tolerance == 0.03
    assert default_config.lifecycle_high_suspicion_positive_min_pct == 2.00
    assert default_config.lifecycle_high_suspicion_negative_max_pct == -0.50

    config_path = tmp_path / "config.txt"
    config_path.write_text(
        "\n".join(
            [
                "local_cache_start_date = 20260203",
                "pages_repo_url = git@github.com:example/other.git",
                "pages_branch = pages",
                "lifecycle_events_path = custom/lifecycle.csv",
                "announcement_file_path = custom/announcements.csv",
                "announcement_source = tushare",
                "announcement_api_name = custom_anns",
                "announcement_sleep_seconds = 0.35",
                "lifecycle_status_path = custom/status.json",
                "lifecycle_request_plan_path = custom/requests.csv",
                "lifecycle_observation_plan_path = custom/observations.csv",
                "lifecycle_pending_confirmations_path = custom/pending.csv",
                "lifecycle_manual_confirmations_path = custom/manual.csv",
                "lifecycle_flow_adjustments_path = custom/adjustments.csv",
                "lifecycle_announcement_window_days = 5",
                "lifecycle_no_announcement_retry_window_days = 12",
                "lifecycle_min_share_change_pct = 0.25",
                "lifecycle_high_suspicion_min_listing_days = 90",
                "lifecycle_integer_ratio_tolerance = 0.05",
                "lifecycle_high_suspicion_positive_min_pct = 3.00",
                "lifecycle_high_suspicion_negative_max_pct = -0.80",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.local_cache_start_date == date(2026, 2, 3)
    assert config.pages_repo_url == "git@github.com:example/other.git"
    assert config.pages_branch == "pages"
    assert config.lifecycle_events_path.as_posix() == "custom/lifecycle.csv"
    assert config.announcement_file_path.as_posix() == "custom/announcements.csv"
    assert config.announcement_source == "tushare"
    assert config.announcement_api_name == "custom_anns"
    assert config.announcement_sleep_seconds == 0.35
    assert config.lifecycle_status_path.as_posix() == "custom/status.json"
    assert config.lifecycle_request_plan_path.as_posix() == "custom/requests.csv"
    assert config.lifecycle_observation_plan_path.as_posix() == "custom/observations.csv"
    assert config.lifecycle_pending_confirmations_path.as_posix() == "custom/pending.csv"
    assert config.lifecycle_manual_confirmations_path.as_posix() == "custom/manual.csv"
    assert config.lifecycle_flow_adjustments_path.as_posix() == "custom/adjustments.csv"
    assert config.lifecycle_announcement_window_days == 5
    assert config.lifecycle_no_announcement_retry_window_days == 12
    assert config.lifecycle_min_share_change_pct == 0.25
    assert config.lifecycle_high_suspicion_min_listing_days == 90
    assert config.lifecycle_integer_ratio_tolerance == 0.05
    assert config.lifecycle_high_suspicion_positive_min_pct == 3.00
    assert config.lifecycle_high_suspicion_negative_max_pct == -0.80


def test_excel_friendly_date_parser_accepts_common_csv_shapes() -> None:
    assert parse_excel_friendly_date("20260105") == pd.Timestamp("2026-01-05")
    assert parse_excel_friendly_date("2026/1/5") == pd.Timestamp("2026-01-05")
    assert parse_excel_friendly_date("20260105.0") == pd.Timestamp("2026-01-05")
    assert parse_excel_friendly_date('="20260105"') == pd.Timestamp("2026-01-05")


def test_publish_repo_url_prefers_cli_then_config() -> None:
    env = proxy_bypass_env({})
    assert (
        resolve_publish_repo_url(
            "git@github.com:cli/repo.git",
            "git@github.com:config/repo.git",
            "origin",
            env,
        )
        == "git@github.com:cli/repo.git"
    )
    assert (
        resolve_publish_repo_url(
            "",
            "git@github.com:config/repo.git",
            "origin",
            env,
        )
        == "git@github.com:config/repo.git"
    )


def test_publish_pages_url_can_include_version_token() -> None:
    assert (
        infer_pages_url("git@github.com:viconia828/ETFflow.git", "20260601", version_token="abc123")
        == "https://viconia828.github.io/ETFflow/reports/20260601/?v=abc123"
    )


def test_remove_tree_handles_read_only_files(tmp_path) -> None:
    folder = tmp_path / "tmp_pages_publish"
    folder.mkdir()
    locked_file = folder / "object"
    locked_file.write_text("git object", encoding="utf-8")
    os.chmod(locked_file, stat.S_IREAD)

    remove_tree(folder)

    assert not folder.exists()


def test_publish_dashboard_path_falls_back_to_previous_generated_trade_date(tmp_path) -> None:
    dashboard = tmp_path / "flow_monitor" / "20260618" / "etf_flow_dashboard.html"
    dashboard.parent.mkdir(parents=True)
    dashboard.write_text("<!doctype html>", encoding="utf-8")

    resolved = resolve_dashboard_path(tmp_path, trade_date="20260619")

    assert resolved == dashboard.resolve()
    assert infer_trade_key(resolved) == "20260618"


def test_publish_homepage_uses_latest_report_date_not_last_publish(tmp_path) -> None:
    worktree = tmp_path / "pages"
    new_dashboard = tmp_path / "new.html"
    old_dashboard = tmp_path / "old.html"
    new_dashboard.write_text("<!doctype html><title>new report</title>", encoding="utf-8")
    old_dashboard.write_text("<!doctype html><title>old report</title>", encoding="utf-8")

    stage_dashboard(worktree, new_dashboard, "20260626")
    stage_dashboard(worktree, old_dashboard, "20260618")

    assert "new report" in (worktree / "index.html").read_text(encoding="utf-8")
    reports_index = (worktree / "reports" / "index.html").read_text(encoding="utf-8")
    assert reports_index.index("2026-06-26") < reports_index.index("2026-06-18")
    assert "./20260626/?v=" in reports_index
    assert "./20260618/?v=" in reports_index


def test_calendar_refresh_status_visualizes_cached_tail_and_action() -> None:
    cached = pd.DataFrame(
        [
            {"cal_date": "2026-06-01"},
            {"cal_date": "2026-06-30"},
        ]
    )

    covered = _calendar_refresh_status(cached, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-27"))
    stale = _calendar_refresh_status(cached, pd.Timestamp("2026-06-01"), pd.Timestamp("2026-07-10"))

    assert covered == {"cached_tail": "20260630", "required_tail": "20260627", "action": "cached"}
    assert stale == {"cached_tail": "20260630", "required_tail": "20260710", "action": "fetch"}


def test_cache_source_start_uses_12_month_background_window() -> None:
    calendar = trading_calendar_from_frame(
        pd.DataFrame(
            [
                {"exchange": "SSE", "cal_date": "2024-12-31", "is_open": 1, "pretrade_date": "2024-12-30"},
                {"exchange": "SSE", "cal_date": "2025-01-01", "is_open": 0, "pretrade_date": "2024-12-31"},
                {"exchange": "SSE", "cal_date": "2025-01-02", "is_open": 1, "pretrade_date": "2024-12-31"},
                {"exchange": "SSE", "cal_date": "2026-01-01", "is_open": 1, "pretrade_date": "2025-12-31"},
            ]
        ),
        exchange="SSE",
    )

    source_start = _source_start_for_usable_date(date(2026, 1, 1))
    assert source_start == pd.Timestamp("2025-01-01")
    assert _previous_trading_day_or_same(calendar, source_start) == pd.Timestamp("2024-12-31")
    assert _report_history_start_date(calendar, pd.Timestamp("2026-01-01")) == pd.Timestamp("2024-12-31")


def test_missing_dates_for_update_uses_fast_interval_edges() -> None:
    trade_dates = [pd.Timestamp("2025-01-02"), pd.Timestamp("2025-01-03"), pd.Timestamp("2025-01-06")]
    full_keys = {"20250102", "20250103", "20250106"}

    missing_daily, missing_share, mode = _missing_dates_for_update(
        daily_keys=full_keys,
        share_keys=full_keys,
        trade_dates=trade_dates,
        refresh=False,
        full_check=False,
    )
    assert mode == "fast_interval_covered"
    assert missing_daily == []
    assert missing_share == []

    missing_daily, missing_share, mode = _missing_dates_for_update(
        daily_keys=full_keys,
        share_keys={"20250102", "20250103"},
        trade_dates=trade_dates,
        refresh=False,
        full_check=False,
    )
    assert mode == "fast_interval_edges"
    assert missing_daily == []
    assert missing_share == [pd.Timestamp("2025-01-06")]


def test_proxy_bypass_env_removes_proxy_variables() -> None:
    env = proxy_bypass_env(
        {
            "HTTP_PROXY": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "NO_PROXY": "example.com",
        },
        bypass_git_ssh_proxy=True,
    )
    assert "HTTP_PROXY" not in env
    assert "https_proxy" not in env
    assert "api.tushare.pro" in env["NO_PROXY"]
    assert "github.com" in env["NO_PROXY"]
    assert "example.com" in env["NO_PROXY"]
    assert "ProxyCommand=none" in env["GIT_SSH_COMMAND"]
