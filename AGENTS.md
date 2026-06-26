# Project Instructions

## Scope

This project monitors daily whole-market ETF fund-flow and activity signals.
Keep it separate from strategy execution. Reports may inform research, but this
starter should not place orders, rebalance portfolios, or mutate strategy
positions.

## Runtime Policy

Use the Python runtime that already has the project dependencies installed on
the current machine. Do not create a new virtual environment or install a
second dependency stack unless the user explicitly asks.

Before running source-fetching or long-running monitor jobs, verify the runtime:

```powershell
python -c "import sys,pandas; print(sys.executable); print(pandas.__version__)"
```

Secrets belong in environment variables or `.local_secrets.local.json`, never in
committed files.

## Data Policy

Treat exchange calendars as official data. Do not silently replace them with a
business-day calendar.

Cache remote source responses before building reports. If a source is missing or
rate-limited, write a clear run ledger entry and stop or degrade explicitly.

The initial `estimated_net_flow` metric is a starter proxy based on share
outstanding changes. Before production use, verify the source field, unit, and
adjustment semantics for ETF shares.

## Long-Running Jobs

Every scheduled or manual monitor run should write:

- `outputs/logs/<run_id>/run.json`
- stdout/stderr when launched from an external scheduler
- report artifacts under `outputs/flow_monitor/YYYYMMDD/`

Do not let a monitor job run unattended for more than 30 minutes without
checking `run.json`, stderr, latest output writes, and process resource use.

## Work Logs

Keep project notes under:

- `docs/work_logs/工作进度_YYYY-MM-DD.md`
- `docs/work_logs/下一步工作list_YYYY-MM-DD.md`

Record source changes, cache schema changes, metric formula changes, output
paths, and rejected data-source assumptions.

## Chinese Text And Encoding

Use UTF-8 for Chinese filenames and Markdown. On Windows PowerShell, prefer:

```powershell
Get-Content <path> -Encoding UTF8
```

Do not rewrite Chinese documents solely because the terminal displayed mojibake.
