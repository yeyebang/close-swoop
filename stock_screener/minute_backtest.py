#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分钟级回测引擎 v2

核心逻辑：
1. 14:00 选股（使用 09:30-14:00 数据）
2. 标签：次日开盘价相对于当日收盘价的收益率（隔夜收益）
3. 严格时间对齐，无前向偏差
4. 分钟线数据驱动
"""

from __future__ import annotations

import json
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))

# 分钟特征列（用于ML模型）
MINUTE_FEATURE_COLUMNS = [
    "change_pct_1400",
    "volume_ratio_1400",
    "turnover_rate_1400",
    "late_30m_return",
    "late_60m_return",
    "late_vol_ratio",
    "vol_acceleration",
    "price_vs_vwap",
    "consecutive_up_bars",
    "max_pullback_30m",
    "ma5_dev",
    "ma10_dev",
    "return_5d",
    "return_10d",
    "vol_vs_ma5",
    "ma_bull_aligned",
    "price_position_10d",
    "volatility_10d",
    "hist_limit_up_rate_20d",
    "recent_high_touch_count",
    "avg_amplitude_20d",
]

MODEL_DIR = Path(__file__).parent / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

_stock_name_cache: dict[str, str] = {}


def _load_stock_names(data_dir: Path) -> dict[str, str]:
    global _stock_name_cache
    if _stock_name_cache:
        return _stock_name_cache
    name_path = data_dir / "stock_names.json"
    if name_path.exists():
        try:
            _stock_name_cache = json.loads(name_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _stock_name_cache


def _get_stock_name(code: str, data_dir: Path) -> str:
    names = _load_stock_names(data_dir)
    return names.get(code, "")


@dataclass
class MinuteBacktestConfig:
    top_n: int = 20
    min_price: float = 3.0
    max_price: float = 50.0
    exclude_prefixes: tuple[str, ...] = ("300", "301", "688", "920")
    signal_hour: int = 14
    signal_minute: int = 0


def _limit_rate(code: str) -> float:
    if code.startswith(("300", "301", "688")):
        return 0.20
    if code.startswith("8") or code.startswith("9"):
        return 0.30
    return 0.10


def _calc_limit_price(pre_close: float, code: str) -> float:
    rate = _limit_rate(code)
    return round(pre_close * (1 + rate), 2)


def _load_daily_history(data_dir: Path, code: str) -> Optional[pd.DataFrame]:
    hist_path = data_dir / f"history_{code}.csv"
    if not hist_path.exists():
        return None
    try:
        df = pd.read_csv(hist_path)
        if df.empty:
            return None
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        for col in ["开盘", "收盘", "最高", "最低", "成交量", "换手率", "涨跌幅"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "涨跌幅" in df.columns and "收盘" in df.columns:
            df["昨收"] = df["收盘"] / (1 + df["涨跌幅"] / 100)
            df.loc[df["涨跌幅"] == 0, "昨收"] = df.loc[df["涨跌幅"] == 0, "收盘"]
        return df.sort_values("日期")
    except Exception:
        return None


def _calc_daily_features(hist: pd.DataFrame, up_to_idx: int) -> dict:
    """计算截至 up_to_idx 行的日K特征（不含未来信息）"""
    hist = hist.iloc[:up_to_idx + 1].tail(60)
    if len(hist) < 25:
        return {}

    close = hist["收盘"].values
    high = hist["最高"].values
    low = hist["最低"].values
    volume = hist["成交量"].values

    ma5 = close[-5:].mean()
    ma10 = close[-10:].mean()
    ma20 = close[-20:].mean() if len(close) >= 20 else 0
    current_close = close[-1]

    ma5_dev = (current_close / ma5 - 1) * 100 if ma5 > 0 else 0
    ma10_dev = (current_close / ma10 - 1) * 100 if ma10 > 0 else 0
    return_5d = (current_close / close[-6] - 1) * 100 if len(close) >= 6 else 0
    return_10d = (current_close / close[-11] - 1) * 100 if len(close) >= 11 else 0

    vol_ma5 = volume[-5:-1].mean() if len(volume) >= 6 else volume[-1]
    vol_vs_ma5 = volume[-1] / max(vol_ma5, 1)

    ma_bull = 1 if (ma5 > ma10 > ma20 and ma20 > 0) else 0

    rolling_high = max(high[-10:]) if len(high) >= 10 else high[-1]
    rolling_low = min(low[-10:]) if len(low) >= 10 else low[-1]
    price_position = ((current_close - rolling_low) / (rolling_high - rolling_low) * 100) if (rolling_high > rolling_low) else 50

    if len(close) >= 11:
        returns = pd.Series(close[-11:]).pct_change().dropna()
        volatility = returns.std() * 100
    else:
        volatility = 0

    return {
        "ma5_dev": round(ma5_dev, 4),
        "ma10_dev": round(ma10_dev, 4),
        "return_5d": round(return_5d, 4),
        "return_10d": round(return_10d, 4),
        "vol_vs_ma5": round(vol_vs_ma5, 4),
        "ma_bull_aligned": ma_bull,
        "price_position_10d": round(price_position, 4),
        "volatility_10d": round(volatility, 4),
    }


def _calc_minute_features(minute_df: pd.DataFrame, signal_hour: int = 14, signal_minute: int = 0) -> Optional[dict]:
    if minute_df is None or minute_df.empty:
        return None

    df = minute_df.copy()
    time_col = "时间" if "时间" in df.columns else "datetime"
    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df = df.dropna(subset=[time_col]).sort_values(time_col)

    if len(df) < 20:
        return None

    close = pd.to_numeric(df["收盘"], errors="coerce").fillna(0)
    volume = pd.to_numeric(df["成交量"], errors="coerce").fillna(0)

    signal_time = df[time_col].max().replace(hour=signal_hour, minute=signal_minute, second=0, microsecond=0)
    signal_mask = df[time_col] <= signal_time
    signal_df = df[signal_mask]

    if len(signal_df) < 10:
        return None

    signal_close = float(signal_df.iloc[-1]["收盘"])
    signal_open = float(df.iloc[0]["开盘"])
    pre_close = float(df.iloc[0].get("昨收", signal_open / 1.02))

    change_pct_1400 = (signal_close / pre_close - 1) * 100 if pre_close > 0 else 0

    signal_volume = float(signal_df["成交量"].sum())
    full_volume = float(volume.sum())
    volume_ratio_1400 = signal_volume / max(full_volume * 0.5, 1)

    last_30_idx = max(len(signal_df) - 6, 0)
    last_60_idx = max(len(signal_df) - 12, 0)
    last_30m = signal_df.iloc[last_30_idx:]
    last_60m = signal_df.iloc[last_60_idx:]

    late_30m_return = (signal_close / float(last_30m.iloc[0]["收盘"]) - 1) * 100 if len(last_30m) >= 3 and float(last_30m.iloc[0]["收盘"]) > 0 else 0
    late_60m_return = (signal_close / float(last_60m.iloc[0]["收盘"]) - 1) * 100 if len(last_60m) >= 3 and float(last_60m.iloc[0]["收盘"]) > 0 else 0

    late_vol = float(last_30m["成交量"].sum())
    late_vol_ratio = late_vol / max(signal_volume, 1)

    half = len(signal_df) // 2
    early_vol = float(signal_df.iloc[:half]["成交量"].mean())
    late_vol_mean = float(signal_df.iloc[half:]["成交量"].mean())
    vol_acceleration = late_vol_mean / max(early_vol, 1)

    vwap_num = (signal_df["成交量"] * ((signal_df["最高"] + signal_df["最低"] + signal_df["收盘"]) / 3)).sum()
    vwap_den = float(signal_df["成交量"].sum())
    price_vs_vwap = (signal_close / (vwap_num / vwap_den) - 1) * 100 if vwap_den > 0 else 0

    closes = signal_df["收盘"].values
    up_count = 0
    for i in range(len(closes) - 1, -1, -1):
        if i < len(closes) - 1 and closes[i] < closes[i + 1]:
            break
        up_count += 1
    consecutive_up_bars = up_count

    last_30_closes = signal_df.iloc[-6:]["收盘"].values if len(signal_df) >= 6 else closes
    if len(last_30_closes) >= 2:
        max_pullback_30m = (max(last_30_closes) - min(last_30_closes)) / max(min(last_30_closes), 0.01) * 100
    else:
        max_pullback_30m = 0

    turnover = float(signal_df.iloc[-1].get("换手率", 0))

    return {
        "change_pct_1400": round(change_pct_1400, 4),
        "volume_ratio_1400": round(volume_ratio_1400, 4),
        "turnover_rate_1400": round(turnover, 4),
        "late_30m_return": round(late_30m_return, 4),
        "late_60m_return": round(late_60m_return, 4),
        "late_vol_ratio": round(late_vol_ratio, 4),
        "vol_acceleration": round(vol_acceleration, 4),
        "price_vs_vwap": round(price_vs_vwap, 4),
        "consecutive_up_bars": consecutive_up_bars,
        "max_pullback_30m": round(max_pullback_30m, 4),
    }


def _calc_history_features(hist: pd.DataFrame, up_to_idx: int) -> dict:
    result = {
        "hist_limit_up_rate_20d": 0.0,
        "recent_high_touch_count": 0,
        "avg_amplitude_20d": 0.0,
    }
    try:
        hist_window = hist.iloc[:up_to_idx + 1].tail(25)
        if "涨跌幅" in hist.columns:
            limit_count = (hist_window["涨跌幅"] >= 9.8).sum()
            result["hist_limit_up_rate_20d"] = round(limit_count / max(len(hist_window), 1) * 100, 4)

        if "振幅" in hist.columns:
            hist_window_num = pd.to_numeric(hist_window["振幅"], errors="coerce")
            result["avg_amplitude_20d"] = round(float(hist_window_num.mean()), 4)

        result["recent_high_touch_count"] = int((hist_window["涨跌幅"] >= 8.5).sum())
    except Exception:
        pass
    return result


def build_minute_dataset(data_dir: Path, cfg: Optional[MinuteBacktestConfig] = None) -> pd.DataFrame:
    cfg = cfg or MinuteBacktestConfig()
    daily_files = sorted(data_dir.glob("history_*.csv"))
    names = _load_stock_names(data_dir)

    all_rows = []

    for hist_path in daily_files:
        code = hist_path.stem.replace("history_", "")
        stock_name = names.get(code, "")

        if any(code.startswith(p) for p in cfg.exclude_prefixes):
            continue

        hist = _load_daily_history(data_dir, code)
        if hist is None or len(hist) < 30:
            continue

        # 遍历历史日期，逐日构建样本
        for idx in range(25, len(hist) - 1):
            date_str = str(hist.iloc[idx]["日期"])[:10]
            next_date_str = str(hist.iloc[idx + 1]["日期"])[:10]

            pre_close = float(hist.iloc[idx].get("昨收", hist.iloc[idx]["收盘"]))
            close_price = float(hist.iloc[idx]["收盘"])
            next_open = float(hist.iloc[idx + 1]["开盘"])

            if close_price < cfg.min_price or close_price > cfg.max_price:
                continue

            daily_feats = _calc_daily_features(hist, idx)
            if not daily_feats:
                continue

            hist_feats = _calc_history_features(hist, idx)

            change_pct = float(hist.iloc[idx].get("涨跌幅", 0))
            if change_pct >= 9.8:
                continue

            turnover = float(hist.iloc[idx].get("换手率", 0))

            # 标签：次日开盘相对于当日收盘的收益率
            next_day_open_return = (next_open / close_price - 1) * 100 if close_price > 0 else 0

            is_strong_open = int(next_day_open_return > 0.5)
            is_limit_up = int(change_pct >= 9.8)

            row_data = {
                "date": date_str,
                "code": code,
                "name": stock_name,
                "pre_close": pre_close,
                "price": close_price,
                "change_pct": round(change_pct, 4),
                "turnover_rate_daily": round(turnover, 4),
                "next_day_open_return": round(next_day_open_return, 4),
                "is_strong_open": is_strong_open,
                "is_limit_up": is_limit_up,
            }
            row_data.update(daily_feats)
            row_data.update(hist_feats)

            all_rows.append(row_data)

    if not all_rows:
        return pd.DataFrame()

    return pd.DataFrame(all_rows)


def rule_score_minute(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(0.0, index=df.index)

    chg = df["change_pct"]
    score += np.select(
        [chg.between(3.5, 7.0), chg.between(2.0, 3.5, inclusive="left"),
         chg.between(7.0, 9.0, inclusive="right"), chg.between(0.5, 2.0, inclusive="left"),
         chg.between(-2.0, 0.5, inclusive="neither")],
        [25, 18, 20, 12, 5], default=0,
    )

    vr = df.get("vol_vs_ma5", pd.Series(1, index=df.index))
    score += np.select([vr >= 2.5, vr >= 2.0, vr >= 1.5, vr >= 1.2, vr >= 1.0],
                       [20, 17, 13, 8, 4], default=0)

    ret5 = df.get("return_5d", pd.Series(0, index=df.index))
    score += np.select([ret5.between(3, 12), ret5.between(0, 3, inclusive="left"),
                        ret5.between(-3, 0, inclusive="neither")],
                       [10, 8, 5], default=0)

    turn = df.get("turnover_rate_daily", pd.Series(0, index=df.index))
    score += np.select([turn.between(5, 15), turn.between(3, 5, inclusive="left"),
                        turn.between(15, 25, inclusive="right")],
                       [15, 12, 10], default=0)

    score += np.where(df.get("ma_bull_aligned", 0) == 1, 5, 0)

    return score


def backtest_minute_rules(dataset: pd.DataFrame, cfg: Optional[MinuteBacktestConfig] = None) -> tuple[pd.DataFrame, dict]:
    cfg = cfg or MinuteBacktestConfig()

    if dataset.empty:
        return pd.DataFrame(), {}

    df = dataset.copy()
    df["score"] = rule_score_minute(df)

    picks = (
        df.sort_values(["date", "score"], ascending=[True, False])
        .groupby("date", group_keys=False)
        .head(cfg.top_n)
        .copy()
    )

    daily = picks.groupby("date").agg(
        picks=("code", "count"),
        avg_score=("score", "mean"),
        avg_next_open_return=("next_day_open_return", "mean"),
        strong_open_rate=("is_strong_open", "mean"),
        avg_change=("change_pct", "mean"),
    )

    daily["equity"] = (1 + daily["avg_next_open_return"] / 100).cumprod()
    running_max = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] / running_max - 1

    metrics = {
        "days": int(len(daily)),
        "picks": int(len(picks)),
        "avg_next_open_return_pct": float(daily["avg_next_open_return"].mean()),
        "strong_open_rate_pct": float(picks["is_strong_open"].mean() * 100),
        "win_rate_pct": float((picks["next_day_open_return"] > 0).mean() * 100),
        "total_return_pct": float((daily["equity"].iloc[-1] - 1) * 100) if len(daily) > 0 else 0,
        "max_drawdown_pct": float(daily["drawdown"].min() * 100) if len(daily) > 0 else 0,
    }

    return picks, metrics


def save_minute_backtest_report(picks: pd.DataFrame, metrics: dict, output_dir: Optional[Path] = None) -> tuple[Path, Path]:
    if output_dir is None:
        output_dir = Path(__file__).parent / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    picks_path = output_dir / "minute_backtest_picks.csv"
    metrics_path = output_dir / "minute_backtest_metrics.json"

    picks.to_csv(picks_path, index=False, encoding="utf-8-sig")
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    return picks_path, metrics_path


def train_minute_model(dataset: pd.DataFrame, features: Optional[list[str]] = None,
                       output_dir: Optional[Path] = None) -> dict:
    try:
        import lightgbm as lgb
        from sklearn.metrics import average_precision_score, roc_auc_score
        lightgbm_available = True
    except Exception as exc:
        lightgbm_available = False
        lgb = None
        lightgbm_error = str(exc).splitlines()[0] if exc else ""
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.metrics import average_precision_score, roc_auc_score

    output_dir = output_dir or MODEL_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    feat_cols = features or MINUTE_FEATURE_COLUMNS
    available = [c for c in feat_cols if c in dataset.columns]
    if not available:
        raise ValueError("没有可用的特征列")

    data = dataset.dropna(subset=available + ["is_strong_open"]).sort_values("date")
    if len(data) < 200:
        raise RuntimeError(f"样本太少，当前只有 {len(data)} 行，需要至少 200 行")

    split_date = data["date"].quantile(0.8)
    train = data[data["date"] <= split_date]
    valid = data[data["date"] > split_date]
    y_train = train["is_strong_open"]
    y_valid = valid["is_strong_open"]

    pos = max(int(y_train.sum()), 1)
    neg = max(int(len(y_train) - y_train.sum()), 1)
    scale_pos = max(neg / pos, 1)

    if lightgbm_available:
        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=300,
            learning_rate=0.03,
            num_leaves=31,
            subsample=0.85,
            colsample_bytree=0.85,
            scale_pos_weight=scale_pos,
            random_state=42,
        )
        model.fit(train[available], y_train, eval_set=[(valid[available], y_valid)], eval_metric="auc")
        model_type = "lightgbm"
    else:
        model = HistGradientBoostingClassifier(
            learning_rate=0.04,
            max_iter=250,
            max_leaf_nodes=31,
            l2_regularization=0.1,
            random_state=42,
        )
        sample_weight = y_train.map({0: 1.0, 1: scale_pos}).astype(float)
        model.fit(train[available], y_train, sample_weight=sample_weight)
        model_type = "sklearn_hist_gradient_boosting"

    prob = model.predict_proba(valid[available])[:, 1]
    metrics = {
        "train_rows": int(len(train)),
        "valid_rows": int(len(valid)),
        "model_type": model_type,
        "positive_rate_pct": float(data["is_strong_open"].mean() * 100),
        "valid_auc": float(roc_auc_score(y_valid, prob)) if y_valid.nunique() > 1 else None,
        "valid_average_precision": float(average_precision_score(y_valid, prob)),
        "features_used": available,
    }
    if not lightgbm_available:
        metrics["lightgbm_fallback_reason"] = lightgbm_error

    if lightgbm_available:
        model.booster_.save_model(str(output_dir / "minute_limit_up_lgbm.txt"))
    else:
        with open(output_dir / "minute_limit_up_sklearn.pkl", "wb") as f:
            pickle.dump(model, f)

    (output_dir / "minute_features.json").write_text(json.dumps(available, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "minute_model_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"分钟级模型训练完成: {model_type}, AUC={metrics['valid_auc']:.4f}")
    return metrics


def load_minute_model(model_dir: Optional[Path] = None):
    model_dir = model_dir or MODEL_DIR

    pkl_path = model_dir / "minute_limit_up_sklearn.pkl"
    txt_path = model_dir / "minute_limit_up_lgbm.txt"

    if pkl_path.exists():
        with open(pkl_path, "rb") as f:
            return pickle.load(f), "sklearn"

    if txt_path.exists():
        try:
            import lightgbm as lgb
            model = lgb.Booster(model_file=str(txt_path))
            return model, "lightgbm"
        except ImportError:
            return None, None

    return None, None


def predict_with_minute_model(df: pd.DataFrame, features: Optional[list[str]] = None,
                               model_dir: Optional[Path] = None) -> pd.DataFrame:
    model, model_type = load_minute_model(model_dir)
    if model is None:
        df["ml_score"] = np.nan
        return df

    feat_cols = features or MINUTE_FEATURE_COLUMNS
    features_path = MODEL_DIR / "minute_features.json"
    if features_path.exists():
        available = json.loads(features_path.read_text(encoding="utf-8"))
    else:
        available = [c for c in feat_cols if c in df.columns]

    missing = [c for c in available if c not in df.columns]
    for c in missing:
        df[c] = 0

    if model_type == "lightgbm":
        df["ml_score"] = model.predict(df[available])
    else:
        df["ml_score"] = model.predict_proba(df[available])[:, 1]

    return df


def calc_history_features_for_stock(code: str, data_dir: Path) -> dict:
    hist_path = data_dir / f"history_{code}.csv"
    result = {
        "hist_limit_up_rate_20d": 0.0,
        "recent_high_touch_count": 0,
        "avg_amplitude_20d": 0.0,
    }
    if not hist_path.exists():
        return result
    try:
        hist = pd.read_csv(hist_path)
        if hist.empty or len(hist) < 25:
            return result
        last_20 = hist.tail(25)
        if "涨跌幅" in hist.columns:
            limit_count = (last_20["涨跌幅"] >= 9.8).sum()
            result["hist_limit_up_rate_20d"] = round(limit_count / max(len(last_20), 1) * 100, 4)
        if "振幅" in hist.columns:
            result["avg_amplitude_20d"] = round(float(pd.to_numeric(last_20["振幅"], errors="coerce").mean()), 4)
        result["recent_high_touch_count"] = int((last_20["涨跌幅"] >= 8.5).sum())
    except Exception:
        pass
    return result
