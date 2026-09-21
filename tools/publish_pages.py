"""Publish generated ETF dashboard HTML files to the gh-pages branch."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.utils.calendar import get_shanghai_now, is_after_market_date_cutoff, normalize_date_input  # noqa: E402
from etf_flow_monitor.utils.proxy import proxy_bypass_env  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish ETF flow dashboard to GitHub Pages.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", default="", help="Report date YYYYMMDD. Blank means latest generated dashboard.")
    parser.add_argument("--range-start", default="", help="Publish all generated dashboards from this date.")
    parser.add_argument("--range-end", default="", help="Publish all generated dashboards through this date.")
    parser.add_argument("--dashboard", default="", help="Explicit dashboard HTML path.")
    parser.add_argument("--repo-url", default="", help="Target Git repository URL. Defaults to config pages_repo_url, then git remote.")
    parser.add_argument("--remote", default="", help="Remote name used inside the temporary clone. Defaults to origin.")
    parser.add_argument("--branch", default="", help="Pages branch. Defaults to config pages_branch.")
    parser.add_argument("--worktree", default="tmp_pages_publish")
    parser.add_argument(
        "--deployment-timeout-seconds",
        type=float,
        default=120.0,
        help="Wait for GitHub Pages deployment after push. Use 0 to skip the check.",
    )
    parser.add_argument("--deployment-poll-seconds", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true", help="Resolve inputs and print intended publish action without git operations.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    git_env = proxy_bypass_env(bypass_git_ssh_proxy=True)
    range_start = str(args.range_start or "").strip()
    range_end = str(args.range_end or "").strip()
    if bool(range_start) != bool(range_end):
        print("[pages] --range-start and --range-end must be provided together.", flush=True)
        return 1
    if range_start:
        dashboard_items = resolve_dashboard_range(config.output_dir, range_start=range_start, range_end=range_end)
        trade_key = dashboard_items[-1][0]
        dashboard_path = dashboard_items[-1][1]
        requested_trade_key = ""
    else:
        requested_trade_key = normalize_trade_key(args.trade_date) if str(args.trade_date or "").strip() else ""
        dashboard_path = resolve_dashboard_path(config.output_dir, trade_date=args.trade_date, dashboard=args.dashboard)
        if str(args.dashboard or "").strip() and requested_trade_key:
            trade_key = requested_trade_key
        else:
            trade_key = infer_trade_key(dashboard_path)
        dashboard_items = [(trade_key, dashboard_path)]
    version_token = file_version_token(dashboard_path)
    remote_name = str(args.remote or "").strip() or "origin"
    branch = str(args.branch or "").strip() or config.pages_branch
    remote_url = resolve_publish_repo_url(args.repo_url, config.pages_repo_url, remote_name, git_env)
    pages_url = infer_pages_url(remote_url, trade_key, version_token=version_token)

    if range_start:
        print(f"[pages] dashboards={len(dashboard_items)} range={dashboard_items[0][0]}..{dashboard_items[-1][0]}", flush=True)
    print(f"[pages] dashboard={dashboard_path}", flush=True)
    if requested_trade_key and requested_trade_key != trade_key:
        print(f"[pages] requested_trade_date={requested_trade_key} resolved_trade_date={trade_key}", flush=True)
    print(f"[pages] repo={remote_url}", flush=True)
    if range_start:
        print(f"[pages] target=reports/{dashboard_items[0][0]}..{dashboard_items[-1][0]} branch={branch} remote={remote_name}", flush=True)
    else:
        print(f"[pages] target=reports/{trade_key}/ index.html branch={branch} remote={remote_name}", flush=True)
    if pages_url:
        print(f"[pages] url={pages_url}", flush=True)
    if args.dry_run:
        print("[pages] dry-run only; no git operations performed.", flush=True)
        return 0

    worktree = (PROJECT_ROOT / args.worktree).resolve()
    ensure_safe_temp_path(worktree)
    if worktree.exists():
        worktree = prepare_publish_worktree(worktree)

    clone = run_git(
        ["git", "clone", "--origin", remote_name, "--branch", branch, "--single-branch", remote_url, str(worktree)],
        cwd=PROJECT_ROOT,
        env=git_env,
    )
    if clone.returncode != 0:
        print("[pages] clone failed; check network and GitHub SSH configuration.", flush=True)
        print(clone.stderr.strip(), flush=True)
        return 1

    stage_dashboards(worktree, dashboard_items)
    status = git_output(["git", "status", "--porcelain"], cwd=worktree, env=git_env)
    if not status.strip():
        print("[pages] no page changes to push; checking existing deployment.", flush=True)
        commit_sha = git_output(["git", "rev-parse", "HEAD"], cwd=worktree, env=git_env)
        return confirm_publish(args, remote_url, commit_sha, pages_url, dashboard_items, worktree)

    add = run_git(["git", "add", "."], cwd=worktree, env=git_env)
    if add.returncode != 0:
        print(add.stderr.strip(), flush=True)
        return 1
    message = (
        f"Publish ETF flow dashboards {format_trade_key(dashboard_items[0][0])} to {format_trade_key(dashboard_items[-1][0])}"
        if range_start
        else f"Publish ETF flow dashboard {format_trade_key(trade_key)}"
    )
    commit = run_git(["git", "commit", "-m", message], cwd=worktree, env=git_env)
    if commit.returncode != 0:
        print(commit.stderr.strip(), flush=True)
        return 1
    push = run_git(["git", "push", remote_name, branch], cwd=worktree, env=git_env)
    if push.returncode != 0:
        print("[pages] push failed; check network and GitHub SSH configuration.", flush=True)
        print(push.stderr.strip(), flush=True)
        return 1
    commit_sha = git_output(["git", "rev-parse", "HEAD"], cwd=worktree, env=git_env)
    print(f"[pages] push succeeded: commit={commit_sha}", flush=True)
    return confirm_publish(args, remote_url, commit_sha, pages_url, dashboard_items, worktree)


def confirm_publish(
    args: argparse.Namespace,
    remote_url: str,
    commit_sha: str,
    pages_url: str,
    dashboard_items: list[tuple[str, Path]],
    worktree: Path,
) -> int:
    verify_online = None
    if pages_url:
        expected_dashboards = [
            (infer_pages_url(remote_url, key, version_token=file_version_token(path)), path.read_bytes())
            for key, path in dashboard_items
        ]
        verify_online = lambda: published_dashboards_state(expected_dashboards)
    deployment_check = wait_for_github_pages_deployment(
        remote_url,
        commit_sha,
        timeout_seconds=args.deployment_timeout_seconds,
        poll_seconds=args.deployment_poll_seconds,
        verify_online=verify_online,
    )
    if deployment_check.state == "success":
        print("[pages] GitHub Pages deployment succeeded.", flush=True)
    elif deployment_check.state == "content_verified":
        print(f"[pages] {deployment_check.message}", flush=True)
    elif deployment_check.state == "skipped":
        print(f"[pages] deployment check skipped: {deployment_check.message}", flush=True)
    elif deployment_check.state in FAILED_DEPLOYMENT_STATES:
        print(f"[pages] GitHub Pages deployment failed: {deployment_check.message or deployment_check.state}", flush=True)
        if deployment_check.target_url:
            print(f"[pages] deployment log={deployment_check.target_url}", flush=True)
        remove_tree(worktree, ignore_errors=True)
        return 1
    elif deployment_check.state:
        print(f"[pages] GitHub Pages deployment not confirmed: {deployment_check.message or deployment_check.state}", flush=True)
        if deployment_check.target_url:
            print(f"[pages] deployment log={deployment_check.target_url}", flush=True)
        remove_tree(worktree, ignore_errors=True)
        return 2

    print(f"[pages] published {dashboard_items[-1][0]}.", flush=True)
    if pages_url:
        print(f"[pages] latest={pages_url}", flush=True)
    remove_tree(worktree, ignore_errors=True)
    return 0


def prepare_publish_worktree(worktree: Path) -> Path:
    try:
        remove_tree(worktree)
        return worktree
    except OSError as exc:
        fallback = worktree.with_name(f"{worktree.name}_{os.getpid()}")
        ensure_safe_temp_path(fallback)
        print(
            f"[pages] warn: cannot remove old temp worktree {worktree}: {exc}. "
            f"Use {fallback.name} for this publish.",
            flush=True,
        )
        if fallback.exists():
            remove_tree(fallback)
        return fallback


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    if not path.exists():
        return

    def make_writable_and_retry(function, raw_path, exc_info):  # noqa: ANN001
        try:
            os.chmod(raw_path, stat.S_IREAD | stat.S_IWRITE)
            function(raw_path)
        except OSError:
            if not ignore_errors:
                raise

    try:
        shutil.rmtree(path, onerror=make_writable_and_retry)
    except OSError:
        if not ignore_errors:
            raise


def resolve_publish_repo_url(cli_repo_url: str, config_repo_url: str, remote_name: str, git_env: dict[str, str]) -> str:
    repo_url = str(cli_repo_url or "").strip() or str(config_repo_url or "").strip()
    if repo_url:
        return repo_url
    return git_output(["git", "remote", "get-url", remote_name], cwd=PROJECT_ROOT, env=git_env)


def resolve_dashboard_path(
    output_dir: Path,
    *,
    trade_date: str = "",
    dashboard: str = "",
    current_date: object = None,
    current_datetime: object = None,
) -> Path:
    if str(dashboard or "").strip():
        path = Path(dashboard).resolve()
    elif str(trade_date or "").strip():
        trade_key = normalize_trade_key(trade_date)
        if trade_key == _current_trade_key(current_date=current_date, current_datetime=current_datetime):
            now = _current_request_datetime(current_date=current_date, current_datetime=current_datetime)
            if not is_after_market_date_cutoff(now):
                return latest_dashboard_path(output_dir, max_trade_key=_previous_calendar_key(trade_key)).resolve()
        path = (output_dir / "flow_monitor" / trade_key / "etf_flow_dashboard.html").resolve()
        if not path.exists():
            return latest_dashboard_path(output_dir, max_trade_key=trade_key).resolve()
    else:
        path = latest_dashboard_path(output_dir).resolve()
    if not path.exists():
        raise FileNotFoundError(f"dashboard not found: {path}")
    return path


def latest_dashboard_path(output_dir: Path, *, max_trade_key: str = "") -> Path:
    base = output_dir / "flow_monitor"
    candidates = []
    if base.exists():
        for folder in base.iterdir():
            if folder.is_dir() and re.fullmatch(r"\d{8}", folder.name):
                if max_trade_key and folder.name > max_trade_key:
                    continue
                dashboard = folder / "etf_flow_dashboard.html"
                if dashboard.exists():
                    candidates.append((folder.name, dashboard))
    if not candidates:
        suffix = f" on or before {max_trade_key}" if max_trade_key else ""
        raise FileNotFoundError(f"no generated dashboards found under {base}{suffix}")
    return sorted(candidates, key=lambda item: item[0])[-1][1]


def resolve_dashboard_range(output_dir: Path, *, range_start: str, range_end: str) -> list[tuple[str, Path]]:
    start_key = normalize_trade_key(range_start)
    end_key = normalize_trade_key(range_end)
    if end_key < start_key:
        start_key, end_key = end_key, start_key
    base = output_dir / "flow_monitor"
    candidates: list[tuple[str, Path]] = []
    if base.exists():
        for folder in base.iterdir():
            if folder.is_dir() and re.fullmatch(r"\d{8}", folder.name) and start_key <= folder.name <= end_key:
                dashboard = folder / "etf_flow_dashboard.html"
                if dashboard.exists():
                    candidates.append((folder.name, dashboard.resolve()))
    if not candidates:
        raise FileNotFoundError(f"no generated dashboards found under {base} between {start_key} and {end_key}")
    return sorted(candidates, key=lambda item: item[0])


def normalize_trade_key(value: object) -> str:
    return normalize_date_input(value, field_name="trade_date").strftime("%Y%m%d")


def _current_trade_key(*, current_date: object = None, current_datetime: object = None) -> str:
    return _current_request_datetime(current_date=current_date, current_datetime=current_datetime).strftime("%Y%m%d")


def _current_request_datetime(*, current_date: object = None, current_datetime: object = None) -> datetime:
    if current_datetime is not None:
        return get_shanghai_now(current_datetime)
    if current_date is not None:
        return datetime.combine(normalize_date_input(current_date, field_name="current_date"), datetime_time.min)
    return get_shanghai_now()


def _previous_calendar_key(value: object) -> str:
    return (normalize_date_input(value, field_name="trade_date") - timedelta(days=1)).strftime("%Y%m%d")


def infer_trade_key(dashboard_path: Path) -> str:
    for part in reversed(dashboard_path.parts):
        if re.fullmatch(r"\d{8}", part):
            return part
    raise ValueError(f"cannot infer YYYYMMDD trade date from {dashboard_path}")


def stage_dashboard(worktree: Path, dashboard_path: Path, trade_key: str) -> None:
    stage_dashboards(worktree, [(trade_key, dashboard_path)])


def stage_dashboards(worktree: Path, dashboard_items: list[tuple[str, Path]]) -> None:
    for trade_key, dashboard_path in dashboard_items:
        reports_dir = worktree / "reports" / trade_key
        reports_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dashboard_path, reports_dir / "index.html")
    (worktree / ".nojekyll").write_text("", encoding="utf-8")
    reports_root = worktree / "reports"
    write_reports_index(reports_root)
    latest_key = latest_report_key(reports_root)
    shutil.copy2(reports_root / latest_key / "index.html", worktree / "index.html")


def write_reports_index(reports_dir: Path) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_keys = sorted_report_keys(reports_dir, reverse=True)
    items = "\n".join(
        f'        <li><a href="./{escape(key)}/?v={escape(report_version_token(reports_dir, key))}">'
        f"{escape(format_trade_key(key))}</a></li>"
        for key in report_keys
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ETF Flow Monitor Reports</title>
  <style>
    body {{ margin: 32px; font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif; color: #111827; }}
    h1 {{ margin: 0 0 18px; font-size: 28px; }}
    ul {{ padding-left: 20px; line-height: 1.9; }}
    a {{ color: #0369a1; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>ETF Flow Monitor Reports</h1>
  <ul>
{items}
  </ul>
</body>
</html>
"""
    (reports_dir / "index.html").write_text(html, encoding="utf-8")


def latest_report_key(reports_dir: Path) -> str:
    report_keys = sorted_report_keys(reports_dir, reverse=True)
    if not report_keys:
        raise FileNotFoundError(f"no report directories found under {reports_dir}")
    return report_keys[0]


def report_version_token(reports_dir: Path, report_key: str) -> str:
    report_path = reports_dir / report_key / "index.html"
    if not report_path.exists():
        return report_key
    return file_version_token(report_path)


def file_version_token(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:12]


def sorted_report_keys(reports_dir: Path, *, reverse: bool = False) -> list[str]:
    if not reports_dir.exists():
        return []
    return sorted(
        (path.name for path in reports_dir.iterdir() if path.is_dir() and re.fullmatch(r"\d{8}", path.name)),
        reverse=reverse,
    )


def format_trade_key(trade_key: str) -> str:
    key = str(trade_key)
    return f"{key[0:4]}-{key[4:6]}-{key[6:8]}" if re.fullmatch(r"\d{8}", key) else key


def infer_pages_url(remote_url: str, trade_key: str, *, version_token: str = "") -> str:
    slug = infer_github_repo_slug(remote_url)
    if not slug:
        return ""
    owner, repo = slug
    version = f"?v={version_token}" if version_token else ""
    return f"https://{owner}.github.io/{repo}/reports/{trade_key}/{version}"


FAILED_DEPLOYMENT_STATES = {"failure", "error", "cancelled", "timed_out", "action_required", "inactive"}
PENDING_DEPLOYMENT_STATES = {"", "waiting", "queued", "pending", "in_progress", "requested", "unknown", "rate_limited"}


@dataclass(frozen=True)
class DeploymentCheck:
    state: str
    message: str = ""
    target_url: str = ""
    environment_url: str = ""


def infer_github_repo_slug(remote_url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/?#]+)", remote_url.strip())
    if not match:
        return None
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return match.group("owner"), repo


def github_api_json(url: str, *, timeout_seconds: float = 10.0) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "etf-flow-monitor-pages-publisher",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    opener = build_opener(ProxyHandler({}))
    with opener.open(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def pages_http_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "etf-flow-monitor-pages-publisher", "Cache-Control": "no-cache"})
    with build_opener(ProxyHandler({})).open(request, timeout=10.0) as response:
        return response.read()


def published_dashboard_state(
    pages_url: str,
    expected_content: bytes,
    *,
    http_get_bytes: Callable[[str], bytes] = pages_http_bytes,
) -> DeploymentCheck:
    actual_content = http_get_bytes(pages_url)
    # Git core.autocrlf can change CRLF to LF when the dashboard is pushed.
    if actual_content.replace(b"\r\n", b"\n") != expected_content.replace(b"\r\n", b"\n"):
        return DeploymentCheck("pending", "Online dashboard content does not match the local report yet")
    return DeploymentCheck(
        "content_verified",
        "Dashboard verified online: content matches the local report; deployment API confirmation unavailable.",
        environment_url=pages_url,
    )


def published_dashboards_state(
    expected_dashboards: list[tuple[str, bytes]],
    *,
    http_get_bytes: Callable[[str], bytes] = pages_http_bytes,
) -> DeploymentCheck:
    if not expected_dashboards:
        return DeploymentCheck("pending", "No dashboards provided for online verification")

    def check_one(item: tuple[str, bytes]) -> DeploymentCheck:
        url, content = item
        try:
            check = published_dashboard_state(url, content, http_get_bytes=http_get_bytes)
            if check.state != "content_verified":
                return DeploymentCheck("pending", f"{url}: {check.message}")
            return check
        except Exception as exc:  # noqa: BLE001
            return DeploymentCheck("pending", f"{url}: Online dashboard check failed: {exc}")

    # Bound simultaneous requests; every report must match, including older dates.
    matched = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for offset in range(0, len(expected_dashboards), 3):
            checks = list(pool.map(check_one, expected_dashboards[offset:offset + 3]))
            failures = [check for check in checks if check.state != "content_verified"]
            matched += len(checks) - len(failures)
            if failures:
                return DeploymentCheck(
                    "pending",
                    f"Online dashboards matched {matched}/{len(expected_dashboards)}; {failures[0].message}",
                )
    return DeploymentCheck(
        "content_verified",
        f"All {matched} dashboard(s) verified online: content matches local reports; deployment API confirmation unavailable.",
        environment_url=expected_dashboards[-1][0],
    )


def github_rate_limit_message(exc: HTTPError) -> str:
    headers = exc.headers or {}
    limited = exc.code == 429 or (
        exc.code == 403
        and (headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After") or "rate limit" in str(exc).lower())
    )
    if not limited:
        return ""
    message = f"GitHub API rate limited: {exc}; API polling stopped for this run"
    if headers.get("Retry-After"):
        message += f"; Retry-After={headers['Retry-After']} seconds"
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            message += f"; quota resets at {datetime.fromtimestamp(float(reset)).astimezone().isoformat()}"
        except (ValueError, OverflowError, OSError):
            pass
    return message


def latest_github_pages_deployment_state(
    remote_url: str,
    commit_sha: str,
    *,
    http_get_json: Callable[[str], Any] = github_api_json,
) -> DeploymentCheck:
    slug = infer_github_repo_slug(remote_url)
    if not slug:
        return DeploymentCheck("skipped", "remote is not a GitHub repository")
    owner, repo = slug
    query = urlencode({"sha": commit_sha, "environment": "github-pages", "per_page": "5"})
    deployments_url = f"https://api.github.com/repos/{owner}/{repo}/deployments?{query}"
    deployments = http_get_json(deployments_url)
    if not isinstance(deployments, list):
        raise RuntimeError("GitHub deployments API returned an unexpected payload")
    if not deployments:
        return DeploymentCheck("pending", "GitHub Pages deployment has not been created yet")

    deployment = deployments[0]
    statuses_url = str(deployment.get("statuses_url") or "")
    if not statuses_url:
        return DeploymentCheck("pending", "GitHub Pages deployment has no statuses URL yet")
    statuses = http_get_json(statuses_url)
    if not isinstance(statuses, list):
        raise RuntimeError("GitHub deployment statuses API returned an unexpected payload")
    if not statuses:
        return DeploymentCheck("pending", "GitHub Pages deployment has no status yet")

    status = statuses[0]
    state = str(status.get("state") or "").lower()
    message = str(status.get("description") or state or "unknown")
    return DeploymentCheck(
        state=state or "unknown",
        message=message,
        target_url=str(status.get("target_url") or status.get("log_url") or ""),
        environment_url=str(status.get("environment_url") or ""),
    )


def wait_for_github_pages_deployment(
    remote_url: str,
    commit_sha: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    http_get_json: Callable[[str], Any] = github_api_json,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    verify_online: Callable[[], DeploymentCheck] | None = None,
) -> DeploymentCheck:
    if timeout_seconds <= 0:
        return DeploymentCheck("skipped", "deployment wait disabled")
    slug = infer_github_repo_slug(remote_url)
    if not slug:
        return DeploymentCheck("skipped", "remote is not a GitHub repository")

    deadline = monotonic() + timeout_seconds
    last_check = DeploymentCheck("pending", "waiting for GitHub Pages deployment")
    checked_online_early = False
    actions_url = f"https://github.com/{slug[0]}/{slug[1]}/actions"
    while True:
        if last_check.state != "rate_limited":
            try:
                last_check = latest_github_pages_deployment_state(remote_url, commit_sha, http_get_json=http_get_json)
            except HTTPError as exc:
                rate_limit_message = github_rate_limit_message(exc)
                last_check = DeploymentCheck(
                    "rate_limited" if rate_limit_message else "unknown",
                    rate_limit_message or f"GitHub Pages deployment check failed: {exc}",
                    actions_url,
                )
            except Exception as exc:  # noqa: BLE001
                last_check = DeploymentCheck("unknown", f"GitHub Pages deployment check failed: {exc}")
        if last_check.state == "success" or last_check.state in FAILED_DEPLOYMENT_STATES:
            return last_check
        if last_check.state not in PENDING_DEPLOYMENT_STATES:
            return last_check
        timed_out = monotonic() >= deadline
        online_message = ""
        if verify_online and (timed_out or last_check.state == "rate_limited" or (last_check.state == "unknown" and not checked_online_early)):
            checked_online_early = True
            try:
                online_check = verify_online()
                if online_check.state == "content_verified":
                    return DeploymentCheck(
                        "content_verified",
                        f"{online_check.message} Last API state={last_check.state}: {last_check.message}",
                        environment_url=online_check.environment_url,
                    )
                online_message = online_check.message
            except Exception as exc:  # noqa: BLE001
                online_message = f"Online dashboard check failed: {exc}"
        if last_check.state == "rate_limited" and (not verify_online or timed_out or monotonic() >= deadline):
            message = last_check.message + (f"; {online_message}" if online_message else "")
            return DeploymentCheck("rate_limited", message, actions_url)
        if timed_out or monotonic() >= deadline:
            message = (
                f"GitHub Pages deployment was not confirmed within {timeout_seconds:g}s; "
                f"last state={last_check.state}: {last_check.message}"
            )
            if online_message:
                message += f"; {online_message}"
            return DeploymentCheck("timeout", message, last_check.target_url or actions_url, last_check.environment_url)
        sleep(min(max(float(poll_seconds), 0.5), max(deadline - monotonic(), 0.0)))


def ensure_safe_temp_path(path: Path) -> None:
    root = PROJECT_ROOT.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing to use worktree outside project root: {resolved}")
    if not resolved.name.lower().startswith(("tmp", "temp")):
        raise ValueError(f"publish worktree must be a temp directory: {resolved}")


def git_output(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = run_git(command, cwd=cwd, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def run_git(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"[pages] publish failed: {exc}", flush=True)
        raise SystemExit(1)
