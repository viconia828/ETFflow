from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields, replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from etf_flow_monitor.config import FlowMonitorConfig
from etf_flow_monitor.data.cache_store import CacheStore
from etf_flow_monitor.data.lifecycle import build_lifecycle_review_plans
from etf_flow_monitor.data.tushare_etf_source import TushareEtfSource
from etf_flow_monitor.run_ledger import RunLedger
from etf_flow_monitor.utils.calendar import trading_calendar_from_frame
from tools import build_etf_lifecycle_table as lifecycle


def calendar_fixture() -> pd.DataFrame:
    # Explicit exchange-style fixture spanning a long closure. Do not generate
    # expected trading dates with a business-day calendar.
    open_dates = {
        "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22",
        "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29",
        "2026-09-30", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13",
        "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
        "2026-10-21",
    }
    rows = []
    previous = "2026-09-15"
    for day in pd.date_range("2026-09-16", "2026-10-21"):
        current = day.strftime("%Y-%m-%d")
        rows.append({"exchange": "SSE", "cal_date": current,
                     "is_open": int(current in open_dates), "pretrade_date": previous})
        if current in open_dates:
            previous = current
    return pd.DataFrame(rows)


class LifecycleCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = FlowMonitorConfig()
        paths = {
            field.name: self.root / getattr(config, field.name)
            for field in fields(config) if isinstance(getattr(config, field.name), Path)
        }
        self.config = replace(config, **paths)
        self.config_path = self.root / "config.txt"
        self.config_path.write_text("\n".join(
            f"{field.name} = {getattr(self.config, field.name)}" for field in fields(self.config)
        ), encoding="utf-8")
        self.cache = CacheStore(self.config.cache_dir)
        self.full = calendar_fixture()
        self.client = Mock()
        self.client.query.return_value = self.full.to_dict("records")
        source = TushareEtfSource(self.client, self.cache)
        self.factory = self.enterContext(patch.object(TushareEtfSource, "from_runtime", return_value=source))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.ledger = RunLedger(log_dir=self.root / "test_log", argv=[], config_path=self.config_path)
        self.audit = pd.DataFrame([{
            "fund_code": "510300.SH", "trade_date": "20260930", "prev_trade_date": "20260929",
            "prev_shares": 100.0, "shares": 300.0, "share_change_pct": 2.0, "match_status": "unmatched",
        }])

    def save_calendar(self, frame: pd.DataFrame) -> None:
        self.cache.save_calendar("tushare", "SSE", frame)

    def ensure_calendar(self, days: int = 10):
        return lifecycle._ensure_lifecycle_calendar(
            self.audit, config=self.config, config_path=self.config_path,
            window_days=days, ledger=self.ledger,
        )

    def test_extends_tail_across_holiday_for_normal_and_retry_windows(self) -> None:
        truncated = self.full.loc[self.full["cal_date"].le("2026-10-11")]
        self.save_calendar(truncated)
        with self.assertRaisesRegex(ValueError, "out of range"):
            build_lifecycle_review_plans(
                self.audit, calendar=trading_calendar_from_frame(truncated), window_days=5,
            )
        calendar = self.ensure_calendar()
        high, low = build_lifecycle_review_plans(self.audit, calendar=calendar, window_days=5)
        self.assertTrue(low.empty)
        self.assertEqual(high.loc[0, "request_start_date"], pd.Timestamp("2026-09-23"))
        self.assertEqual(high.loc[0, "request_end_date"], pd.Timestamp("2026-10-14"))
        pending = self.audit.assign(status="no_announcement_found")
        retry = lifecycle._expand_no_announcement_retry_windows(high, pending, window_days=10, calendar=calendar)
        self.assertEqual(retry.loc[0, "request_start_date"], pd.Timestamp("2026-09-16"))
        self.assertEqual(retry.loc[0, "request_end_date"], pd.Timestamp("2026-10-21"))
        self.client.query.assert_called_once()
        self.assertEqual(self.client.query.call_args.args[0], "trade_cal")
        self.assertEqual(self.client.query.call_args.kwargs["params"]["exchange"], "SSE")
        saved = self.cache.load_calendar("tushare", "SSE")
        self.assertEqual(len(saved), len(self.full))
        self.assertEqual(saved["cal_date"].nunique(), len(self.full))

    def test_extends_head_and_preserves_cache_outside_requested_range(self) -> None:
        outside = pd.DataFrame([{
            "exchange": "SSE", "cal_date": "2025-01-02", "is_open": 1, "pretrade_date": "2024-12-31",
        }])
        self.save_calendar(pd.concat([outside, self.full.loc[self.full["cal_date"].ge("2026-09-29")]]))
        calendar = self.ensure_calendar()
        self.assertEqual(calendar.shift_trade_date(pd.Timestamp("2026-09-30").date(), -10).isoformat(), "2026-09-16")
        saved = self.cache.load_calendar("tushare", "SSE")
        self.assertIn("2025-01-02", saved["cal_date"].tolist())

    def test_covered_cache_needs_no_credentials_or_fetch(self) -> None:
        self.save_calendar(self.full)
        self.assertIsNotNone(self.ensure_calendar())
        self.factory.assert_not_called()

    def test_missing_cache_fetches_official_calendar(self) -> None:
        calendar = self.ensure_calendar()
        self.assertEqual(calendar.open_dates[-1].isoformat(), "2026-10-21")
        self.client.query.assert_called_once()

    def test_internal_missing_closed_day_requires_official_refresh(self) -> None:
        self.save_calendar(self.full.loc[self.full["cal_date"].ne("2026-10-01")])
        calendar = self.ensure_calendar(days=5)
        self.assertIn(pd.Timestamp("2026-10-01").date(), [row.cal_date for row in calendar.rows])
        self.client.query.assert_called_once()

    def test_internal_gap_is_not_hidden_by_shiftable_endpoints(self) -> None:
        incomplete = self.full.loc[self.full["cal_date"].ne("2026-10-01")]
        self.save_calendar(incomplete)
        self.client.query.return_value = incomplete.to_dict("records")
        with self.assertRaisesRegex(RuntimeError, "still does not cover"):
            self.ensure_calendar(days=5)

    def test_empty_remote_response_without_cache_stops_clearly(self) -> None:
        self.client.query.return_value = []
        with self.assertRaisesRegex(RuntimeError, "still does not cover.*cached=missing"):
            self.ensure_calendar()

    def test_resolved_or_empty_audit_needs_no_calendar(self) -> None:
        for status in (lifecycle.MATCH_STATUS_MATCHED, lifecycle.MATCH_STATUS_MANUAL_CONFIRMED):
            self.audit["match_status"] = status
            self.assertIsNone(self.ensure_calendar())
        self.audit = pd.DataFrame()
        self.assertIsNone(self.ensure_calendar())
        self.factory.assert_not_called()

    def test_insufficient_remote_calendar_stops_without_calendar_day_fallback(self) -> None:
        truncated = self.full.loc[self.full["cal_date"].le("2026-10-11")]
        self.save_calendar(truncated)
        self.client.query.return_value = []
        with self.assertRaisesRegex(RuntimeError, "still does not cover"):
            self.ensure_calendar()
        calendar = trading_calendar_from_frame(truncated)
        with self.assertRaisesRegex(ValueError, "out of range"):
            lifecycle._trading_day_window("20260930", window_days=10, calendar=calendar)

    def test_base_after_cache_tail_is_not_anchored_to_stale_last_day(self) -> None:
        self.save_calendar(self.full)
        self.audit["trade_date"] = "20261101"
        with self.assertRaisesRegex(RuntimeError, "still does not cover"):
            self.ensure_calendar(days=0)
        self.client.query.assert_called_once()

    def test_rejects_business_day_cache(self) -> None:
        self.save_calendar(self.full.assign(exchange="BIZ"))
        with self.assertRaisesRegex(ValueError, "Official trading calendar required"):
            self.ensure_calendar()
        self.factory.assert_not_called()

    def run_main(self) -> tuple[int, dict]:
        shares = pd.DataFrame([
            {"fund_code": "510300.SH", "trade_date": "20260929", "shares": 100.0},
            {"fund_code": "510300.SH", "trade_date": "20260930", "shares": 300.0},
        ])
        with patch.object(lifecycle, "load_cached_share_cross_sections", return_value=shares):
            result = lifecycle.main(["--config", str(self.config_path), "--force"])
        ledger_path = next((self.config.output_dir / "logs").glob("lifecycle_audit_*/run.json"))
        return result, json.loads(ledger_path.read_text(encoding="utf-8"))

    def test_main_builds_plan_and_records_success_after_refresh(self) -> None:
        self.save_calendar(self.full.loc[self.full["cal_date"].le("2026-10-11")])
        result, ledger = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(ledger["status"], "success")
        self.assertEqual(ledger["stats"]["request_plan_rows"], 1)
        plan = pd.read_csv(self.config.lifecycle_request_plan_path)
        self.assertEqual(str(plan.loc[0, "request_end_date"]), "20261014")

    def test_refresh_failure_is_logged_without_overwriting_reference_files(self) -> None:
        self.save_calendar(self.full.loc[self.full["cal_date"].le("2026-10-11")])
        preserved = {}
        for name in ("lifecycle_events_path", "lifecycle_pending_confirmations_path", "lifecycle_request_plan_path"):
            path = getattr(self.config, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            content = "fund_code,trade_date,review_status\n510300.SH,20260930,manual\n"
            path.write_text(content, encoding="utf-8")
            preserved[path] = path.read_bytes()
        cache_path = self.config.cache_dir / "tushare/calendar/SSE.csv"
        preserved[cache_path] = cache_path.read_bytes()
        self.client.query.side_effect = RuntimeError("rate limit exceeded")
        with redirect_stderr(io.StringIO()):
            result, ledger = self.run_main()
        self.assertEqual(result, 1)
        self.assertEqual(ledger["status"], "failed")
        self.assertEqual(ledger["last_stage"], "refresh_calendar")
        self.assertIn("rate limit exceeded", ledger["error"])
        for path, content in preserved.items():
            self.assertEqual(path.read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
