# 美国各 ISO 电力市场日报 · 数据仓库

这个仓库为每日的「美国各ISO电力市场日报」提供数据。报告由 Claude 定时任务每天北京时间约 9 点生成（Word 文档：曲线、表格加简要点评）。

## 工作方式

1. **取数**：GitHub Actions（`.github/workflows/daily.yml`）每天北京时间 06:47 运行 `fetch/fetch_iso.py`，通过开源库 [gridstatus](https://github.com/gridstatus/gridstatus) 下载各 ISO 的公开数据，结果提交到 `data/<日期>/`。08:17 再运行一次，只重试失败的 ISO。
2. **出报告**：Claude 定时任务读取 `data/<日期>/`，运行 `report/analyze.py` 计算统计量并画图，写点评，再用 `report/build_docx.js` 生成 Word。

## 覆盖范围

| ISO | 价格节点 | 辅助服务 |
|---|---|---|
| CAISO | SP15 / NP15 / ZP26 | 日前：上/下调频、旋转、非旋转备用 |
| ERCOT | North / Houston / South / West Hub、Hub Average | 日前 MCPC：Reg-Up/Down、RRS、ECRS、Non-Spin |
| PJM | Western、AEP-Dayton、N. Illinois、Eastern、Dominion Hub | 日前备用：同步、一级、30 分钟 |
| MISO | Indiana、Illinois、Michigan、Minnesota、Louisiana Hub | 日前事前 MCP（各备用区平均） |
| NYISO | Zone J / G / F / A / K | 日前：调频、10 分钟旋转/非同步、30 分钟备用 |
| ISO-NE | Mass Hub、NEMA/Boston、Connecticut、Maine | 实时备用价格（TMSR / TMNSR / TMOR） |
| SPP | North Hub、South Hub | 日前 MCP（各备用区平均） |

报告日期 D 为北京日期：日前市场取运行日 D（以及 D-1、D-2 做对比），实时市场取 D-2（美国当地最近一个完整运行日）。负荷取 D 日最新预测和 D-2 日实际值。

## 可选的 API key（在仓库 Settings → Secrets and variables → Actions 中添加）

- `PJM_API_KEY`：PJM Data Miner 2 的 API key（在 apiportal.pjm.com 免费注册）。不填时脚本会尝试 PJM 的公开 key，失败则跳过 PJM。
- `MISO_API_PRICING_SUBSCRIPTION_KEY`：MISO Data Exchange 定价 API key（可选）。不填时从 MISO 公开市场报告读取辅助服务价格。

## 手动运行

在 Actions 页面选择 **daily-iso-fetch → Run workflow**，可以指定日期（YYYY-MM-DD）和 ISO 子集（如 `PJM,MISO`）。重复运行会合并进当天的数据，不会覆盖其他 ISO。

## 数据格式（`data/<D>/`）

- `prices.csv`：`iso, market(DA/RT), location, day, he, interval_start_utc, value`（$/MWh，实时为小时均值）
- `as_prices.csv`：`iso, market, product, zone, day, he, interval_start_utc, value`（$/MW·h）
- `load.csv`：`iso, kind(forecast/actual), day, he, interval_start_utc, value`（MW）
- `meta.json`：各数据集抓取状态、时区、运行记录；`fetch.log`：抓取日志

`day` 为各 ISO 当地运行日（MISO 按 EST），`he` 为小时结束编号（夏令时切换日为 23 或 25 小时）。
