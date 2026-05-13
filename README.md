# A股尾盘涨停扫描器

> 每日 14:00 扫描全 A 股，筛选尾盘拉升、次日有望高开的股票

[English](#english) | [项目介绍](#项目介绍) | [快速开始](#快速开始) | [功能特性](#功能特性) | [命令行](#命令行) | [Web界面](#web界面) | [开发文档](#开发文档)

---

## 项目介绍

本项目是一个基于 Python 的 A 股尾盘选股工具，通过规则评分 + 机器学习模型，筛选出尾盘有拉升迹象的股票，供次日开盘参考。

### 策略逻辑

```
14:00 选股
  → 计算 21 个特征（涨幅、量比、均线偏离、历史股性等）
  → 规则评分 + ML 模型预测次日高开概率
  → 二次增强（同花顺/腾讯分笔验证）
  → 输出 Top N 候选股（含代码、名称）
次日 9:30 开盘验证
```

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 实时扫描
python run.py scan

# 启动 Web 界面
python run.py ui
# 访问 http://127.0.0.1:3002
```

---

## 功能特性

### 核心功能

- **全市场扫描** — 覆盖沪深 A 股，14:00 自动/手动触发
- **21 维特征体系** — 涨幅、量比、均线偏离、换手率、历史股性等
- **规则评分** — 主观经验评分，快速筛选
- **ML 模型排序** — LightGBM 学习特征权重，预测次日高开概率
- **二次增强** — 同花顺/腾讯分笔数据验证
- **V3复盘自修正** — 真实扫描信号次日标记成功/失败，并反向修正后续评分

### 回测系统

- **分钟级回测框架** — 严格时间对齐（14:00 决策 → 次日开盘验证）
- **日线回测（旧版）** — 保留用于对比
- **标签定义** — `next_day_open_return`（次日开盘收益率），贴合实际交易

### Web 界面

- 暗色主题 Dashboard
- 实时扫描状态 — 点击扫描弹出进度弹窗，含阶段指示、进度条和实时日志
- 收益曲线图表
- 候选股 / 回测 / 虚拟盘 / 模型 多页面

---

## 命令行

### 扫描

```bash
python run.py scan              # 实时扫描一次
python run.py --auto            # 定时扫描（每日14:00）
```

### 回测

```bash
python run.py minute-backtest   # 分钟级回测（推荐）
python run.py backtest          # 日线回测（旧版）
```

### 模型

```bash
python run.py minute-train     # 训练分钟级 ML 模型
python run.py minute-candidates # 输出候选股（带 ML 概率排序）
```

### 其他

```bash
python run.py paper-report      # 虚拟盘报告
python run.py ui                # 启动 Web 界面
python run.py explain <model>   # Ollama AI 分析
```

---

## Web 界面

启动后访问 `http://127.0.0.1:3002`

```
┌──────────┬──────────────────────────────────────┐
│ 侧边导航   │ 顶部栏（标题 + 开始扫描按钮）          │
│          ├──────────────────────────────────────┤
│ 仪表盘    │ 统计卡片 + 收益曲线 + 候选分布        │
│ 候选股    │ 全部回测候选明细表（含名称）            │
│ 回测     │ 回测指标 + 每日回测明细               │
│ 虚拟盘    │ 虚拟盘台账（含名称、收益率、状态）     │
│ 模型     │ 模型状态 + 特征重要性图表             │
└──────────┴──────────────────────────────────────┘
```

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
│   ├── main.py             # CLI 入口
│   ├── data_fetcher.py     # 数据获取层
│   ├── minute_backtest.py  # 回测引擎 + ML 训练
│   ├── enrichment.py       # 二次增强
│   ├── paper.py            # 虚拟盘系统
│   ├── ui_server.py        # Web 后端
│   └── web/                # Web 前端
├── config.json             # 配置文件
├── requirements.txt        # 依赖
├── run.py                 # 启动脚本
├── README.md              # 本文档
└── DEVELOPMENT.md         # 开发文档
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
| `paper.adaptive_min_samples` | V3复盘模型最小样本 | 30 |
| `paper.v3_recent_days` | V3近期复盘窗口 | 60 |
| `paper.v3_recent_weight` | V3近期模型权重 | 0.6 |
| `paper.v3_model_weight` | V3修正模型权重 | 0.35 |

---

## 开发文档

详细技术文档见 [DEVELOPMENT.md](./DEVELOPMENT.md)，包含：

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

---

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股票投资有风险，决策需谨慎。

---

## License

MIT

---

<br>

## English

### A-share Limit-Up Scanner

Scan the entire A-share market daily at 14:00, filtering stocks with strong afternoon momentum that may gap up at next day's open.

**Key features**: 21-dimensional feature system, rule-based scoring + ML ranking, minute-level backtest framework, self-iterating paper trading.

```bash
pip install -r requirements.txt
python run.py scan
python run.py ui  # http://127.0.0.1:3002
```
