# ETF 全市场资金流监控使用说明

这是一个用于每日查看全市场 ETF 资金流向和活跃度的本地工具。它会从 Tushare 获取 ETF 行情和份额数据，缓存到本机，生成 Markdown 报告和可分享的静态网页。

本工具只做监控和研究辅助，不会下单、调仓、修改持仓，也不会连接任何交易系统。

## 你可以用它做什么

- 每天生成一张 ETF 全市场资金流 dashboard。
- 查看最近 1 日、1 周、2 周、1 月、3 月、6 月、今年来、12 月的资金流变化。
- 在页面中切换 `走势 / 轮动 / 明细` 三个视图。
- 查看 ETF 流入前十、流出前十，以及分类资金轮动。
- 自动发布最新页面到配置的 GitHub Pages 仓库。
- 在本地保留缓存，日常运行时只补齐新增交易日数据。

## 首次使用

1. 确认本机已有可用 Python 环境。

   双击 `资金流监控.bat` 时，程序会自动按顺序寻找：

   - 环境变量 `ETF_FLOW_PYTHON`
   - 环境变量 `MOMENTUM_PYTHON`
   - Codex bundled Python
   - 系统 `python`

2. 配置 Tushare token。

   复制 `.local_secrets.example.json` 为 `.local_secrets.local.json`，填入自己的 Tushare token。不要把 `.local_secrets.local.json` 提交到 Git。

3. 检查 `config.txt`。

   `config.txt` 是正式配置文件，里面的注释已经按普通用户阅读方式写成中文。最常改的是：

   - `local_cache_start_date`：用户可生成页面的最早日期，以 `config.txt` 当前值为准。
   - `category_map_path`：ETF 分类表路径，默认使用本地维护表。
   - `announcement_file_path`：ETF 公告原始表路径。
   - `lifecycle_events_path`：ETF 生命周期事件表路径。
   - `pages_repo_url`：GitHub Pages 自动发布的目标仓库。
   - `pages_branch`：GitHub Pages 发布分支，默认 `gh-pages`。
   - 输出目录和缓存目录：一般不需要改。

4. 如需自动发布 GitHub Pages，确认本机已经配置好 GitHub SSH。

   自动发布依赖网络和 SSH。发布失败不会影响本地页面生成。

## 日常使用

最简单的方式是双击：

```text
资金流监控.bat
```

窗口会提示：

```text
Select run mode:
  1 = Build one daily report (default)
  2 = Build every trading-day report in a date range
Mode 1/2, blank = 1:
```

- 直接回车：选择模式 1，生成单日报告。
- 输入 `1`：同样生成单日报告。随后可以直接回车生成最近可用市场日，或输入 `20260625` 这样的日期生成指定日期页面；如果输入的是非交易日，程序会映射到上一交易日。
- 输入 `2`：生成起止日期之间所有交易日的报告。这个模式会花更久，预处理数据和逐日生成阶段都会持续输出 `[range heartbeat]` 心跳进度。

BAT 会按三步执行：

1. 更新本地缓存。单日模式按目标日期更新；区间模式按区间结束日更新。
2. 使用本地缓存生成报告和 dashboard。
3. 尝试发布到 GitHub Pages。区间模式会一次性发布区间内已经生成的 dashboard 归档。

第 1 步会显示交易日历尾部状态，例如：

```text
[cache] calendar auto-refresh: cached_tail=20260630 required_tail=20260707 action=cached
```

如果 `action=fetch`，表示本地官方交易日历不足，会自动向 Tushare 补拉到 `required_tail`。

页面生成阶段使用本地缓存，不会再次访问远端数据源。

区间模式会先做一段 P2 提速评估并打印摘要：朴素逐日生成需要读取多少个历史横截面、批量复用后实际读取多少、预计节约多少。区间模式默认采用明细输出瘦身：`etf_flow_snapshot.csv` 只写报表日当天明细，dashboard 和文字报告仍使用完整 12 个月历史窗口计算。

## 输出在哪里

一次成功运行后，主要文件在：

```text
outputs/flow_monitor/YYYYMMDD/
```

常用文件：

- `etf_flow_dashboard.html`：最适合打开和分享的静态网页。
- `etf_flow_report.md`：文字版报告。
- `etf_flow_snapshot.csv`：ETF 明细快照。
- `market_summary.csv`：市场汇总序列。

运行日志在：

```text
outputs/logs/flow_monitor_YYYYMMDD_HHMMSS/run.json
```

如果 BAT 窗口提示失败，优先看这个 `run.json`。

## 页面怎么看

打开 `etf_flow_dashboard.html` 后，可以在顶部切换视图：

- `走势`：全市场 ETF 估算净额趋势，以及口径说明。
- `轮动`：按主分类聚合的资金流入、流出和自动解读。
- `明细`：当前区间的 ETF 流入前十、流出前十。

区间按钮包括：

```text
最近1日 / 1周 / 2周 / 1月 / 3月 / 6月 / 今年来 / 12月
```

页面统一按金额展示，单位为亿元。明细页中的涨跌幅是区间涨跌幅，成交额和估算净额也是区间累计口径。

分类列默认只显示主分类，例如 `电子`；鼠标移到分类上时，会显示完整分类，例如 `电子 / 半导体材料`。

## GitHub Pages 自动发布

`资金流监控.bat` 生成页面成功后，会自动尝试发布到 `config.txt` 中配置的 Pages 仓库和分支：

- `index.html`：最新日报。
- `reports/YYYYMMDD/index.html`：指定日期归档。
- `reports/index.html`：归档入口。

归档入口按报告日期倒序排列，报告链接会自动带内容版本参数，例如 `?v=...`，避免浏览器继续打开旧缓存。如果以后补发较早日期的报告，首页 `index.html` 仍会保留为报告日期最新的一期，不会被补发的旧日报覆盖。

相关配置：

```text
pages_repo_url = git@github.com:viconia828/ETFflow.git
pages_branch = gh-pages
```

如果 `pages_repo_url` 留空，发布脚本会回退使用当前 Git 仓库的 `origin`。

如果输入日期是非交易日，生成阶段会自动映射到上一交易日；发布阶段也会跟随实际生成的交易日页面。例如输入 `20260619`，实际生成 `outputs/flow_monitor/20260618/` 时，发布归档也会使用 `reports/20260618/`。

发布失败时，BAT 窗口会显示：

```text
[WARN] Pages publish failed. Local dashboard was generated; check network, GitHub SSH, or temp publish directory permissions.
```

这只表示线上发布失败，本地 `outputs/flow_monitor/YYYYMMDD/` 下的页面仍然可用。

手动发布指定日期：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u tools\publish_pages.py --config config.txt --trade-date 20260625
```

也可以临时指定目标仓库：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u tools\publish_pages.py --config config.txt --trade-date 20260625 --repo-url git@github.com:owner/repo.git
```

## 代理和联网说明

很多电脑会配置系统代理。为了避免黑洞代理影响数据抓取或发布，BAT 会清理常见代理环境变量，并让 Tushare、巨潮资讯和交易所备用源下载绕过代理；Pages 发布脚本也会让 Git 子进程绕过代理配置。

如果仍然抓取失败，通常先检查：

- 当前网络是否能访问 Tushare。
- Tushare token 是否正确、权限是否足够。
- 是否有安全软件拦截 Python 或 Git。
- GitHub SSH 是否可以正常连接。

## 缓存起始日

`config.txt` 中的 `local_cache_start_date` 表示用户可生成页面的最早日期。这个值可以按你的本地缓存计划手动调整。

为了计算 12 个月区间，程序会在后台自动从该日期往前 12 个月，并额外包含前一交易日开始补缓存。日常运行时，如果本地缓存区间已经完整覆盖目标日期，程序会走快速检查，不逐日扫描全部历史。

如怀疑中间缓存文件被手工删除，可以手动做完整检查：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u tools\update_flow_cache.py --config config.txt --full-check
```

## ETF 分类维护

分类表在：

```text
data/local_reference/etf_category_map.csv
```

页面的资金轮动主图按 `category` 聚合。明细表默认显示 `category`，鼠标悬停显示完整 `category / subcategory`。

如果要重新生成分类表：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\build_etf_category_map.py --config config.txt --refresh
```

如果要用申万行业进一步细化行业 ETF：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\refine_category_map_with_sw.py --config config.txt
```

校验分类表：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\validate_category_map.py --config config.txt
```

校验结果会写入：

```text
outputs/category_map_validation/
```

## ETF 生命周期核对

份额拆分、份额折算、基金转型、清盘等事件可能造成非资金流性质的份额跳变。生命周期事件表在：

```text
data/local_reference/etf_lifecycle_events.csv
```

公告原始表模板在：

```text
data/local_reference/etf_announcements.csv
```

可以双击独立入口更新真实公告并生成生命周期核对报告：

```text
ETF公告更新.bat
```

这个 BAT 是重任务入口，不会被日常 `资金流监控.bat` 调用。它会按下面顺序运行：

1. 检查上一轮“窗口内未抓到公告”的跳变是否已经人工确认。
2. 检查生命周期状态是否已经覆盖到本地份额数据日期；如未覆盖，先审计本地份额跳变，并拆成高疑似待抓清单和低疑似观察清单。
3. 按高疑似待抓清单从巨潮资讯抓取基金公告，填入 `etf_announcements.csv`。
4. 用新公告重建生命周期事件表和本地份额跳变核对报告。

BAT 窗口默认只显示简要进度和关键数量，详细 JSON 摘要会写入 `outputs/lifecycle_audit/`；排查问题时可以给对应 Python 命令加 `--verbose`。

公告抓取会在每个请求窗口之间留出间隔；遇到连接被远端断开时会自动重试。若仍有部分窗口没有抓完，BAT 不会把这件事藏起来：结束前的 `[summary]` 会显示公告抓取任务数、完成数、失败数、本次抓到公告数、生命周期已审计跳变数，以及高疑似待抓数量。未完成的窗口会保留在待抓清单里，下次运行继续重试。

高疑似待抓、低疑似观察、人工确认和状态文件在：

```text
data/local_reference/etf_lifecycle_announcement_requests.csv
data/local_reference/etf_lifecycle_observation_jumps.csv
data/local_reference/etf_lifecycle_pending_confirmations.csv
data/local_reference/etf_lifecycle_manual_confirmations.csv
data/local_reference/etf_lifecycle_flow_adjustments.csv
data/local_reference/etf_lifecycle_status.json
```

公告下载主源和审计参数可在 `config.txt` 中修改：

```text
announcement_source = cninfo
announcement_api_name = anns_d
announcement_sleep_seconds = 0.20
lifecycle_announcement_window_days = 5
lifecycle_no_announcement_retry_window_days = 10
lifecycle_min_share_change_pct = 0.50
lifecycle_high_suspicion_min_listing_days = 60
lifecycle_integer_ratio_tolerance = 0.03
lifecycle_high_suspicion_positive_min_pct = 2.00
lifecycle_high_suspicion_negative_max_pct = -0.50
```

`announcement_source = cninfo` 表示优先使用巨潮资讯统一抓取基金公告；`exchange` 是上交所/深交所官网备用源；`tushare` 仅作为手动备用源。
`announcement_sleep_seconds = 0.20` 表示公告请求之间默认间隔 0.2 秒；如果网络不稳，可以调大。
`lifecycle_announcement_window_days = 5` 表示对未匹配份额跳变的交易日前后各抓 5 个官方交易日公告。
`lifecycle_no_announcement_retry_window_days = 10` 表示某个窗口完全没抓到公告时，下一轮扩大到前后各 10 个官方交易日再抓一次。
`lifecycle_min_share_change_pct` 是份额跳变筛选阈值，`0.50` 表示相邻两期份额变化达到 50% 或以上才列入核对报告。

自动抓取公告只处理高疑似跳变：排除货币 ETF、排除上市未满 60 天的跳变，保留正向整数倍跳变、正向变化达到 200% 及以上的跳变、或负向变化达到 -50% 及以下的跳变。其他跳变会写入 `etf_lifecycle_observation_jumps.csv`，只做本地观察，不自动抓公告。

公告解析只关心会影响资金流估算的生命周期事件，例如份额拆分、份额折算、转型、合并、清盘。季度报告、流动性服务商公告、净值低于 5000 万提示、溢价风险提示等不会进入生命周期事件表；如果这些公告出现在跳变窗口内，程序会自动把该跳变确认成“无资金流生命周期事件”。

如果某个跳变窗口内完全没有抓到公告，BAT 不再要求你手工确认。下一轮会自动扩大到 `lifecycle_no_announcement_retry_window_days` 设置的交易日窗口重新抓取；如果扩大窗口后仍然没有公告，程序会自动确认该跳变无生命周期事件。

“可能触发基金合同终止情形”只作为清盘线索，不会直接写入生命周期表。程序会从该提示公告日期向后探测到最新日期，只搜索 `终止上市`、`进入清算`、`清算报告`、`基金合同终止并清算` 这些严格关键词；命中后才写入生命周期事件和资金流修正表。

这些本地维护 CSV 都按 UTF-8 BOM 写出，适合用 Excel 打开。若 Excel 把日期保存成 `2026/1/5`、`20260105.0` 或 `="20260105"`，工具会尽量自动识别；`etf_category_map.csv` 的中文分类和代码文本也按同样规则读取。

如果公告接口不可用，也可以把 ETF 公告导出的 CSV 填入公告原始表，至少包含 `fund_code`、`announcement_date`、`title`；如能确认实际生效日，再填 `event_date`。然后手动生成生命周期表和本地份额跳变核对报告：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\build_etf_lifecycle_table.py --config config.txt
```

结果会写入：

```text
outputs/lifecycle_audit/
```

审计工具会按 `data/local_reference/etf_category_map.csv` 中的 ETF universe 过滤本地份额横截面，避免把场外基金代码混入核对报告。

已匹配的拆分、折算、转型、合并、清盘等非资金流份额跳变会写入 `etf_lifecycle_flow_adjustments.csv`。日常生成页面时会读取这张修正表，把对应 ETF 当日的 `estimated_net_flow` 归零，并在 `etf_flow_snapshot.csv` 中保留原始值和修正字段，方便追溯。

## 资金流口径

当前 `estimated_net_flow` 是估算口径：

```text
estimated_net_flow = share_change * 10000 * flow_price
```

其中：

- `share_change` 来自 Tushare `fund_share.fd_share`，当前按“万份”处理。
- 普通 ETF 和债券 ETF 使用场内收盘价作为 `flow_price`。
- 100 元附近报价的货币 ETF 使用 `close / 100`，避免把每份约 1 元的货币基金放大。
- 页面中统一换算为亿元展示。
- Tushare `fund_daily.vol` 抽样核对为“手”口径；当前页面仍以成交额和估算净额为主，不展示成交量、规模或份额视图。

抽样核对后，`fund_share.fd_share` 按 ETF 总份额的万份口径使用。这个指标适合做资金流监控和研究观察。份额折算、基金转型、清盘等生命周期事件仍需通过本地生命周期修正表处理。

如果本地生命周期修正表中已有对应记录，页面生成时会对这些非资金流份额跳变做归零修正。

## 命令行用法

直接生成最近可用市场日：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.txt
```

生成指定日期：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.txt --trade-date 20260625
```

只测试流程，不拉远端数据：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 -u -m etf_flow_monitor.cli --config config.txt --dry-run
```

单独抽样检查 Tushare 数据源：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -X utf8 tools\check_tushare_source_health.py --config config.txt --trade-date 20260625 --sample-size 2
```

## 常见问题

`发布失败，但本地页面生成成功`

通常是网络、GitHub SSH 或代理问题。可以先打开本地 `etf_flow_dashboard.html` 使用，之后再手动运行发布命令。

`提示缺少行情或份额交易日`

说明当前请求日期需要的统计窗口超出了本地完整缓存区间。先运行 BAT 让它补缓存，或检查 `local_cache_start_date` 是否设置得太晚。

`页面没有某只 ETF`

可能是该 ETF 当日缺行情、缺份额、新上市、停牌或数据源暂未返回。可以查看 `etf_flow_snapshot.csv` 和运行日志确认。

`分类看起来不准确`

编辑 `data/local_reference/etf_category_map.csv`，修改 `category`、`subcategory` 或 `review_note` 后重新生成页面。

## 项目边界

本项目只负责 ETF 资金流监控、报告和静态页面发布。不包含策略执行、组合构建、自动交易、调仓或任何会改变账户状态的功能。
