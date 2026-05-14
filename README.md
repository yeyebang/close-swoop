# A股尾盘涨停扫描器

> 每日 14:00 扫描全 A 股，筛选尾盘拉升、次日有望高开的股票

[English](#english) | [项目介绍](#项目介绍) | [快速开始](#快速开始) | [功能特性](#功能特性) | [命令行](#命令行) | [Web界面](#web界面) | [开发文档](#开发文档)

---

## 项目介绍

本项目是一个基于 Python 的 A 股尾盘选股工具，通过规则评分 + 机器学习模型 + 风控过滤，构建从扫描到验证的尾盘策略闭环工作台。

### 策略核心（v4.0）

```
14:00 风控过滤 + 扫描大盘（5000+ 只 → 风控剔除 → 候选池 ~50 只）
    → 计算 21 个特征 + 规则评分
    → 二次增强（同花顺/腾讯分笔验证）
14:05/14:10/14:15/14:20 跟踪候选池
    → 计算跟踪评分（涨幅趋势、量价配合、回撤等）
14:20 生成最终买入参考
    → 最终评分 = 初筛评分 × 40% + 跟踪评分 × 60% - 风险扣分
次日 9:30 开盘验证
    → 结算次日开盘收益 + 30分钟冲高收益
    → 成功/失败样本沉淀，反向优化评分权重
```

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 执行 V4 流程
python run.py v4-scan    # 扫描大盘，创建候选池
python run.py v4-track   # 跟踪候选池（每隔 5 分钟运行一次）
python run.py v4-verify # 次日验证候选股的收益表现

# 启动 Web 界面
python run.py ui
# 访问 http://127.0.0.1:3002
```

---

## 功能特性

### 核心功能

- **风控过滤** — 硬剔除（ST、停牌、一字板、低股价、低成交额等）+ 降权提醒（高振幅、长上影、板块弱等）
- **全市场扫描** — 覆盖沪深 A 股，14:00 自动/手动触发，5200+ 只股票快速扫描
- **21 维特征体系** — 涨幅、量比、均线偏离、换手率、历史股性等
- **规则评分 + ML 模型** — 主观经验评分 + LightGBM 预测次日高开概率
- **分时段跟踪** — 14:00-14:20 多次跟踪候选池，计算跟踪评分
- **二次增强** — 同花顺/腾讯分笔数据验证
- **次日验证闭环** — 真实扫描信号次日标记成功/失败，反向优化后续评分
- **全中文化 UI** — 所有表格、状态、原因、指标均使用中文展示

### 回测系统

- **分钟级回测框架** — 严格时间对齐（14:00 决策 → 次日开盘验证）
- **日线回测（旧版）** — 保留用于对比
- **标签定义** — `next_day_open_return`（次日开盘收益率），贴合实际交易

### Web 界面

- 暗色主题 Dashboard（Tail Strategy Workbench v4.0）
- 「扫描大盘」「跟踪扫描」「次日验证」三个主动作按钮
- 实时扫描状态 — 点击扫描弹出进度弹窗，含阶段指示、进度条和实时日志
- 收益曲线图表
- 候选股 / 回测 / 虚拟盘 / 模型 多页面

---

## 命令行

### V4 尾盘策略闭环

```bash
python run.py v4-scan             # 扫描大盘，创建今日候选池
python run.py v4-track            # 跟踪当前候选池，生成跟踪快照
python run.py v4-verify           # 验证前一交易日的最终候选收益
python run.py v4-state            # 查看当前批次状态和 Top 候选
```

### 扫描（旧版）

```bash
python run.py scan                # 实时扫描一次
python run.py --auto              # 定时扫描（每日14:00）
```

### 回测

```bash
python run.py minute-backtest     # 分钟级回测（推荐）
python run.py backtest            # 日线回测（旧版）
```

### 模型

```bash
python run.py minute-train       # 训练分钟级 ML 模型
python run.py minute-candidates # 输出候选股（带 ML 概率排序）
```

### 其他

```bash
python run.py paper-report        # 虚拟盘报告
python run.py ui                  # 启动 Web 界面
python run.py explain <model>     # Ollama AI 分析
```

---

## Web 界面

启动后访问 `http://127.0.0.1:3002`

```
┌──────────┬─────────────────────────────────────────────┐
│ 侧边导航     │ 顶部栏（尾盘涨停扫描器 v4.0 + 主动作按钮）      │
│            ├─────────────────────────────────────────────┤
│ 仪表盘      │ 扫描大盘/跟踪扫描/次日验证 + 统计卡片 + 候选分布 │
│ 候选股      │ 今日候选池 / 跟踪快照 / 最终候选 / 验证结果     │
│ 回测       │ 成功/失败样本对比 + 回测指标 + 每日明细        │
│ 虚拟盘      │ 虚拟盘台账（含名称、收益率、状态）            │
│ 模型       │ 模型状态 + 特征重要性 + 权重优化建议          │
└──────────┴─────────────────────────────────────────────┘
```

---

## 数据模型（v4.0）

| 数据集 | 文件 | 作用 |
|--------|------|------|
| `scan_batches` | `reports/v4/scan_batches.csv` | 每日扫描批次信息 |
| `candidates` | `reports/v4/candidates.csv` | 入池候选股及评分、风控状态 |
| `excluded` | `reports/v4/excluded.csv` | 风控剔除股票及原因 |
| `tracking_snapshots` | `reports/v4/tracking_snapshots.csv` | 14:00-14:20 跟踪快照 |
| `verifications` | `reports/v4/verifications.csv` | 次日验证结果 |
| `model_feedback` | `reports/v4/model_feedback.json` | 成功/失败样本反馈和权重建议 |

运行数据保存在 `stock_screener/reports/v4/`，默认不提交到 Git。

---

## 技术栈

- **Python 3.9+**
- **AKShare** — A 股历史数据 / 涨停股池 / 股票列表
- **腾讯行情 HTTP** — 全市场实时行情主源（`qt.gtimg.cn`，并发直连）
- **pandas / numpy** — 数据处理
- **LightGBM / sklearn** — 机器学习
- **loguru** — 日志
- **APScheduler** — 定时任务

---

## 项目结构

```
stock-screener/
├── stock_screener/
│    ├── main.py               # CLI 入口（扫描+v4命令）
│    ├── v4_strategy.py         # V4 核心逻辑（扫描/跟踪/验证）
│    ├── data_fetcher.py       # 数据获取层
│    ├── minute_backtest.py    # 回测引擎 + ML 训练
│    ├── enrichment.py         # 二次增强
│    ├── paper.py              # 虚拟盘系统
│    ├── ui_server.py          # Web 后端（含 V4 API）
│     └── web/                  # Web 前端
├── config.json               # 配置文件
├── requirements.txt          # 依赖
├── run.py                   # 启动脚本
├── README.md                # 本文档
└── DEVELOPMENT.md           # 开发文档
```

---

## 配置

编辑 `config.json` 调整参数：

| 参数 | 说明 | 默认值 |
|-----|------|--------|
| `scan_hour` | 扫描时间（小时） | 14 |
| `top_n` | 输出候选数量 | 20 |
| `backtrack_days` | 历史数据天数 | 60 |
| `filters.min_price` | 最低价格 | 3 |
| `filters.max_price` | 最高价格 | 50 |
| `paper.adaptive_min_samples` | 复盘模型最小样本 | 30 |
| `paper.v3_recent_days` | 近期复盘窗口 | 60 |
| `paper.v3_recent_weight` | 近期模型权重 | 0.6 |
| `paper.v3_model_weight` | 修正模型权重 | 0.35 |

---

## 风控过滤规则

### 硬剔除规则
- ST / *ST / 退市整理股 / 风险警示股
- 当日停牌、一字涨停、一字跌停、无成交量
- 上市不足 60 日
- 股价低于 3 元
- 当日成交额低于 1 亿元
- 连续涨停 >= 3 天
- 近 10 日涨幅超过 50%

### 降权提醒规则
- 近 5 日涨幅超过 30%
- 上影线过长 / 当日振幅过大
- 距涨停过近但成交不足
- 大盘环境较弱 / 板块弱于市场

---

## 开发文档

详细技术文档见 [DEVELOPMENT.md](./DEVELOPMENT.md)，包含：

- V4.0 数据模型与风控规则详解
- 核心模块设计
- 特征体系详解
- ML 模型训练流程
- 数据流说明
- API 接口文档
- 已知问题与改进方向

---

## 版本历史

| 版本 | 日期 | 变更 |
|-----|------|------|
| v1.0 | 2026-04 | 初始版本：日线代理回测 + 主观评分 |
| v2.0 | 2026-05 | 分钟级回测引擎 + ML 模型 + 特征体系重构 |
| v2.1 | 2026-05 | 修正标签为次日开盘收益 + Dashboard UI + 股票名称管理 |
| v2.2 | 2026-05 | 修复兼容性 + Canvas 图表 + 懒加载 |
| v2.3 | 2026-05 | 新增扫描进度弹窗（可视化阶段、进度条、实时日志） |
| v3.0 | 2026-05-12 | V3 真实扫描复盘自修正闭环；数据源韧性强化 |
| v3.1 | 2026-05-13 | 实时行情切换腾讯直连；修复次日开盘收益显示；复盘次日收益按钮；候选股历史记录；扫描弹窗最小化；扫描完成自动结算刷新 |
| v4.0 | 2026-05-14 | 尾盘策略闭环工作台：风控过滤层、候选池跟踪、次日验证、V4 CLI + Web UI |

---

## English

Scan the entire A-share market daily at 14:00, filtering stocks with strong afternoon momentum that may gap up at next day's open. V4.0 upgrades the scanner into a full closed-loop trading workstation with risk filtering, pool tracking, next-day verification, and self-optimizing score feedback.

**Key features**: Risk filtering layer, candidate pool tracking, next-day verification, rule + ML scoring, 21-dimensional feature system, full Chinese UI.

```bash
pip install -r requirements.txt
python run.py v4-scan    # scan the market
python run.py v4-track   # track candidate pool
python run.py v4-verify  # verify next-day returns
python run.py v4-state   # checkpoint status
python run.py ui         # http://127.0.0.1:3002
```

---

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股票投资有风险，决策需谨慎。

---

## License

MIT

---
