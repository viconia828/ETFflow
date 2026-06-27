"""Publish generated ETF dashboard HTML files to the gh-pages branch."""

from __future__ import annotations

import argparse
import hashlib
from html import escape
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from etf_flow_monitor.config import load_config  # noqa: E402
from etf_flow_monitor.utils.calendar import normalize_date_input  # noqa: E402
from etf_flow_monitor.utils.proxy import proxy_bypass_env  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish ETF flow dashboard to GitHub Pages.")
    parser.add_argument("--config", default="config.txt")
    parser.add_argument("--trade-date", default="", help="Report date YYYYMMDD. Blank means latest generated dashboard.")
    parser.add_argument("--dashboard", default="", help="Explicit dashboard HTML path.")
    parser.add_argument("--repo-url", default="", help="Target Git repository URL. Defaults to config pages_repo_url, then git remote.")
    parser.add_argument("--remote", default="", help="Remote name used inside the temporary clone. Defaults to origin.")
    parser.add_argument("--branch", default="", help="Pages branch. Defaults to config pages_branch.")
    parser.add_argument("--worktree", default="tmp_pages_publish")
    parser.add_argument("--dry-run", action="store_true", help="Resolve inputs and print intended publish action without git operations.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    git_env = proxy_bypass_env(bypass_git_ssh_proxy=True)
    requested_trade_key = normalize_trade_key(args.trade_date) if str(args.trade_date or "").strip() else ""
    dashboard_path = resolve_dashboard_path(config.output_dir, trade_date=args.trade_date, dashboard=args.dashboard)
    if str(args.dashboard or "").strip() and requested_trade_key:
        trade_key = requested_trade_key
    else:
        trade_key = infer_trade_key(dashboard_path)
    version_token = file_version_token(dashboard_path)
    remote_name = str(args.remote or "").strip() or "origin"
    branch = str(args.branch or "").strip() or config.pages_branch
    remote_url = resolve_publish_repo_url(args.repo_url, config.pages_repo_url, remote_name, git_env)
    pages_url = infer_pages_url(remote_url, trade_key, version_token=version_token)

    print(f"[pages] dashboard={dashboard_path}", flush=True)
    if requested_trade_key and requested_trade_key != trade_key:
        print(f"[pages] requested_trade_date={requested_trade_key} resolved_trade_date={trade_key}", flush=True)
    print(f"[pages] repo={remote_url}", flush=True)
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

    stage_dashboard(worktree, dashboard_path, trade_key)
    status = git_output(["git", "status", "--porcelain"], cwd=worktree, env=git_env)
    if not status.strip():
        print("[pages] no page changes to publish.", flush=True)
        remove_tree(worktree, ignore_errors=True)
        return 0

    add = run_git(["git", "add", "."], cwd=worktree, env=git_env)
    if add.returncode != 0:
        print(add.stderr.strip(), flush=True)
        return 1
    message = f"Publish ETF flow dashboard {format_trade_key(trade_key)}"
    commit = run_git(["git", "commit", "-m", message], cwd=worktree, env=git_env)
    if commit.returncode != 0:
        print(commit.stderr.strip(), flush=True)
        return 1
    push = run_git(["git", "push", remote_name, branch], cwd=worktree, env=git_env)
    if push.returncode != 0:
        print("[pages] push failed; check network and GitHub SSH configuration.", flush=True)
        print(push.stderr.strip(), flush=True)
        return 1

    print(f"[pages] published {trade_key}.", flush=True)
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


def resolve_dashboard_path(output_dir: Path, *, trade_date: str = "", dashboard: str = "") -> Path:
    if str(dashboard or "").strip():
        path = Path(dashboard).resolve()
    elif str(trade_date or "").strip():
        trade_key = normalize_trade_key(trade_date)
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


def normalize_trade_key(value: object) -> str:
    return normalize_date_input(value, field_name="trade_date").strftime("%Y%m%d")


def infer_trade_key(dashboard_path: Path) -> str:
    for part in reversed(dashboard_path.parts):
        if re.fullmatch(r"\d{8}", part):
            return part
    raise ValueError(f"cannot infer YYYYMMDD trade date from {dashboard_path}")


def stage_dashboard(worktree: Path, dashboard_path: Path, trade_key: str) -> None:
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
    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", remote_url.strip())
    if not match:
        return ""
    owner = match.group("owner")
    repo = match.group("repo")
    version = f"?v={version_token}" if version_token else ""
    return f"https://{owner}.github.io/{repo}/reports/{trade_key}/{version}"


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
