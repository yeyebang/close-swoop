# A股尾盘涨停扫描器 - 开发文档

> 版本: v2.4
> 最后更新: 2026-05-12

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

### 1.3 2026-05-12 修复记录

本次修复围绕“下午扫描、次日开盘退出”的真实使用链路展开，重点解决数据源失败误报、扫描结果和 UI 语义不一致、虚拟盘结算逻辑不匹配的问题。

| 模块 | 修改 | 目的 |
|------|------|------|
| `main.py` | 实时行情和历史行情增加超时、备用源、旧缓存兜底 | 避免东方财富接口断开导致扫描中断 |
| `main.py` | 东方财富历史源每只股票默认只试 1 次，连续失败后本轮直接切新浪 | 避免每只股票三次红色失败刷屏 |
| `config.json` | 新增 `history_primary_max_attempts`、`history_primary_failure_threshold` | 可配置历史主源重试和熔断阈值 |
| `main.py` | 扫描结果新增 `信号时间`、`买入价`、`计划卖出=次日开盘` | 让结果文件表达真实交易计划 |
| `paper.py` | 虚拟盘改为按信号日后第一个交易日开盘价结算 | 对齐“今日下午买、次日早盘卖”策略 |
| `ui_server.py` | 新增 `/api/backtest/run`、`/api/backtest/status`、`/api/backtest/report` | Web 端可运行和读取回测报告 |
| `web/app.js` | 候选股页改读最新扫描全量结果，回测页按模式读取报告 | 避免候选股和回测数据混杂 |
| `web/app.js` | 仪表盘评分分布改为真实扫描结果统计 | 移除固定演示数据 |
| `web/index.html` | “分钟级回测”改名为“隔夜代理回测” | 避免把日线代理误称为真实分钟回测 |

#### 历史数据失败日志说明

扫描日志中出现“东方财富历史源断开，改用新浪备用源”时，不代表这只股票无数据，也不代表扫描失败。它表示主接口连接被对方关闭，程序会继续用新浪历史源获取数据。

只有出现“历史数据全部失败，无法参与本轮评分”时，才表示该股票在本轮没有可用历史数据，会被跳过。

### 1.4 技术栈

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
├── get_all_realtime()          # 全市场实时行情（东方财富优先，新浪备用）
├── get_history()               # 日K线（前复权）
├── get_minute_history()        # 分钟K线（批量，N天）
├── get_minute_history_by_date()# 分钟K线（指定日期）
├── get_limit_up_pool()         # 涨停股池
├── get_all_concept_sectors()   # 概念板块数据
├── get_market_sentiment()      # 大盘情绪指标
├── get_stock_name(code)        # 获取股票名称（从缓存）
├── set_stock_name(code, name)  # 设置股票名称（写入缓存）
└── batch_resolve_names(codes)  # 批量解析股票名称（AKShare + 持久化）
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
实时行情:
东方财富 (stock_zh_a_spot_em)
  → 失败/超时后切换新浪 (stock_zh_a_spot)
  → 全部失败使用最近实时缓存（应急参考）

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
样本>30  → apply_adaptive_scores() → 自适应评分
```

虚拟盘信号语义:
- `scan_price`: 扫描当时观察价/计划买入价
- `exit_plan`: 当前固定为 `next_open`
- `planned_exit_date`: 实际结算后写入次日交易日期
- `next_exit_price`: 信号日之后第一个交易日开盘价
- `next_return_pct`: `next_exit_price / scan_price - 1`

注意: 虚拟盘不再用“下一次扫描时的实时价”作为卖出价。未取得次日开盘日K时，记录保持 `open`，等待下一次扫描复盘。

自适应评分使用 LogisticRegression 学习历史成功模式，需要至少 30 条已复盘样本才启用。

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
        "adaptive_min_samples": 30
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
