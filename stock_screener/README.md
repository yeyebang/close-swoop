# A股尾盘涨停扫描器 - 使用指南（改进版）

## 快速开始

```bash
cd /Users/aw/Documents/开发项目/stock-screener
pip install -r requirements.txt
python run.py scan
```

## 功能

- 每日14:00自动扫描全A股
- 筛选尾盘拉升、有涨停潜力的股票
- 输出候选股名单到终端和CSV文件
- 数据自动保存到/data/目录

### 旧版（日线代理，时间对齐有误，仅供对比）
- 使用缓存日线做规则代理回测
- 训练监督学习模型，给候选股做排序

### 新版（分钟级，时间对齐正确）
- 分钟级回测：严格使用14:00之前数据，标签是当天15:00是否涨停
- 分钟级ML模型：LightGBM 自动学习特征权重
- 新增板块/情绪/历史特征

- 可选调用本地Ollama解释候选股风险

## 参数设置

编辑 `config.json` 调整参数：
- `scan_hour`: 扫描时间（默认14点）
- `top_n`: 返回候选股数量（默认20）
- `exclude_st`: 是否排除ST股

## 常用命令

```bash
# 实时扫描
python run.py scan

# 定时扫描
python run.py --auto
```

### 日线代理回测（旧版，时间对齐有误，仅供对比）

```bash
# 日线代理回测
python run.py backtest

# 训练日线模型
python run.py train

# 输出日线候选
python run.py candidates 20
```

### 分钟级回测（新版，时间对齐正确）

```bash
# 分钟级回测：14:00选股 -> 当天15:00验证涨停
python run.py minute-backtest

# 训练分钟级ML模型（自动LightGBM，缺依赖退回sklearn）
python run.py minute-train

# 输出分钟级候选（带ML概率排序）
python run.py minute-candidates 20
```

### 其他

```bash
# 查看虚拟盘复盘报告
python run.py paper-report

# 用本地Ollama解释候选股
python run.py explain qwen3.6:35b-a3b-q8_0

# 打开本地Web工作台
python run.py ui
```

打开后访问：

```text
http://127.0.0.1:3002
```

## 重要说明

**新版分钟级回测**已经解决了日线代理的前瞻偏差问题：
- 严格使用14:00之前的数据做决策
- 标签是"当天15:00是否涨停"
- 新增了25个分钟级特征（尾盘涨幅、成交量加速、VWAP等）
- ML模型自动学习特征权重，替代主观评分

## 虚拟盘自我迭代

`python run.py scan` 会自动做三件事：

1. 复盘之前未结算的虚拟盘推荐；
2. 在样本足够后，用复盘结果训练自适应排序分；
3. 把本次 Top 候选写入 `stock_screener/paper/paper_trades.csv`。

自适应评分默认需要至少30条已复盘样本才启用，避免样本太少时过拟合。
