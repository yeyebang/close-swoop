# A股尾盘涨停扫描器 - 开发文档

> 版本: v4.0 规划中
> 最后更新: 2026-05-14

---

## 一、项目概述

### 1.1 目标

每日 14:00 扫描全 A 股，筛选出尾盘拉升、次日开盘有望高开的股票，输出候选名单供交易参考。

### 1.2 策略核心逻辑

```
14:00/尾盘扫描（使用近期日K特征 + 实时行情）
  → 计算 21 个特征（涨幅、量比、均线偏离、历史股性等）
  → 规则评分 + ML 模型预测次日高开概率
  → 二次增强（同花顺/腾讯分笔验证）
  → 输出 Top N 候选股（含代码、名称、买入价、计划卖出）
次日 9:30 开盘验证（次日开盘价相对扫描观察价/代理价的收益率）
```

### 1.3 变更记录

#### v4.0（2026-05-14，开发计划）

v4.0 的目标是把项目从“单次扫描榜单”升级为“尾盘策略闭环工作台”：

```
风控过滤
  → 14:00 扫描大盘建池
  → 14:05 / 14:10 / 14:15 / 14:20 跟踪候选池
  → 14:20 生成最终买入参考
  → 次日早盘验证开盘/30分钟冲高收益
  → 成功/失败样本沉淀，反向优化下一次扫描
```

核心原则：

- “扫描大盘”只负责发现新候选池。
- “跟踪扫描”只跟踪当前候选池，不重新扫描全市场。
- “次日验证”只验证前一交易日最终候选，不产生新股票。
- “虚拟盘”合并进候选股生命周期，作为验证状态和收益结果。
- 前端展示字段全部中文化，底层可继续使用英文计算字段。

##### v4.0 数据模型

| 数据集 | 作用 | 关键字段 |
|--------|------|----------|
| `scan_batches` | 每日 14:00 大盘扫描批次 | 批次编号、交易日期、扫描时间、市场环境、扫描股票数、剔除股票数、入池候选数、最终候选数、批次状态 |
| `candidates` | 批次候选股池 | 股票代码、股票名称、当前价、当日涨幅、换手率、成交额、距涨停幅度、五日涨幅、十日涨幅、风控状态、剔除原因、入选原因、初筛评分、最终评分、当前状态 |
| `tracking_snapshots` | 14:00-14:20 多次跟踪快照 | 快照时间、当前价、当前涨幅、成交量、成交额、换手率、距涨停幅度、跟踪期涨幅、跟踪期成交额、趋势状态 |
| `verifications` | 次日早盘验证 | 信号日期、验证日期、买入参考价、次日开盘价、次日开盘收益、次日30分钟最高价、次日30分钟收益、次日最高收益、次日收盘收益、是否达标、失败原因 |
| `model_feedback` | 自优化分析结果 | 样本数量、成功率、主要成功特征、主要失败特征、建议提高权重、建议降低权重 |

##### v4.0 风控过滤层

扫描大盘前先执行风险过滤。硬剔除规则默认包括：

- ST / *ST / 退市整理股 / 风险警示股。
- 当日停牌、一字涨停、一字跌停、无成交量或明显异常交易。
- 上市天数不足 N 日，默认 60 日。
- 股价低于 3 元。
- 当日成交额低于 1 亿元。
- 连续涨停大于等于 3 天。
- 近 10 日涨幅超过 50%。
- 明显低流动性股票。

降权提醒规则默认包括：

- 近 5 日涨幅超过 30%。
- 上影线过长。
- 当日振幅过大。
- 距涨停过近但成交不足。
- 大盘环境较弱。
- 行业弱于市场。

每只股票需要保留“风控状态”和“剔除/降权原因”，方便后续验证规则有效性。

##### v4.0 14:00 建池字段

前端中文展示字段：

- 股票代码、股票名称、交易日期、当前价、当日涨幅、成交量、成交额、换手率。
- 昨收、涨停价、跌停价、距涨停幅度。
- 五日涨幅、十日涨幅、当日振幅、上影线比例、价格日内位置。
- 风控状态、剔除原因、入选原因、初筛评分、当前状态。

##### v4.0 14:00-14:20 跟踪字段

跟踪快照使用真实抓取时间，不强行依赖固定分钟 K 线接口。汇总字段包括：

- 14:00 价格、14:05 价格、14:10 价格、14:15 价格、14:20 价格。
- 14:00-当前涨幅、14:00-当前成交额变化、14:00-当前成交量变化。
- 当前距涨停幅度、是否接近涨停、是否突破日内高点、是否回落。
- 跟踪价格斜率、跟踪回撤、量价配合、放量滞涨信号、趋势状态。

##### v4.0 14:20 决策规则

最终评分第一版采用规则组合，后续再由样本反馈自动调权：

```
最终评分 = 初筛评分 × 40% + 跟踪评分 × 60% - 风险扣分
```

跟踪评分重点关注：

- 14:00-14:20 涨幅。
- 14:00-14:20 成交额和成交量增长。
- 价格斜率是否向上。
- 量价是否配合。
- 是否站上日内 VWAP 或接近成本线上方。
- 是否突破日内前高。
- 距涨停幅度是否合理。
- 是否没有明显冲高回落。

候选状态包括：初筛、跟踪中、增强、观察、减弱、放量滞涨、冲高回落、接近涨停、淘汰、最终候选、已验证。

##### v4.0 次日验证与标签

次日验证不扫描新股票，只验证前一交易日最终候选。

达标第一版定义：

- 次日开盘收益 >= 1%；或
- 次日 09:30-10:00 最高收益 >= 2%。

失败原因分类：

- 低开。
- 平开无冲高。
- 高开低走。
- 尾盘诱多。
- 放量不涨。
- 大盘拖累。
- 数据不足。

##### v4.0 前端改版

页面结构调整：

| 页面 | 变化 |
|------|------|
| 仪表盘 | 增加“扫描大盘”“跟踪扫描”“次日验证”三个主动作，展示今日批次状态、候选池、最终候选、待验证数 |
| 候选股 | 合并虚拟盘视图，展示今日候选、跟踪快照、最终候选、验证结果、剔除/淘汰原因 |
| 回测优化 | 展示成功/失败样本对比、风控规则效果、指标分布、权重优化建议 |
| 模型 | 展示当前模型状态、样本数量、最近成功率、当前规则权重、自优化建议 |

##### v4.0 开发里程碑

1. 新增 4.0 数据结构与文件存储，兼容旧结果文件。
2. 重构扫描大盘：创建批次、执行风控过滤、生成 14:00 候选池。
3. 新增跟踪扫描：只跟踪当前批次候选股，生成快照和最终评分。
4. 新增次日验证：验证最终候选的开盘收益和 30 分钟冲高收益。
5. 候选股与虚拟盘合并：以候选股生命周期为中心展示交易验证。
6. 升级回测优化：分析成功/失败样本，输出规则权重建议。
7. 前端中文化：所有表格、状态、原因、指标均使用中文展示。

##### v4.0 CLI 命令

| 命令 | 作用 |
|------|------|
| `python run.py v4-scan` | 扫描大盘，创建一个新的 4.0 候选池批次 |
| `python run.py v4-track` | 跟踪最新 4.0 批次候选股，不重新扫描全市场 |
| `python run.py v4-verify` | 验证最新可验证批次的次日早盘收益 |
| `python run.py v4-state` | 查看当前 4.0 批次状态和 Top 候选 |
| `python run.py v4-auto` | 启动 4.0 自动定时流程 |

默认自动流程由 `config.json` 的 `v4.schedule` 控制：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `scan_time` | `14:00` | 扫描大盘建池时间 |
| `track_times` | `["14:05", "14:10", "14:15", "14:20"]` | 跟踪扫描时间点 |
| `verify_time` | `10:00` | 次日验证时间 |
| `days` | `mon-fri` | 定时运行星期 |
| `verification_minute_period` | `1` | 次日早盘验证分钟 K 周期 |
| `verification_end_time` | `10:00` | 次日早盘冲高验证截止时间 |

##### v4.0 运行文件

4.0 运行数据保存在 `stock_screener/reports/v4/`，属于运行产物，默认不提交到 Git。

| 文件 | 内容 |
|------|------|
| `scan_batches.csv` | 扫描批次 |
| `candidates.csv` | 入池候选股 |
| `excluded.csv` | 风控剔除股票及剔除原因 |
| `tracking_snapshots.csv` | 跟踪扫描快照 |
| `verifications.csv` | 次日验证结果 |
| `model_feedback.json` | 成功/失败样本反馈和权重建议 |

跟踪扫描后，候选股会写入 `decision_reason`，前端显示为“决策解释”，用于说明进入最终候选、淘汰或降权的原因。

次日验证会优先读取真实分钟 K，计算 09:30 到 `verification_end_time` 的最高价和收益；若分钟 K 暂不可用，则回退到“日K最高价代理”，并在 `next_30min_source` / 前端“数据源”中明确标记，避免把代理值误认为真实 30 分钟收益。

#### v3.1（2026-05-13）

| 模块 | 修改 | 目的 |
|------|------|------|
| `data_fetcher.py` | 实时行情主源从东方财富改为**腾讯直连 HTTP** (`qt.gtimg.cn`) | 东方财富频繁超时，腾讯直连热缓存 ~0.7s 完成全市场 5200+ 只 |
| `data_fetcher.py` | 并发 5 线程批量拉取，每批 100 只 | 首次含代码列表约 6s，之后代码列表缓存 24h |
| `data_fetcher.py` | 新浪降为托底备用；旧缓存应急保留 | 腾讯失败时仍能完成扫描（新浪缺量比/换手率会填 0） |
| `paper.py` | `settle_pending` 新增 `fallback_data_dirs` 参数 | 结算逻辑可依次查找多个历史文件目录 |
| `main.py` | 传入 `legacy_data_dir` 作为结算 fallback | 修复 settlement 读不到 history 文件（文件在 legacy 目录） |
| `ui_server.py` | 新增 `_enrich_with_paper_returns()`，`scope=all/top` 时自动 join paper ledger | 候选股列表现在会展示已结算的次日开盘收益 |
| `web/app.js` | 候选股表格列名修正：`next_day_open_return` → `next_return_pct` | 原列名从未存在，导致次日收益永远显示 `--` |
| `web/index.html` | 版本号 v2 → v3 | — |

#### v3.0（2026-05-12）

| 模块 | 修改 | 目的 |
|------|------|------|
| `main.py` | 实时行情和历史行情增加超时、备用源、旧缓存兜底 | 避免东方财富接口断开导致扫描中断 |
| `main.py` | 东方财富历史源每只股票默认只试 1 次，连续失败后本轮直接切新浪 | 避免每只股票三次红色失败刷屏 |
| `config.json` | 新增 `history_primary_max_attempts`、`history_primary_failure_threshold` | 可配置历史主源重试和熔断阈值 |
| `main.py` | 扫描结果新增 `信号时间`、`买入价`、`计划卖出=次日开盘` | 让结果文件表达真实交易计划 |
| `paper.py` | 虚拟盘改为按信号日后第一个交易日开盘价结算 | 对齐”今日下午买、次日早盘卖”策略 |
| `ui_server.py` | 新增 `/api/backtest/run`、`/api/backtest/status`、`/api/backtest/report` | Web 端可运行和读取回测报告 |
| `web/app.js` | 候选股页改读最新扫描全量结果，回测页按模式读取报告 | 避免候选股和回测数据混杂 |
| `web/index.html` | “分钟级回测”改名为”隔夜代理回测” | 避免把日线代理误称为真实分钟回测 |

#### 历史数据失败日志说明

扫描日志中出现”东方财富历史源断开，改用新浪备用源”时，不代表这只股票无数据，也不代表扫描失败。它表示主接口连接被对方关闭，程序会继续用新浪历史源获取数据。

只有出现”历史数据全部失败，无法参与本轮评分”时，才表示该股票在本轮没有可用历史数据，会被跳过。

### 1.4 V3 自修正闭环

V3 的目标是把每天真实扫描出来的信号变成后续评分模型的训练数据，而不是只依赖静态规则或历史代理回测。

```
第 N 天下午扫描
  → 记录候选股、扫描价格、当时特征、规则分、最终排名
第 N+1 天早上/下次扫描前复盘
  → 用次日开盘价结算
  → success=1 作为正样本
  → success=0 作为负样本
  → 对比成功/失败共性
  → 生成 V3 复盘修正模型
第 N+1 天中午/下午扫描
  → 基础规则分
  → V3 全量复盘模型分
  → V3 近期复盘模型分
  → 失败共性风险惩罚
  → 输出 final_score
```

V3 输出字段:

| 字段 | 含义 |
|------|------|
| `v3_model_score` | 全量模型与近期模型混合后的成功概率分 |
| `v3_all_score` | 全量复盘样本模型分 |
| `v3_recent_score` | 近期窗口样本模型分 |
| `v3_adjustment` | V3 对基础规则分的净修正 |
| `v3_risk_penalty` | 命中失败共性规则后的扣分 |
| `v3_notes` | 命中的失败共性说明 |

模型诊断报告保存到 `stock_screener/models/paper_v3_review_model.json`，包含样本量、成功率、近期成功率、成功/失败特征均值差异、风险规则列表。

### 1.5 技术栈

- **Python 3.9+**
- **AKShare** — A 股数据获取（行情、分钟K线、板块、股票名称）
- **pandas / numpy** — 数据处理
- **LightGBM / sklearn** — ML 模型
- **loguru** — 日志
- **APScheduler** — 定时任务

---

## 二、项目结构

```
stock-screener/
├── stock_screener/
│   ├── __init__.py
│   ├── main.py                 # CLI 入口，所有命令路由
│   ├── data_fetcher.py         # 统一数据获取层（AKShare + 缓存 + 股票名称管理）
│   ├── features.py             # 旧版特征工程（日K为主，保留对比）
│   ├── minute_backtest.py      # ★ 核心：回测引擎 + ML 训练 + 标签定义
│   ├── enrichment.py           # 二次增强（同花顺/腾讯分笔验证）
│   ├── paper.py                # 虚拟盘台账 + 自适应评分
│   ├── research.py             # 旧版日线回测 + 训练（保留对比）
│   ├── ollama_advisor.py       # 本地 Ollama AI 分析
│   ├── ui_server.py            # Web 工作台（后端 API）
│   ├── web/                    # Web 前端（Dashboard 暗色主题）
│   │   ├── index.html          # Dashboard 页面
│   │   ├── styles.css          # 暗色主题样式
│   │   └── app.js              # 前端交互逻辑 + Canvas 图表
│   ├── data/                   # 历史数据缓存
│   │   ├── history_*.csv       # 日K数据
│   │   ├── minute_*.csv        # 分钟K数据
│   │   └── stock_names.json    # 股票名称缓存（代码 → 名称映射）
│   ├── models/                 # 训练好的 ML 模型
│   ├── reports/                # 回测报告
│   │   ├── minute_backtest_picks.csv
│   │   ├── minute_backtest_metrics.json
│   │   ├── backtest_picks.csv
│   │   └── backtest_metrics.json
│   ├── paper/                  # 虚拟盘台账
│   └── logs/                   # 运行日志
├── config.json                 # 配置文件
├── requirements.txt            # 依赖列表
├── run.py                      # 启动脚本
├── README.md                   # 使用说明
└── DEVELOPMENT.md              # 开发文档
```

---

## 三、核心模块设计

### 3.1 数据获取层 (`data_fetcher.py`)

#### DataFetcher 类

```
DataFetcher
├── get_all_realtime()           # 全市场实时行情（腾讯直连优先，新浪备用）
│   ├── _fetch_tencent_realtime()    # 腾讯批量拉取（并发5线程，100只/批）
│   ├── _fetch_tencent_batch()       # 单批次请求 qt.gtimg.cn
│   └── _get_all_codes_tx()          # 全量代码列表（24h 缓存）
├── get_history()                # 日K线（前复权）
├── get_minute_history()         # 分钟K线（批量，N天）
├── get_minute_history_by_date() # 分钟K线（指定日期）
├── get_limit_up_pool()          # 涨停股池
├── get_all_concept_sectors()    # 概念板块数据
├── get_market_sentiment()       # 大盘情绪指标
├── get_stock_name(code)         # 获取股票名称（从缓存）
├── set_stock_name(code, name)   # 设置股票名称（写入缓存）
└── batch_resolve_names(codes)   # 批量解析股票名称（AKShare + 持久化）
```

#### 股票名称管理

- 缓存文件: `data/stock_names.json`（`{"000001": "平安银行", ...}`）
- 扫描时自动从实时行情中提取名称并缓存
- 回测/训练时从缓存读取名称
- 批量解析: 调用 `ak.stock_info_a_code_name()` 一次性获取全市场名称

#### CacheManager 类

- 统一管理数据缓存，带过期时间
- 缓存路径格式: `cache_{key}.csv`
- 历史数据缓存: `history_{code}.csv` / `minute_{code}_{period}_{date}.csv`

#### 数据源降级策略

```
实时行情（v3.1 起）:
腾讯直连 HTTP (qt.gtimg.cn) — 并发 5 线程，批量 100 只/次
  → 失败后切换新浪 AKShare (stock_zh_a_spot)  ← 缺量比/换手率，填 0
  → 全部失败使用最近实时缓存（应急参考）

腾讯字段覆盖：last_price / pre_close / open_price / volume /
              change_amt / change_pct / high / low / amount /
              turnover_rate / amplitude / volume_ratio（全覆盖）

股票代码列表:
AKShare stock_info_a_code_name() — 缓存 24h 到 cache_all_codes.json

历史日K:
东方财富 (stock_zh_a_hist)
  → 默认每只股票尝试 1 次
  → 失败后切换新浪 (stock_zh_a_daily)
  → 东方财富连续失败达到阈值后，本轮扫描直接使用新浪
  → 全部失败但有旧缓存时使用旧缓存
  → 全部失败且无缓存时，该股票跳过本轮评分
```

#### 关键配置

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `realtime_cache_ttl_seconds` | 实时行情缓存有效期 | 120 秒 |
| `max_history_cache_age_days` | 历史日K缓存新鲜度阈值，配置缺省时使用代码默认值 | 7 天 |
| `history_primary_max_attempts` | 东方财富历史源每只股票最大尝试次数 | 1 |
| `history_primary_failure_threshold` | 东方财富历史源连续失败多少只后本轮暂停 | 5 |

### 3.2 回测引擎 (`minute_backtest.py`)

#### 标签定义

| 字段 | 计算方法 | 含义 |
|-----|---------|------|
| `next_day_open_return` | `(次日开盘价 / 当日收盘价 - 1) × 100` | 次日开盘收益率 |
| `is_strong_open` | `next_day_open_return > 0.5` | 次日是否高开超0.5% |
| `is_limit_up` | `当日涨跌幅 >= 9.8` | 当日是否涨停 |

#### 特征体系（21个）

| 类别 | 特征名 | 计算方法 | 逻辑意义 |
|-----|--------|---------|---------|
| **涨幅** | `change_pct` | 当日涨跌幅 | 当前涨幅 |
| **量比** | `vol_vs_ma5` | 当日成交量 / 近5日均量 | 放量确认 |
| **换手率** | `turnover_rate_daily` | 当日换手率 | 活跃度 |
| **均线偏离** | `ma5_dev` | 收盘/MA5-1 | 短线偏离 |
| | `ma10_dev` | 收盘/MA10-1 | 中线偏离 |
| **趋势** | `return_5d` | 近5日涨幅 | 短期趋势 |
| | `return_10d` | 近10日涨幅 | 中期趋势 |
| **形态** | `ma_bull_aligned` | MA5>MA10>MA20 | 多头排列 |
| | `price_position_10d` | 价格在10日高低点位置 | 价格位置 |
| | `volatility_10d` | 近10日收益标准差 | 波动率 |
| **历史股性** | `hist_limit_up_rate_20d` | 近20日涨停次数占比 | 涨停频率 |
| | `recent_high_touch_count` | 近5日涨幅>8.5%次数 | 强势频率 |
| | `avg_amplitude_20d` | 近20日平均振幅 | 波动特性 |

#### 评分规则

```
涨幅 3.5-7%:     25分
涨幅 2.0-3.5%:   18分
涨幅 7.0-9.0%:   20分
涨幅 0.5-2.0%:   12分
涨幅 -2.0-0.5%:  5分

量比 >= 2.5:     20分
量比 >= 2.0:     17分
量比 >= 1.5:     13分
量比 >= 1.2:     8分
量比 >= 1.0:     4分

5日涨幅 3-12%:   10分
5日涨幅 0-3%:    8分
5日涨幅 -3-0%:   5分

换手率 5-15%:    15分
换手率 3-5%:     12分
换手率 15-25%:   10分

均线多头排列:    5分
```

#### ML 模型训练

```python
LGBMClassifier(
    objective="binary",
    n_estimators=300,
    learning_rate=0.03,
    num_leaves=31,
    subsample=0.85,
    colsample_bytree=0.85,
    scale_pos_weight=neg/pos,  # 处理样本不平衡
    random_state=42,
)
```

- 标签: `is_strong_open`（次日高开超0.5%）
- 时间序列分割: 80/20 按日期
- 缺 LightGBM 自动退回 sklearn HistGradientBoosting

### 3.3 二次增强 (`enrichment.py`)

对 Top 50 候选进行同花顺/腾讯分笔验证：

| 信号源 | 加分 | 逻辑 |
|-------|------|------|
| 同花顺量价齐升 | +6 | 资金流入 |
| 同花顺热门概念 | +5 | 题材热度 |
| 同花顺突破5日线 | +4 | 技术突破 |
| 同花顺连续上涨 | +3 | 趋势延续 |
| 腾讯买盘占优 | +6 | 资金方向 |
| 腾讯尾盘成交放大 | +4 | 尾盘异动 |

### 3.4 虚拟盘系统 (`paper.py`)

```
扫描结果 → append_signals() → 写入台账
下次扫描 → settle_pending()  → 结算收益
样本>30  → build_v3_review_model() → V3复盘修正模型
扫描时   → apply_adaptive_scores() → V3修正分 + 风险惩罚
```

虚拟盘信号语义:
- `scan_price`: 扫描当时观察价/计划买入价
- `exit_plan`: 当前固定为 `next_open`
- `planned_exit_date`: 实际结算后写入次日交易日期
- `next_exit_price`: 信号日之后第一个交易日开盘价
- `next_return_pct`: `next_exit_price / scan_price - 1`

注意: 虚拟盘不再用“下一次扫描时的实时价”作为卖出价。未取得次日开盘日K时，记录保持 `open`，等待下一次扫描复盘。

V3 复盘修正使用 LogisticRegression 学习真实扫描信号的成功/失败模式，需要至少 30 条已复盘样本才启用。模型会同时看全量样本和近期样本，默认近期窗口 60 天、近期权重 0.6。

### 3.5 Web 界面 (`web/` + `ui_server.py`)

#### 前端架构

- **index.html** — Dashboard 页面结构（5个Tab：仪表盘、候选股、回测、虚拟盘、模型）
- **styles.css** — 暗色主题，CSS 变量，响应式布局
- **app.js** — 前端交互（Tab 切换、懒加载、Canvas 图表渲染）

#### 后端 API

```
ui_server.py
├── /api/summary          → 全局汇总数据（指标、候选、配置）
├── /api/results          → 结果数据（scope=top/all/latest-candidates/paper/backtest）
├── /api/scan/run         → 触发一次扫描（POST）
├── /api/scan/status      → 扫描状态（running/idle/completed/failed）
├── /api/backtest/run     → 触发回测（POST，mode=daily/minute）
├── /api/backtest/status  → 回测状态
├── /api/backtest/report  → 回测报告（mode=daily/minute）
├── /api/history          → 个股历史K线
├── /api/minute-backtest  → 分钟级回测指标 + 明细
├── /api/ollama/models    → Ollama 可用模型列表
└── /api/ollama/analyze   → Ollama AI 分析候选股（POST）
```

#### 扫描进程管理

- 使用 `sys.executable` 执行扫描脚本（修复 macOS `python` 命令问题）
- 后台进程管理，状态轮询
- 状态流转: `idle → running → completed/failed`

---

## 四、数据流

```
┌─────────────────────────────────────────────────────────────┐
│                        扫描流程                              │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  14:00 触发                                                   │
│      ↓                                                       │
│  获取全市场实时行情 (含股票名称)                               │
│      ↓                                                       │
│  名称缓存 → data/stock_names.json                              │
│      ↓                                                       │
│  基本过滤: 排除ST/科创板/创业板/价格区间/涨跌停                │
│      ↓                                                       │
│  预排序: 用实时字段轻量筛选前500只                             │
│      ↓                                                       │
│  逐股处理:                                                    │
│      ├─ 获取日K线历史                                         │
│      ├─ 计算特征（均线偏离、量比、趋势、历史股性）              │
│      ├─ 规则评分                                               │
│      └─ ML预测（如有模型）                                     │
│      ↓                                                       │
│  排序: 优先按 ml_score / score 降序                           │
│      ↓                                                       │
│  二次增强: Top 50 同花顺/腾讯验证                              │
│      ↓                                                       │
│  输出: Top N 候选股（代码 + 名称 + 评分）                      │
│      ├─ 终端打印                                              │
│      ├─ CSV 保存                                              │
│      └─ 写入虚拟盘台账                                        │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 五、回测系统

### 5.1 隔夜代理回测（当前推荐）

```bash
python run.py minute-backtest [top_n]
```

**核心逻辑**:
1. 从缓存日K数据构建数据集
2. 对每个日期，用当日收盘数据作为尾盘观察价代理
3. 标签是次日开盘收益率（`next_day_open_return`）
4. 按规则评分选出 Top N
5. 统计命中率、收益等指标

重要说明:
- 当前项目本地没有真实 14:00 分钟快照文件，因此该模式不是严格的分钟级回测。
- Web UI 已将按钮显示为“隔夜代理回测”，避免把日线代理误解成真实分钟回测。
- 后续如果接入 14:00/14:30 快照或分钟K缓存，需要把买入价从“收盘代理价”替换为真实扫描时点价格。

**输出指标**:
- `strong_open_rate_pct`: 次日高开率(>0.5%)
- `win_rate_pct`: 次日开盘胜率
- `avg_next_open_return_pct`: 平均次日开盘收益
- `total_return_pct`: 累计收益
- `max_drawdown_pct`: 最大回撤

### 5.2 日线回测（旧版，仅供参考）

```bash
python run.py backtest [top_n]
```

> 旧版使用"次日收盘涨停"作为标签，与真实交易逻辑不符，仅保留用于对比。

### 5.3 回测对比

| 版本 | 标签定义 | 交易逻辑 | 可信度 |
|-----|---------|---------|--------|
| 旧版 | 次日收盘是否涨停 | 不匹配 | ❌ 参考用 |
| 隔夜代理 | 次日开盘收益率 | 尾盘代理价→次日开盘卖 | ✅ 当前推荐 |
| 真实分钟回测 | 真实14:00/14:30价格→次日开盘收益 | 14:00/14:30买→次日开盘卖 | 待接入分钟快照 |

---

## 六、模型训练

```bash
# 分钟级模型（推荐）
python run.py minute-train

# 日线模型（旧版）
python run.py train
```

### 模型文件

```
models/
├── minute_limit_up_lgbm.txt        # LightGBM 模型
├── minute_limit_up_sklearn.pkl     # sklearn 备选模型
├── minute_features.json            # 特征列表
├── minute_model_metrics.json       # 训练指标
├── limit_up_lgbm.txt               # 旧版日线模型
├── limit_up_sklearn.pkl            # 旧版 sklearn 模型
├── features.json                   # 旧版特征列表
└── metrics.json                    # 旧版训练指标
```

### 模型评估指标

| 指标 | 含义 | 参考阈值 |
|-----|------|---------|
| `valid_auc` | 排序能力 | > 0.6 可用 |
| `valid_average_precision` | 正样本识别能力 | > 0.1 可用 |
| `positive_rate_pct` | 正样本比例 | 通常 30-60% |

---

## 七、配置说明

### 7.1 config.json

```json
{
    "scan_hour": 14,
    "scan_minute": 0,
    "backtrack_days": 60,
    "max_history_fetch": 500,
    "realtime_cache_ttl_seconds": 120,
    "history_primary_max_attempts": 1,
    "history_primary_failure_threshold": 5,
    "top_n": 20,
    "enrichment": {
        "enabled": true,
        "candidate_count": 50,
        "ths_enabled": true,
        "tencent_tick_enabled": true,
        "tencent_tick_count": 20,
        "max_bonus": 20
    },
    "paper": {
        "enabled": true,
        "success_return_pct": 1.0,
        "adaptive_min_samples": 30,
        "v3_recent_days": 60,
        "v3_recent_weight": 0.6,
        "v3_model_weight": 0.35
    },
    "filters": {
        "min_volume": 10000,
        "max_price": 50,
        "min_price": 3,
        "exclude_st": true
    }
}
```

### 7.2 参数说明

| 参数 | 说明 | 默认值 |
|-----|------|--------|
| `scan_hour/minute` | 扫描时间 | 14:00 |
| `top_n` | 输出候选数量 | 20 |
| `backtrack_days` | 历史数据天数 | 60 |
| `max_history_fetch` | 最大处理股票数 | 500 |
| `realtime_cache_ttl_seconds` | 行情缓存有效期 | 120s |
| `history_primary_max_attempts` | 东方财富历史源单股尝试次数 | 1 |
| `history_primary_failure_threshold` | 东方财富历史源连续失败熔断阈值 | 5 |
| `enrichment.candidate_count` | 二次增强候选数 | 50 |
| `paper.adaptive_min_samples` | 自适应评分最小样本 | 30 |
| `paper.v3_recent_days` | V3 近期复盘窗口 | 60 |
| `paper.v3_recent_weight` | V3 近期模型权重 | 0.6 |
| `paper.v3_model_weight` | V3 修正模型在最终分中的权重 | 0.35 |
| `filters.min_price/max_price` | 价格区间 | 3-50元 |

---

## 八、CLI 命令参考

```bash
# 实时扫描
python run.py scan

# 定时扫描
python run.py --auto

# 隔夜代理回测
python run.py minute-backtest [top_n]

# 分钟级/代理模型训练
python run.py minute-train

# 分钟级/代理候选排序
python run.py minute-candidates [top_n]

# 日线回测（旧版）
python run.py backtest [top_n]

# 日线模型训练（旧版）
python run.py train

# 日线候选（旧版）
python run.py candidates [top_n]

# 虚拟盘报告
python run.py paper-report

# Ollama 分析
python run.py explain [model_name]

# Web 工作台
python run.py ui [host] [port]
```

---

## 九、Web 界面

访问 `http://127.0.0.1:3002`

### 页面布局

```
┌──────────┬─────────────────────────────────────┐
│ 侧边导航   │ 顶部栏（标题 + 开始扫描按钮）         │
│          ├─────────────────────────────────────┤
│ 仪表盘    │ 统计卡片 + 收益曲线 + 候选分布       │
│          │ + 最新候选表                         │
│ 候选股    │ 最新扫描全量候选明细表（含名称/评分）   │
│ 回测     │ 回测指标 + 每日回测明细              │
│ 虚拟盘    │ 扫描信号跟踪（观察价/退出价/收益率）  │
│ 模型     │ 模型状态 + 特征重要性图表            │
└──────────┴─────────────────────────────────────┘
```

### UI 技术特性

- **暗色主题** — 低对比度设计，长时间使用不疲劳
- **响应式布局** — 适配桌面和移动端
- **Canvas 图表** — 零依赖轻量级图表渲染
- **懒加载** — Tab 切换时才加载数据
- **实时状态** — 扫描状态动画显示（运行/完成/失败）
- **真实分布** — 仪表盘评分分布来自最新扫描结果，不再使用固定演示数据
- **移动端一致** — 底部导航和侧边导航都会触发对应 Tab 数据加载

---


### 扫描进度弹窗

点击"开始扫描"按钮后弹出进度弹窗，提供可视化扫描状态反馈：

```
┌────────────────────────────────────┐
│   ● 扫描进度                        × │
├────────────────────────────────────┤
│ 阶段：获取实时行情                    │
│ ████████████████░░░░░░░░░░░░░░░░░░ │
│ ─────────────────────────────────   │
│ 扫描日志：                           │
│   正在获取全市场实时数据...           │
│   获取到 5000 只股票实时行情         │
│   排除ST股后剩余 4800 只...          │
│ ─────────────────────────────────   │
└────────────────────────────────────┘
```

**前端实现** (`web/app.js`):
- `openScanModal()` — 打开弹窗并重置状态
- `closeScanModal()` — 关闭弹窗
- `updateScanModal(status)` — 轮询回调更新阶段、进度条、日志列表
- `runScan()` — 启动扫描时打开弹窗
- `pollScanStatus()` — 轮询状态，完成后自动关闭（成功延迟 1.5s）

**后端状态** (`ui_server.py`):
- `SERVER_STATE` 字典维护 `scan_phase`（8 个阶段映射）
- `append_scan_log()` — 逐行解析扫描日志，自动更新阶段
- `/api/scan/status` — 轮询接口，返回完整状态 + 日志

**阶段映射**:
| 日志关键字 | 阶段显示 |
|-----------|---------|
| "正在获取全市场实时数据" | 获取实时行情 |
| "获取到" | 整理市场数据 |
| "排除ST" | 执行基础过滤 |
| "实时预筛" | 预筛候选池 |
| "已处理" | 计算特征评分 |
| "开始二次增强" | 二次增强验证 |
| "二次增强完成" | 排序保存结果 |
| "扫描完成" / "虚拟盘记录新增" | 扫描完成 |

**视觉特性**:
- 毛玻璃遮罩背景，点击遮罩可关闭
- 进度条扫描中为 indeterminate 动画（流动效果）
- 完成后变为 100% 进度条
- 日志区域等宽字体，成功/失败日志分别着绿/红色
- 扫描结束后成功状态延迟 1.5 秒自动关闭

## 十、已知问题与改进方向

### 10.1 当前限制

1. **分钟数据未完全接入**：回测框架已就绪，但实际分钟K线特征计算依赖逐股拉取
2. **板块数据缺失**：AKShare 板块接口不稳定，板块特征尚未完全实现
3. **盘口数据缺失**：没有 Level-2 逐笔数据，无法计算委比、大单流向
4. **名称缓存需首次扫描后生效**：`stock_names.json` 需运行一次扫描后才生成

### 10.2 改进方向

#### 短期（1-2周）
- [ ] 批量预加载分钟K线数据
- [ ] 完善板块特征计算
- [ ] 加入新闻/公告 NLP 特征
- [ ] 优化名称缓存初始化逻辑

#### 中期（1个月）
- [ ] 接入 Tushare Pro 更稳定数据
- [ ] 模型超参数优化（Optuna）
- [ ] 实时分钟特征计算（盘中）

#### 长期（2-3个月）
- [ ] 接入 Level-2 行情
- [ ] 深度学习模型（LSTM/Transformer）
- [ ] 多因子组合策略

---

## 十一、开发规范

### 11.1 代码风格

- Python 3.9+
- 使用类型提示
- 函数命名: `snake_case`
- 类命名: `PascalCase`
- 常量: `UPPER_CASE`

### 11.2 日志

```python
from loguru import logger

logger.info("信息")
logger.warning("警告")
logger.error("错误")
```

### 11.3 错误处理

- 数据获取失败时降级（缓存 > 空数据）
- 不阻塞主流程
- 记录错误日志

### 11.4 UI Server

- 使用 `sys.executable` 而非硬编码 `python` 执行子进程
- 扫描进程后台运行，通过轮询获取状态
- API 统一返回 JSON 格式

---

## 十二、常见问题

### Q1: 点击"开始扫描"报错 No such file or directory: 'python'

A: macOS 上 Python 命令是 `python3`。已在 `ui_server.py` 中修复，使用 `sys.executable` 代替硬编码的 `"python"`。

### Q2: 为什么回测结果和实盘差异大？

A: 旧版回测使用"次日收盘涨停"标签，与真实交易逻辑（次日开盘卖出）不匹配。新版已修正为"次日开盘收益率"。

### Q3: ML 模型训练后 AUC 很低怎么办？

A: 次日高开预测本身是困难任务，AUC 0.55-0.65 已属正常。关键看回测实际命中率和收益。

### Q4: 如何提高选股准确率？

A:
1. 积累更多历史数据（至少半年）
2. 接入板块/新闻等外部信息
3. 考虑多模型融合

### Q5: 股票名称显示不全怎么办？

A: 首次扫描时会自动从 AKShare 拉取全市场名称并缓存到 `data/stock_names.json`。如果某些股票名称为空，运行一次全市场扫描即可补全。

### Q6: 回测数据中有 41,071 行，但触及涨停率很低

A: 回测使用日K数据代理，标签是"次日开盘收益率"而非"涨停"。这更贴近你的实际交易逻辑。触及率低说明当前规则筛选的股票确实很少能在次日高开，这也验证了策略需要更多维度的特征（分钟线、板块、情绪等）。

### Q7: 扫描日志里经常出现“历史数据失败”是什么意思？

A: 如果日志是“东方财富历史源断开，改用新浪备用源”，这只是主接口断开，程序会继续走新浪备用源，不代表这只股票无数据。只有“历史数据全部失败，无法参与本轮评分”才表示该股票本轮被跳过。

---

## 十三、版本历史

| 版本 | 日期 | 变更 |
|-----|------|------|
| v1.0 | 2026-04 | 初始版本：日线代理回测 + 主观评分 |
| v2.0 | 2026-05 | 分钟级回测引擎 + ML 模型 + 特征体系重构 |
| v2.1 | 2026-05 | 修正标签为次日开盘收益 + 现代化Dashboard UI + 股票名称管理 |
| v2.2 | 2026-05 | 修复 sys.executable 兼容性问题 + 完整Web界面（Canvas图表、懒加载） |
| v2.3 | 2026-05 | 新增扫描进度弹窗（可视化阶段、进度条、实时日志） |
| v2.4 | 2026-05-12 | 数据源熔断与备用源优化 + 虚拟盘次日开盘结算 + UI 结果语义对齐 |
| v3.0 | 2026-05-12 | 真实扫描复盘闭环：成功/失败样本训练 V3 修正模型，输出风险惩罚和失败共性说明 |

---

## 十四、部署状态

### 2026-05-10 部署进度

#### 已完成
- ✅ GitHub 仓库创建：**yeyebang/close-swoop**（公开仓库）
- ✅ 代码已 push 到远程仓库
- ✅ 仓库命名：**close-swoop**（尾盘突袭）

#### 待部署（服务器：116.62.230.151，OpenCloudOS 9，宝塔面板）
- [ ] 克隆代码到服务器：`git clone https://github.com/yeyebang/close-swoop.git`
- [ ] 安装 Python 依赖：`pip install -r requirements.txt`
- [ ] 配置 Qwen API Key（替代 Ollama）：
  - 环境变量 `QWEN_API_KEY`
  - 修改 `ollama_advisor.py` 和 `ui_server.py` 中的 API 调用逻辑
- [ ] 启动 Web UI：`python run.py ui 0.0.0.0 3002`
- [ ] 使用 systemd 或 supervisor 保持进程运行
- [ ] Cloudflare 域名解析：`gupiao.nicetalking.site` → `116.62.230.151`
- [ ] Nginx 反向代理：`gupiao.nicetalking.site` → `localhost:3002`

#### SSH 连接问题
- 服务器 SSH 端口 22 开放且安全组已放行
- 本地 SSH 连接在密钥交换阶段被服务器关闭（kex_exchange_identification: Connection closed）
- 原因待排查：可能是服务器 sshd 配置限制或 IP 被安全软件拦截
- 替代方案：通过网页终端操作部署

---

**文档结束**
