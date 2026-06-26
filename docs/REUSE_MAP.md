# Reuse Map

| starter file | source inspiration | notes |
| --- | --- | --- |
| `src/etf_flow_monitor/data/tushare_http.py` | `动量策略v2ex/etf_momentum/data/tushare_http.py` | Kept as a dependency-light urllib client with secret loading, retry, rate-limit handling, and proxy bypass. |
| `src/etf_flow_monitor/data/cache_store.py` | `现金增强辅助策略/data/cache_store.py` | Extended with daily cross-section and manifest helpers. |
| `src/etf_flow_monitor/utils/calendar.py` | `动量策略v2ex/etf_momentum/utils/calendar.py` | Kept official-calendar checks and request-date semantics. |
| `src/etf_flow_monitor/utils/logger.py` | `动量策略v2ex/etf_momentum/utils/logger.py` | Small standalone logging wrapper. |
| `src/etf_flow_monitor/utils/io.py` | `现金增强辅助策略/data/utils.py` | Safe filename, date formatting, merge helpers, JSON writes. |
| `src/etf_flow_monitor/run_ledger.py` | `动量策略v2ex/etf_momentum/dev/cache_update_console.py` | Simplified `run.json` ledger for monitor runs. |
| `AGENTS.md` | root `AGENTS.md` and `AGENTS.local.md` conventions | Adapted for a new ETF flow monitor project. |
| `资金流监控.bat` | v2/v2ex BAT launchers | Keeps UTF-8, runtime probing, and `PYTHONPATH=src`. |

## Boundaries

The package intentionally does not include strategy selection, portfolio
construction, backtest execution, off-exchange execution, or parameter tuning
modules. Those are strategy-specific and should remain outside a market-flow
monitor.
