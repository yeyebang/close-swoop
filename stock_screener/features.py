#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一特征工程

从分钟K线、日K线、板块/情绪数据中提取特征，输出统一特征字典。
"""

import numpy as np
import pandas as pd
from typing import Optional


# ==================== 分钟级特征（核心） ====================

def calc_minute_features(minute_df: pd.DataFrame, current_row: dict) -> dict:
    """
    从分钟K线计算尾盘特征

    Args:
        minute_df: 标准化后的分钟K线 DataFrame（columns: datetime, open, close, high, low, volume, amount）
        current_row: 实时行情中的一只股票数据（来自 DataFetcher.get_all_realtime）
    Returns:
        特征字典
    """
    features = {}

    if minute_df is None or minute_df.empty or len(minute_df) < 20:
        return _default_minute_features()

    hist = minute_df.copy()
    hist["datetime"] = pd.to_datetime(hist["datetime"], errors="coerce")
    hist = hist.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    if len(hist) < 20:
        return _default_minute_features()

    curr_price = float(current_row.get("last_price", 0))
    curr_change = float(current_row.get("change_pct", 0))
    curr_volume_ratio = float(current_row.get("volume_ratio", 0))
    curr_turnover = float(current_row.get("turnover_rate", 0))

    # 1. 尾盘30分钟涨幅（13:30-14:00）
    last_30min = hist[hist["datetime"] >= hist["datetime"].iloc[-1] - pd.Timedelta(minutes=30)]
    if len(last_30min) >= 3:
        features["late_30m_return"] = (curr_price / last_30min["close"].iloc[0] - 1) * 100
    else:
        features["late_30m_return"] = 0

    # 2. 尾盘30分钟涨幅占全天涨幅比例（越高说明尾盘越异动）
    if curr_change != 0:
        late_ratio = abs(features["late_30m_return"]) / max(abs(curr_change), 0.01)
        features["late_ratio"] = late_ratio
    else:
        features["late_ratio"] = 0

    # 3. 尾盘30分钟成交量占全天比例
    last_30m_vol = last_30min["volume"].sum() if len(last_30min) >= 3 else 0
    total_vol = hist["volume"].sum()
    features["late_vol_ratio"] = last_30m_vol / max(total_vol, 1)

    # 4. 尾盘成交量趋势：后1/3 vs 前2/3
    split_idx = len(hist) // 3
    early_vol = hist.iloc[:split_idx]["volume"].mean()
    late_vol = hist.iloc[split_idx:]["volume"].mean()
    features["late_vol_trend"] = late_vol / max(early_vol, 1)

    # 5. 最后10根K线（50分钟）的成交量标准差 / 均值 = 放量稳定性
    if len(hist) >= 10:
        recent_vol = hist["volume"].iloc[-10:]
        features["vol_stability"] = recent_vol.mean() / max(recent_vol.std(), 1)
    else:
        features["vol_stability"] = 1.0

    # 6. 价格相对日内高低点位置
    intraday_high = hist["high"].max()
    intraday_low = hist["low"].min()
    if intraday_high > intraday_low:
        features["price_position_intraday"] = (curr_price - intraday_low) / (intraday_high - intraday_low) * 100
    else:
        features["price_position_intraday"] = 50

    # 7. 收盘价相对收盘价的位置（偏多还是偏空）
    if len(hist) >= 20:
        recent_closes = hist["close"].iloc[-20:]
        features["close_vs_recent"] = (curr_price / recent_closes.mean() - 1) * 100
    else:
        features["close_vs_recent"] = 0

    # 8. 近10根K线（50分钟）的最大涨幅（捕捉脉冲式拉升）
    if len(hist) >= 10:
        recent_returns = hist["close"].pct_change().iloc[-10:].dropna()
        features["max_surge_10bar"] = float(recent_returns.max()) * 100
    else:
        features["max_surge_10bar"] = 0

    # 9. 近10根K线累计涨幅
    if len(hist) >= 10:
        features["return_10bar"] = (hist["close"].iloc[-1] / hist["close"].iloc[-10] - 1) * 100
    else:
        features["return_10bar"] = 0

    # 10. 成交量加权均价（VWAP）
    hist["vp"] = hist["volume"] * ((hist["high"] + hist["low"] + hist["close"]) / 3)
    total_vol_vwap = hist["volume"].sum()
    if total_vol_vwap > 0:
        vwap = hist["vp"].sum() / total_vol_vwap
        features["price_vs_vwap"] = (curr_price / vwap - 1) * 100
    else:
        features["price_vs_vwap"] = 0

    # 11. 尾盘成交额趋势（最后10根 vs 之前10根）
    if len(hist) >= 20:
        recent_amt = hist["amount"].iloc[-10:].mean()
        prev_amt = hist["amount"].iloc[-20:-10].mean()
        features["amount_trend"] = recent_amt / max(prev_amt, 1)
    else:
        features["amount_trend"] = 1.0

    # 12. 最近5根K线（25分钟）上涨K线比例
    if len(hist) >= 5:
        up_bars = (hist["close"].iloc[-5:].to_numpy() > hist["close"].iloc[-6:-1].to_numpy()).sum()
        features["up_bar_ratio_5"] = up_bars / 5
    else:
        features["up_bar_ratio_5"] = 0.5

    # 13. 换手率（实时）
    features["turnover_rate"] = curr_turnover

    # 14. 量比（实时）
    features["volume_ratio"] = curr_volume_ratio

    # 15. 涨跌幅（实时）
    features["change_pct"] = curr_change

    return features


def _default_minute_features() -> dict:
    """默认特征（无分钟数据时的降级）"""
    return {
        "late_30m_return": 0,
        "late_ratio": 0,
        "late_vol_ratio": 0,
        "late_vol_trend": 1,
        "vol_stability": 1,
        "price_position_intraday": 50,
        "close_vs_recent": 0,
        "max_surge_10bar": 0,
        "return_10bar": 0,
        "price_vs_vwap": 0,
        "amount_trend": 1,
        "up_bar_ratio_5": 0.5,
        "turnover_rate": 0,
        "volume_ratio": 0,
        "change_pct": 0,
    }


# ==================== 日K 特征（保留但降权重） ====================

def calc_daily_features(history_df: pd.DataFrame) -> dict:
    """
    从日K线计算特征

    Args:
        history_df: 标准化后的日K DataFrame
    Returns:
        特征字典
    """
    features = {}

    if history_df is None or history_df.empty or len(history_df) < 10:
        return _default_daily_features()

    hist = history_df.copy()
    required = ["date", "open", "close", "high", "low", "volume"]
    if not all(c in hist.columns for c in required):
        return _default_daily_features()

    hist = hist.sort_values("date").reset_index(drop=True)
    hist["close"] = pd.to_numeric(hist["close"], errors="coerce").fillna(0)
    hist["volume"] = pd.to_numeric(hist["volume"], errors="coerce").fillna(0)
    hist["turnover_rate"] = pd.to_numeric(hist.get("turnover_rate", 0), errors="coerce").fillna(0)

    curr = hist.iloc[-1]

    # 1. MA5/10/20 偏离度
    features["ma5_dev"] = (curr["close"] / hist["close"].iloc[-5:].mean() - 1) * 100
    features["ma10_dev"] = (curr["close"] / hist["close"].iloc[-10:].mean() - 1) * 100
    if len(hist) >= 20:
        features["ma20_dev"] = (curr["close"] / hist["close"].iloc[-20:].mean() - 1) * 100
    else:
        features["ma20_dev"] = 0

    # 2. 均线多头排列
    ma_vals = []
    for n in [5, 10, 20]:
        if len(hist) >= n:
            ma_vals.append(hist["close"].iloc[-n:].mean())
    features["ma_bull_aligned"] = (len(ma_vals) == 3 and ma_vals[0] > ma_vals[1] > ma_vals[2])

    # 3. 近N日涨幅
    if len(hist) >= 5:
        features["return_5d"] = (curr["close"] / hist["close"].iloc[-5] - 1) * 100
    else:
        features["return_5d"] = 0
    if len(hist) >= 10:
        features["return_10d"] = (curr["close"] / hist["close"].iloc[-10] - 1) * 100
    else:
        features["return_10d"] = 0
    if len(hist) >= 20:
        features["return_20d"] = (curr["close"] / hist["close"].iloc[-20] - 1) * 100
    else:
        features["return_20d"] = 0

    # 4. 成交量趋势
    vol_ma5 = hist["volume"].iloc[-5:-1].mean()
    vol_ma10 = hist["volume"].iloc[-10:-1].mean() if len(hist) >= 10 else vol_ma5
    features["vol_vs_ma5"] = curr["volume"] / max(vol_ma5, 1)
    features["vol_vs_ma10"] = curr["volume"] / max(vol_ma10, 1)

    # 5. 换手率趋势
    turn_ma5 = hist["turnover_rate"].iloc[-5:-1].mean() if len(hist) >= 5 else hist["turnover_rate"].iloc[-1]
    features["turn_vs_ma5"] = turn_ma5 if turn_ma5 > 0 else 1   # placeholder, set below
    features["turn_vs_ma5"] = curr["turnover_rate"] / max(turn_ma5, 0.1) if "turnover_rate" in hist.columns else 1

    # 6. 波动率（近20日收益率标准差）
    if len(hist) >= 20:
        returns = hist["close"].pct_change().dropna().iloc[-20:]
        features["volatility_20d"] = float(returns.std()) * 100
    elif len(hist) >= 10:
        returns = hist["close"].pct_change().dropna().iloc[-10:]
        features["volatility_20d"] = float(returns.std()) * 100
    else:
        features["volatility_20d"] = 0

    # 7. 振幅
    features["amplitude"] = float(pd.to_numeric(curr.get("amplitude", curr["high"] - curr["low"]), errors="coerce") or 0)

    # 8. 涨跌幅
    features["change_pct_daily"] = float(pd.to_numeric(curr.get("change_pct", 0), errors="coerce") or 0)

    # 9. 换手率
    features["turnover_rate_daily"] = float(pd.to_numeric(curr.get("turnover_rate", 0), errors="coerce") or 0)

    # 10. 量比
    features["volume_ratio_daily"] = float(pd.to_numeric(curr.get("volume_ratio", 0), errors="coerce") or 0)

    return features


def _default_daily_features() -> dict:
    """默认日K特征"""
    return {
        "ma5_dev": 0, "ma10_dev": 0, "ma20_dev": 0,
        "ma_bull_aligned": False,
        "return_5d": 0, "return_10d": 0, "return_20d": 0,
        "vol_vs_ma5": 1, "vol_vs_ma10": 1,
        "turn_vs_ma5": 1,
        "volatility_20d": 0,
        "amplitude": 0, "change_pct_daily": 0, "turnover_rate_daily": 0, "volume_ratio_daily": 0,
    }


# ==================== 板块/情绪特征 ====================

def calc_sentiment_features(sentiment: dict) -> dict:
    """
    从大盘情绪数据中提取特征

    Args:
        sentiment: DataFetcher.get_market_sentiment() 返回的字典
    Returns:
        特征字典
    """
    if not sentiment:
        return _default_sentiment_features()

    rise = sentiment.get("rise_count", 0)
    fall = sentiment.get("fall_count", 0)
    total = sentiment.get("total_stocks", 0)
    limit_up = sentiment.get("limit_up_count", 0)
    limit_down = sentiment.get("limit_down_count", 0)
    total_amount = sentiment.get("total_amount", 0)

    rise_fall_ratio = rise / max(fall, 1)
    rise_pct = rise / max(total, 1) * 100

    return {
        "rise_fall_ratio": rise_fall_ratio,
        "rise_pct": rise_pct,
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "limit_up_minus_down": limit_up - limit_down,
        "total_amount_billion": total_amount / 1e9 if total_amount > 0 else 0,
        "market_breadth": rise_pct,
    }


def _default_sentiment_features() -> dict:
    """默认情绪特征"""
    return {
        "rise_fall_ratio": 1.0,
        "rise_pct": 50.0,
        "limit_up_count": 0,
        "limit_down_count": 0,
        "limit_up_minus_down": 0,
        "total_amount_billion": 0,
        "market_breadth": 50.0,
    }


# ==================== 统一入口 ====================

def build_features(
    minute_df: pd.DataFrame,
    history_df: pd.DataFrame,
    current_row: dict,
    sentiment: dict,
) -> dict:
    """
    统一特征计算入口

    Args:
        minute_df: 分钟K线数据（来自 DataFetcher.get_minute_history）
        history_df: 日K线数据（来自 DataFetcher.get_history）
        current_row: 实时行情中的一只股票（来自 DataFetcher.get_all_realtime）
        sentiment: 大盘情绪数据（来自 DataFetcher.get_market_sentiment）
    Returns:
        合并后的特征字典
    """
    # 分钟特征
    minute_feats = calc_minute_features(minute_df, current_row)

    # 日K特征
    daily_feats = calc_daily_features(history_df)

    # 情绪特征
    sent_feats = calc_sentiment_features(sentiment)

    # 合并
    features = {}
    features.update(minute_feats)
    # 日K特征覆盖同名分钟特征（日K更准）
    for k, v in daily_feats.items():
        if k in ("turnover_rate", "volume_ratio", "change_pct"):
            continue   # 保留分钟数据的实时值
        features[k] = v
    features.update(sent_feats)

    # 衍生特征
    # 尾盘异动综合评分
    features["late_surge_score"] = (
        minute_feats.get("late_ratio", 0) * 20 +    # 尾盘涨幅占全天比例
        minute_feats.get("late_vol_ratio", 0) * 30 +   # 尾盘放量
        minute_feats.get("vol_stability", 1) * 10 +    # 放量稳定性
        minute_feats.get("up_bar_ratio_5", 0.5) * 20 +   # 上涨K线比例
        minute_feats.get("late_30m_return", 0) * 2 +    # 尾盘涨幅
        daily_feats.get("ma_bull_aligned", False) * 10 +   # 均线多头
        daily_feats.get("vol_vs_ma5", 1) * 5   # 放量
    )

    return features


def features_to_dataframe(features_list: list[dict]) -> pd.DataFrame:
    """
    将特征列表转为 DataFrame（用于模型训练/推理）

    Args:
        features_list: 特征字典列表
    Returns:
        DataFrame
    """
    if not features_list:
        return pd.DataFrame()

    df = pd.DataFrame(features_list)

    # 确保数值列是数值类型
    numeric_cols = df.select_dtypes(include=[np.number, bool]).columns
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # 布尔列转 0/1
    bool_cols = df.select_dtypes(include=[bool]).columns
    for col in bool_cols:
        df[col] = df[col].astype(int)

    return df
