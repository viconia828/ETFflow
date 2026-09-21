from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from email.message import Message
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tools import publish_pages as pages


class PublishPagesTests(unittest.TestCase):
    def wait(self, get_json, verify_online=None, timeout=2):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        return pages.wait_for_github_pages_deployment(
            "git@github.com:example/reports.git",
            "abc123",
            timeout_seconds=timeout,
            poll_seconds=10,
            http_get_json=get_json,
            verify_online=verify_online,
            sleep=sleep,
            monotonic=lambda: clock[0],
        )

    def test_online_content_accepts_only_newline_normalization(self):
        check = pages.published_dashboard_state(
            "https://example.github.io/reports/",
            b"<html>\r\nnew report\r\n</html>",
            http_get_bytes=lambda url: b"<html>\nnew report\n</html>",
        )
        self.assertEqual(check.state, "content_verified")
        for old_content in (b"<html>\nold report\n</html>", b"<html>\n new report\n</html>"):
            check = pages.published_dashboard_state(
                "https://example.github.io/reports/",
                b"<html>\r\nnew report\r\n</html>",
                http_get_bytes=lambda url: old_content,
            )
            self.assertEqual(check.state, "pending")

    def test_api_dns_error_can_be_resolved_by_matching_online_content(self):
        get_json = Mock(side_effect=URLError("[Errno 11001] getaddrinfo failed"))
        check = self.wait(
            get_json,
            lambda: pages.DeploymentCheck("content_verified", "content matches", environment_url="https://example.test/"),
        )
        self.assertEqual(check.state, "content_verified")
        self.assertIn("getaddrinfo failed", check.message)
        self.assertEqual(check.environment_url, "https://example.test/")
        self.assertEqual(get_json.call_count, 1)

    def test_timeout_preserves_api_error_and_does_not_accept_stale_content(self):
        check = self.wait(
            Mock(side_effect=URLError("[Errno 11001] getaddrinfo failed")),
            lambda: pages.DeploymentCheck("pending", "Online content is stale"),
        )
        self.assertEqual(check.state, "timeout")
        self.assertIn("getaddrinfo failed", check.message)
        self.assertIn("Online content is stale", check.message)
        self.assertEqual(check.target_url, "https://github.com/example/reports/actions")

    def test_timeout_preserves_both_network_errors(self):
        check = self.wait(
            Mock(side_effect=URLError("API unavailable")),
            Mock(side_effect=URLError("Page unavailable")),
        )
        self.assertEqual(check.state, "timeout")
        self.assertIn("API unavailable", check.message)
        self.assertIn("Page unavailable", check.message)

    def test_pending_deployment_can_use_content_check_at_timeout(self):
        verify = Mock(return_value=pages.DeploymentCheck("content_verified", "content matches"))
        check = self.wait(lambda url: [], verify)
        self.assertEqual(check.state, "content_verified")
        verify.assert_called_once()

    def test_pending_timeout_keeps_job_link_and_description(self):
        def get_json(url):
            if "deployments?" in url:
                return [{"statuses_url": "https://api.github.test/statuses"}]
            return [{"state": "in_progress", "description": "Deployment queued", "log_url": "https://example.test/job/1"}]

        check = self.wait(get_json)
        self.assertEqual(check.state, "timeout")
        self.assertIn("Deployment queued", check.message)
        self.assertEqual(check.target_url, "https://example.test/job/1")

    def test_explicit_failure_and_success_do_not_use_content_fallback(self):
        for state in ("failure", "error", "success"):
            with self.subTest(state=state):
                def get_json(url):
                    if "deployments?" in url:
                        return [{"statuses_url": "https://api.github.test/statuses"}]
                    return [{"state": state}]

                verify = Mock()
                self.assertEqual(self.wait(get_json, verify).state, state)
                verify.assert_not_called()

    def test_disabled_check_does_not_contact_network(self):
        get_json, verify = Mock(), Mock()
        self.assertEqual(self.wait(get_json, verify, timeout=0).state, "skipped")
        get_json.assert_not_called()
        verify.assert_not_called()

    def test_range_verification_requires_every_report_to_match(self):
        reports = [(f"https://example.test/{date}/", f"report {date}\r\n".encode()) for date in range(6)]
        online = {url: content.replace(b"\r\n", b"\n") for url, content in reports}
        check = pages.published_dashboards_state(reports, http_get_bytes=online.__getitem__)
        self.assertEqual(check.state, "content_verified")
        self.assertIn("All 6", check.message)
        # The latest report matches, but an earlier report is stale.
        online[reports[0][0]] = b"old version"
        check = pages.published_dashboards_state(reports, http_get_bytes=online.__getitem__)
        self.assertEqual(check.state, "pending")
        self.assertIn(reports[0][0], check.message)

    def test_range_network_failure_stops_after_first_batch(self):
        reports = [(f"https://example.test/{date}/", b"report") for date in range(20)]
        get_bytes = Mock(side_effect=URLError("Page unavailable"))
        check = pages.published_dashboards_state(reports, http_get_bytes=get_bytes)
        self.assertEqual(check.state, "pending")
        self.assertIn("Page unavailable", check.message)
        self.assertEqual(get_bytes.call_count, 3)
        self.assertEqual(pages.published_dashboards_state([]).state, "pending")

    def test_rate_limit_stops_api_retry_and_preserves_reset_information(self):
        headers = Message()
        headers["x-ratelimit-remaining"] = "0"
        headers["x-ratelimit-reset"] = "1790006400"
        get_json = Mock(side_effect=HTTPError("https://api.github.test/", 403, "Forbidden", headers, None))
        verify = Mock(return_value=pages.DeploymentCheck("pending", "Older report is stale"))
        check = self.wait(get_json, verify)
        self.assertEqual(check.state, "rate_limited")
        self.assertIn("quota resets at", check.message)
        self.assertIn("Older report is stale", check.message)
        self.assertEqual(get_json.call_count, 1)
        self.assertEqual(verify.call_count, 2)

    def test_rate_limited_run_waits_for_online_reports_without_more_api_calls(self):
        get_json = Mock(side_effect=HTTPError("https://api.github.test/", 429, "Too Many Requests", {}, None))
        verify = Mock(side_effect=[
            pages.DeploymentCheck("pending", "Not deployed yet"),
            pages.DeploymentCheck("content_verified", "All reports match"),
        ])
        check = self.wait(get_json, verify)
        self.assertEqual(check.state, "content_verified")
        self.assertEqual(get_json.call_count, 1)
        self.assertEqual(verify.call_count, 2)

    def test_rate_limit_can_be_resolved_by_all_online_reports(self):
        get_json = Mock(side_effect=HTTPError("https://api.github.test/", 403, "rate limit exceeded", {}, None))
        reports = [(f"https://example.test/{date}/", b"report") for date in range(6)]
        check = self.wait(get_json, lambda: pages.published_dashboards_state(reports, http_get_bytes=lambda url: b"report"))
        self.assertEqual(check.state, "content_verified")
        self.assertIn("All 6", check.message)
        self.assertIn("rate_limited", check.message)
        self.assertEqual(get_json.call_count, 1)

    def test_rate_limit_without_fallback_returns_without_retry(self):
        get_json = Mock(side_effect=HTTPError("https://api.github.test/", 429, "Too Many Requests", {"Retry-After": "60"}, None))
        check = self.wait(get_json)
        self.assertEqual(check.state, "rate_limited")
        self.assertIn("Retry-After=60", check.message)
        self.assertEqual(get_json.call_count, 1)

    def test_non_rate_limit_403_is_not_misclassified(self):
        error = HTTPError("https://api.github.test/", 403, "Forbidden", {}, None)
        self.assertEqual(pages.github_rate_limit_message(error), "")
        check = self.wait(Mock(side_effect=error))
        self.assertEqual(check.state, "timeout")
        self.assertIn("Forbidden", check.message)

    def test_confirmation_exit_codes_and_range_fallback_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dashboard = root / "dashboard.html"
            dashboard.write_text("new report", encoding="utf-8")
            args = argparse.Namespace(deployment_timeout_seconds=120, deployment_poll_seconds=10)
            for count in (1, 2):
                items = [(str(20260920 + index), dashboard) for index in range(count)]
                for state, expected_code in (("success", 0), ("content_verified", 0), ("skipped", 0), ("failure", 1), ("timeout", 2), ("rate_limited", 2)):
                    with self.subTest(count=count, state=state), patch.object(
                        pages, "wait_for_github_pages_deployment", return_value=pages.DeploymentCheck(state)
                    ) as wait, patch.object(pages, "remove_tree"), redirect_stdout(io.StringIO()):
                        result = pages.confirm_publish(args, "git@github.com:example/reports.git", "abc123", "https://example.test/", items, root)
                        self.assertEqual(result, expected_code)
                        with patch.object(pages, "published_dashboards_state") as verify:
                            wait.call_args.kwargs["verify_online"]()
                            expected = verify.call_args.args[0]
                            self.assertEqual(len(expected), count)
                            self.assertTrue(all(content == b"new report" for url, content in expected))

    def test_no_changes_still_checks_existing_deployment_without_push(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = Path(directory) / "20260921" / "etf_flow_dashboard.html"
            dashboard.parent.mkdir()
            dashboard.write_text("new report", encoding="utf-8")
            config = SimpleNamespace(output_dir=Path(directory), pages_repo_url="git@github.com:example/reports.git", pages_branch="gh-pages")
            with patch.object(pages, "load_config", return_value=config), patch.object(
                pages, "resolve_dashboard_path", return_value=dashboard
            ), patch.object(pages, "stage_dashboards"), patch.object(
                pages, "prepare_publish_worktree", side_effect=lambda path: path
            ), patch.object(
                pages, "run_git", return_value=subprocess.CompletedProcess([], 0, "", "")
            ) as git, patch.object(pages, "git_output", side_effect=["", "abc123"]), patch.object(
                pages, "confirm_publish", return_value=2
            ) as confirm, redirect_stdout(io.StringIO()):
                self.assertEqual(pages.main(["--dashboard", str(dashboard)]), 2)
                confirm.assert_called_once()
                self.assertEqual(confirm.call_args.args[2], "abc123")
                self.assertEqual(git.call_count, 1)
                self.assertEqual(git.call_args.args[0][1], "clone")


if __name__ == "__main__":
    unittest.main()
