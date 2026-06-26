# ETF 全市场资金流监控 starter 包

这是从 `D:\momentum` 动量策略项目里拆出来的可拷贝 starter。它的目标不是复制动量策略，而是复用其中比较成熟的工程零件，快速启动一个“每日全市场 ETF 资金流向 / 活跃度监控”项目。

## 已打包内容

- `src/etf_flow_monitor/data/tushare_http.py`  
  Tushare HTTP 客户端：本地 secret/env 读取、限速、重试、频率超限处理、代理绕过。
- `src/etf_flow_monitor/data/cache_store.py`  
  轻量 CSV/JSON 缓存：calendar、static、per-code time series、daily cross-section、manifest。
- `src/etf_flow_monitor/utils/calendar.py`  
  官方交易日历校验、交易日偏移、请求日到市场日解析。
- `src/etf_flow_monitor/run_ledger.py`  
  `run.json` 运行账本：PID、阶段、输出、错误、统计。
- `src/etf_flow_monitor/data/tushare_etf_source.py`  
  ETF 基础信息、日行情、份额数据的 Tushare source starter。
- `src/etf_flow_monitor/monitor/flow_metrics.py`  
  第一版资金流 proxy：用 ETF 份额变化乘价格估算净流入；成交额作为辅助活跃度信号。
- `src/etf_flow_monitor/monitor/report.py`  
  Markdown 报告输出。
- `AGENTS.md`  
  适配新项目的 Codex/agent 工作规范。
- `资金流监控.bat`  
  Windows 一键入口。

## 快速开始

1. 复制整个 `etf_flow_monitor_starter/` 到新位置。
2. 复制 `.local_secrets.example.json` 为 `.local_secrets.local.json`，填入 Tushare token。
3. 如需改参数，复制 `config.example.txt` 为 `config.local.txt`。
4. 双击 `资金流监控.bat`，或在 PowerShell 中运行：

BAT 会按顺序寻找可导入 `pandas` 的运行时：

- 环境变量 `ETF_FLOW_PYTHON`
- 环境变量 `MOMENTUM_PYTHON`
- Codex bundled Python
- 系统 `python`

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.example.txt
```

双击 BAT 时会提示输入监控日期：

```text
Trade date YYYYMMDD, blank = default:
```

指定日期的命令行写法：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.example.txt --trade-date 20260625
```

只测试工程链路、不拉远端数据：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.example.txt --dry-run
```

## 输出结构

一次成功运行会写：

- `outputs/logs/flow_monitor_YYYYMMDD_HHMMSS/run.json`
- `outputs/flow_monitor/YYYYMMDD/etf_flow_snapshot.csv`
- `outputs/flow_monitor/YYYYMMDD/market_summary.csv`
- `outputs/flow_monitor/YYYYMMDD/etf_flow_report.md`
- `outputs/flow_monitor/YYYYMMDD/etf_flow_dashboard.html`

## 可视化监控器

`etf_flow_dashboard.html` 是单日静态自包含页面：

- 内嵌 CSS、SVG 图形、交互脚本和本次运行的数据；
- 不依赖外部 CDN、本地 CSV、图片路径或后台服务；
- 不包含 Tushare token 或本地 secret；
- 可以直接发群分享，对方用浏览器打开即可查看。

第一版包含三个视图：

- `走势`：全市场 ETF 估算净额趋势、摘要卡、周期切换。
- `轮动`：按 ETF 类型聚合的资金流入/流出条形图、自动解读。
- `明细`：请求日 ETF 流入前十、流出前十，以及本次报表口径说明。

周期按钮包括 `最近1日 / 1周 / 2周 / 1月 / 3月 / 6月 / 今年来 / 12月`。默认 `lookback_days = 370`，用于支撑较长周期页面；如果首次拉取太慢，可在 `config.local.txt` 里调小。

## ETF 分类表

先用 Tushare `fund_basic` 建缓存并生成可编辑分类表：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\build_etf_category_map.py --config config.example.txt --refresh
```

输出：

- `data/cache/tushare/static/etf_basic_E.csv`
- `data/local_reference/etf_category_map.csv`

分类表会按 `fund_type / benchmark / name` 预填 `category` 和 `subcategory`，并保留 `fund_type`、`benchmark`、`status`、`management` 等源字段方便人工校正。默认只输出当前上市且符合 ETF 候选规则的品种；如果分类表已存在，重跑时会保留 `category / subcategory / review_note` 的人工修改。

用 Tushare 申万行业指数进一步细化行业 ETF：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\refine_category_map_with_sw.py --config config.example.txt
```

该脚本会缓存 `index_classify(src=SW2021)` 到：

- `data/cache/tushare/static/sw_index_classify_SW2021.csv`

匹配成功时，`category` 使用申万一级行业，`subcategory` 使用命中的申万二级/三级行业，并写入 `sw_index_code / sw_industry_name / sw_level / sw_match_term / sw_match_rule` 方便人工复核。脚本会跳过债券、货币、港股、海外、商品、红利价值，以及已识别的宽基指数；“上证综合/创业板综合/科创板综合”不会因“综合”二字被误分到申万综合行业，“上证50/上证180/中证500”等宽基也不会因为交易所名称里的“证券”被误分到证券行业。

主程序会读取 `category_map_path`，并将 `category / subcategory / review_note` 合并进 `etf_flow_snapshot.csv`。Dashboard 的资金轮动主图按 `category` 聚合，明细表显示 `category / subcategory`，但分类维护清单只保留在本地 CSV/校验输出中，不放进分享页面。

校验分类表：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\validate_category_map.py --config config.example.txt
```

校验结果会写入：

- `outputs/category_map_validation/category_map_validation_YYYYMMDD_HHMMSS.json`

## 数据源体检

抽样检查 Tushare 数据源可用性：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\check_tushare_source_health.py --config config.example.txt --trade-date 20260625 --sample-size 2
```

体检结果会写入：

- `outputs/source_health/tushare_source_health_YYYYMMDD_HHMMSS.json`

检查项包括 `trade_cal`、`fund_basic`、`fund_daily`、`fund_share`，用于确认接口权限、缓存命中、行情行数、份额数据行数和份额量级。

确认 Tushare 是否支持按交易日批量取数：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\probe_tushare_fetch_shapes.py --config config.example.txt --trade-date 20260625 --sample-code 510300.SH
```

当前已确认 `fund_daily(trade_date=YYYYMMDD)` 和 `fund_share(trade_date=YYYYMMDD)` 可以返回当日横截面。主程序优先按官方交易日列表逐日拉取横截面，并缓存到：

- `data/cache/tushare/daily_cross_section/etf_daily/YYYYMMDD.csv`
- `data/cache/tushare/daily_cross_section/etf_share/YYYYMMDD.csv`

如果横截面接口失败，程序会回退到旧的逐代码 time series 路径。默认 `lookback_days = 370` 时，冷启动请求量从“代码数 × 2 类数据”降为“交易日数 × 2 类数据”；以后每天增量通常只新增当日行情和当日份额两个横截面缓存。

2026-06-25 样本实测：旧逐代码路径约 8 分 26 秒；切换横截面后，默认 370 天 lookback 冷横截面缓存约 2 分 43 秒，热缓存约 31 秒。热缓存阶段主要耗时来自读取数百个本地 CSV、构建 33 万行明细和写出大 CSV。

单独更新历史缓存：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u tools\update_flow_cache.py --config config.example.txt --trade-date 20260625
```

`资金流监控.bat` 会先调用这个 helper，再生成 HTML。启动时会披露：

- 请求日期和解析后的市场日期；
- 当前缓存截止日；
- 如果已更新至目标市场日，直接生成页面；
- 如果未更新至目标市场日，先补齐缺失横截面，更新时每 15 秒输出一次心跳。
- 如果远端取数失败，不会直接中断 BAT；界面会显示本地完整缓存区间，并继续尝试仅使用本地缓存生成。
- 如果请求日期或统计窗口超出本地缓存区间，程序会跳过生成并提示缺少的行情/份额交易日数量。

BAT 的页面生成阶段使用 `--cache-only`，不会在生成报表时再次访问远端数据源。

## 交易日解析

真实运行时会使用 Tushare `trade_cal` 官方交易日历：

- 命令行输入 `--trade-date YYYYMMDD`：如果该日不是交易日，会映射到上一交易日，并写入 `run.json`。
- BAT 留空日期：解析为最近可用市场日，避免直接使用自然日或业务日历。
- `--dry-run` 不联网，使用请求日期本身，仅验证工程输出链路。

## 资金流口径说明

当前 starter 的 `estimated_net_flow` 是工程占位口径：

```text
estimated_net_flow = share_change * 10000 * flow_price
```

其中 `share_change` 来自 Tushare `fund_share.fd_share`，当前按“万份”处理；`flow_price` 是资金流估算价格。普通 ETF（含债券 ETF）使用场内收盘价；`fund_type=货币型` 且场内报价在 100 元附近的货币 ETF 使用 `close / 100`，避免把每份约 1 元的货币基金按 100 元报价放大。`estimated_net_flow` 输出单位为元，页面再换算为亿元。

Tushare `fund_daily.amount` 当前按“千元”规范化为元后进入 `amount` 字段；CSV、Markdown 和 HTML 中显示为亿元时均基于规范化后的元值。

后续必须继续确认：

- 是否为上市 ETF 总份额，是否混入 ETF 联接、LOF、封闭基金或其他场内基金份额；
- 复权、拆分、份额折算、基金转型、清盘等生命周期事件，需要从 ETF 公告建立生命周期表核对；
- `fund_daily.vol` 的单位仍需同步复核，避免成交量展示单位错位。

已按 A 股、港股通、QDII 样本核对：`fund_share.trade_date=T` 按 T 日份额处理。`港股` 和 `海外` 类 ETF 遇到底层市场交易日历或披露错位导致某个 A 股交易日缺行情/份额时，会使用该基金前一可用交易日数据前值填充，避免从当日报表中消失。

如果份额数据不可用，报告仍会输出成交额、涨跌幅、成交量排名，但 `estimated_net_flow` 会退化为 0。

## 推荐下一步

1. 建立 ETF 生命周期表，核对拆分、折算、转型、清盘等公告事件。
2. 增加 ETF 分类映射：宽基、行业、主题、跨境、债券、货币、商品。
3. 增加聚合报告：按分类的净流入、成交额、连续流入天数、异常放量。
4. 加一个每日调度入口：收盘后跑一次，晚间复核一次。
5. 用生命周期表和 T 日更新时间校验把 `estimated_net_flow` 升级成可长期复核的正式口径。

## 不建议直接搬的东西

- 动量策略的调仓、组合构建、回测执行、场外交易语义。
- v2ex 完整 PIT / Plan Seal 回测缓存治理。资金流监控第一版用轻量 cache + manifest 足够，后续如果要做严格历史复现，再逐步升级。
