"""Proxy-bypass helpers for data fetches and publish subprocesses."""

from __future__ import annotations

import os
from typing import Mapping

PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
)

DEFAULT_NO_PROXY_HOSTS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "api.tushare.pro",
    "tushare.pro",
    "github.com",
    "ssh.github.com",
)


def proxy_bypass_env(
    base_env: Mapping[str, str] | None = None,
    *,
    extra_no_proxy: tuple[str, ...] = (),
    bypass_git_ssh_proxy: bool = False,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    for key in PROXY_ENV_KEYS:
        env.pop(key, None)
    no_proxy_hosts = _merge_no_proxy_hosts(DEFAULT_NO_PROXY_HOSTS + tuple(extra_no_proxy), env.get("NO_PROXY"), env.get("no_proxy"))
    env["NO_PROXY"] = no_proxy_hosts
    env["no_proxy"] = no_proxy_hosts
    if bypass_git_ssh_proxy:
        env["GIT_SSH_COMMAND"] = "ssh -o ProxyCommand=none -o ProxyJump=none"
    return env


def _merge_no_proxy_hosts(default_hosts: tuple[str, ...], *existing_values: str | None) -> str:
    ordered: list[str] = []
    for value in list(default_hosts) + [part for raw in existing_values if raw for part in str(raw).split(",")]:
        text = str(value).strip()
        if text and text not in ordered:
            ordered.append(text)
    return ",".join(ordered)
