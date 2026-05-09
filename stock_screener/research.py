#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline research tools for strategy validation and ML ranking.

The cached files in this project are daily bars, so this module implements a
daily proxy backtest. It is useful for validating broad ranking signals before
adding minute-level data for a true 14:00 -> close strategy.
"""

from __future__ import annotations

import json
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))


ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PACKAGE_DIR / "data"
LEGACY_DATA_DIR = PACKAGE_DIR / "stock-screener" / "data"
MODEL_DIR = PACKAGE_DIR / "models"
REPORT_DIR = PACKAGE_DIR / "reports"


FEATURE_COLUMNS = [
    "change_pct",
    "amplitude",
    "turnover_rate",
    "volume_ratio_5",
    "volume_ratio_10",
    "turnover_ratio_5",
    "ma5_dev",
    "ma10_dev",
    "ma20_dev",
    "return_3d",
    "return_5d",
    "return_10d",
    "volatility_10d",
    "price_position_10d",
    "ma_bull_aligned",
]


@dataclass
class BacktestConfig:
    top_n: int = 20
    min_price: float = 3.0
    max_price: float = 50.0
    min_turnover: float = 1.0
    exclude_prefixes: tuple[str, ...] = ("300", "301", "688", "920")


def resolve_data_dir(config: dict | None = None) -> Path:
    """Return the data directory with a compatibility fallback."""
    config = config or {}
    configured = config.get("data_dir")
    if configured:
        data_dir = Path(configured)
        if not data_dir.is_absolute():
            data_dir = ROOT_DIR / data_dir
        if data_dir.exists() and any(data_dir.glob("history_*.csv")):
            return data_dir

    if DEFAULT_DATA_DIR.exists() and any(DEFAULT_DATA_DIR.glob("history_*.csv")):
        return DEFAULT_DATA_DIR
    return LEGACY_DATA_DIR


def history_files(data_dir: Path) -> list[Path]:
    return sorted(data_dir.glob("history_*.csv"))


def _read_history(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"股票代码": str})
    if df.empty:
        return df

    code = path.stem.replace("history_", "")
    df["股票代码"] = df.get("股票代码", code).astype(str).str.zfill(6)
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    numeric_cols = ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["日期", "开盘", "收盘", "最高", "最低"]).sort_values("日期")
    return df.reset_index(drop=True)


def _limit_rate(code: str) -> float:
    if code.startswith(("300", "688")):
        return 0.20
    if code.startswith("8") or code.startswith("9"):
        return 0.30
    return 0.10


def _make_features_for_stock(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 25:
        return pd.DataFrame()

    out = pd.DataFrame()
    code = str(df["股票代码"].iloc[0]).zfill(6)
    close = df["收盘"]
    volume = df["成交量"]
    turnover = df["换手率"]
    high = df["最高"]
    low = df["最低"]

    out["date"] = df["日期"]
    out["code"] = code
    out["open"] = df["开盘"]
    out["close"] = close
    out["high"] = high
    out["low"] = low
    out["volume"] = volume
    out["amount"] = df["成交额"]
    out["change_pct"] = df["涨跌幅"]
    out["amplitude"] = df["振幅"]
    out["turnover_rate"] = turnover

    out["volume_ratio_5"] = volume / volume.rolling(5).mean().shift(1)
    out["volume_ratio_10"] = volume / volume.rolling(10).mean().shift(1)
    out["turnover_ratio_5"] = turnover / turnover.rolling(5).mean().shift(1)
    out["ma5_dev"] = (close / close.rolling(5).mean() - 1) * 100
    out["ma10_dev"] = (close / close.rolling(10).mean() - 1) * 100
    out["ma20_dev"] = (close / close.rolling(20).mean() - 1) * 100
    out["return_3d"] = close.pct_change(3) * 100
    out["return_5d"] = close.pct_change(5) * 100
    out["return_10d"] = close.pct_change(10) * 100
    out["volatility_10d"] = close.pct_change().rolling(10).std() * 100

    rolling_high = high.rolling(10).max()
    rolling_low = low.rolling(10).min()
    out["price_position_10d"] = (close - rolling_low) / (rolling_high - rolling_low).replace(0, np.nan) * 100

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    out["ma_bull_aligned"] = ((ma5 > ma10) & (ma10 > ma20)).astype(int)

    next_open = df["开盘"].shift(-1)
    next_close = df["收盘"].shift(-1)
    next_high = df["最高"].shift(-1)
    next_next_open = df["开盘"].shift(-2)
    limit_price = close * (1 + _limit_rate(code))
    out["next_date"] = df["日期"].shift(-1)
    out["next_open_return"] = (next_next_open / next_open - 1) * 100
    out["next_close_return"] = (next_close / next_open - 1) * 100
    out["next_touch_limit_up"] = (next_high >= limit_price * 0.995).astype(int)
    out["next_close_limit_up"] = (next_close >= limit_price * 0.995).astype(int)

    return out.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURE_COLUMNS + ["next_open_return"])


def build_dataset(data_dir: Path | None = None, limit: int | None = None) -> pd.DataFrame:
    data_dir = data_dir or resolve_data_dir()
    frames = []
    files = history_files(data_dir)
    if limit:
        files = files[:limit]
    for path in files:
        df = _read_history(path)
        features = _make_features_for_stock(df)
        if not features.empty:
            frames.append(features)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values(["date", "code"]).reset_index(drop=True)


def rule_score(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(0.0, index=df.index)

    chg = df["change_pct"]
    score += np.select(
        [
            chg.between(3.5, 7.0),
            chg.between(2.0, 3.5, inclusive="left"),
            chg.between(7.0, 9.0, inclusive="right"),
            chg.between(0.5, 2.0, inclusive="left"),
            chg.between(-2.0, 0.5, inclusive="neither"),
        ],
        [25, 18, 20, 12, 5],
        default=0,
    )

    vr = df["volume_ratio_5"]
    score += np.select([vr >= 2.5, vr >= 2.0, vr >= 1.5, vr >= 1.2, vr >= 1.0], [20, 17, 13, 8, 4], default=0)

    turn = df["turnover_rate"]
    score += np.select([turn.between(5, 15), turn.between(3, 5, inclusive="left"), turn.between(15, 25, inclusive="right"), turn.between(2, 3, inclusive="left")], [15, 12, 10, 6], default=0)

    score += np.select([vr >= 3.0, vr >= 2.0, vr >= 1.5, vr >= 1.0], [15, 12, 8, 3], default=0)

    tr = df["turnover_ratio_5"]
    score += np.select([tr >= 1.5, tr >= 1.2, tr >= 1.0], [10, 7, 3], default=0)

    ret5 = df["return_5d"]
    score += np.select([ret5.between(3, 12), ret5.between(0, 3, inclusive="left"), ret5.between(-3, 0, inclusive="neither")], [10, 8, 5], default=0)
    score += np.where(df["ma_bull_aligned"] == 1, 5, 0)
    pos = df["price_position_10d"]
    score += np.select([pos.between(60, 95), pos.between(40, 60, inclusive="left")], [5, 3], default=0)
    return score


def apply_universe_filters(df: pd.DataFrame, cfg: BacktestConfig) -> pd.DataFrame:
    out = df.copy()
    for prefix in cfg.exclude_prefixes:
        out = out[~out["code"].astype(str).str.startswith(prefix)]
    out = out[(out["close"] >= cfg.min_price) & (out["close"] <= cfg.max_price)]
    out = out[out["turnover_rate"] >= cfg.min_turnover]
    out = out[(out["change_pct"] < 9.8) & (out["change_pct"] > -4.0)]
    return out


def backtest_rules(dataset: pd.DataFrame, cfg: BacktestConfig | None = None) -> tuple[pd.DataFrame, dict]:
    cfg = cfg or BacktestConfig()
    filtered = apply_universe_filters(dataset, cfg)
    if filtered.empty:
        return pd.DataFrame(), {}

    filtered = filtered.assign(score=rule_score(filtered))
    picks = (
        filtered.sort_values(["date", "score"], ascending=[True, False])
        .groupby("date", group_keys=False)
        .head(cfg.top_n)
        .copy()
    )

    daily = picks.groupby("date").agg(
        picks=("code", "count"),
        avg_score=("score", "mean"),
        touch_limit_rate=("next_touch_limit_up", "mean"),
        close_limit_rate=("next_close_limit_up", "mean"),
        avg_next_open_return=("next_open_return", "mean"),
        median_next_open_return=("next_open_return", "median"),
    )
    daily["equity"] = (1 + daily["avg_next_open_return"] / 100).cumprod()
    running_max = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] / running_max - 1

    metrics = {
        "days": int(len(daily)),
        "picks": int(len(picks)),
        "avg_daily_return_pct": float(daily["avg_next_open_return"].mean()),
        "median_daily_return_pct": float(daily["median_next_open_return"].median()),
        "touch_limit_rate_pct": float(picks["next_touch_limit_up"].mean() * 100),
        "close_limit_rate_pct": float(picks["next_close_limit_up"].mean() * 100),
        "win_rate_pct": float((picks["next_open_return"] > 0).mean() * 100),
        "total_return_pct": float((daily["equity"].iloc[-1] - 1) * 100),
        "max_drawdown_pct": float(daily["drawdown"].min() * 100),
    }
    return picks, metrics


def train_lightgbm(dataset: pd.DataFrame, output_dir: Path | None = None) -> dict:
    try:
        import lightgbm as lgb
        from sklearn.metrics import average_precision_score, roc_auc_score
        lightgbm_error = None
    except Exception as exc:
        lgb = None
        lightgbm_error = exc
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.metrics import average_precision_score, roc_auc_score

    output_dir = output_dir or MODEL_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    data = dataset.dropna(subset=FEATURE_COLUMNS + ["next_close_limit_up"]).sort_values("date")
    if len(data) < 200:
        raise RuntimeError(f"样本太少，当前只有 {len(data)} 行，建议先积累更多历史数据。")

    split_date = data["date"].quantile(0.8)
    train = data[data["date"] <= split_date]
    valid = data[data["date"] > split_date]
    y_train = train["next_close_limit_up"]
    y_valid = valid["next_close_limit_up"]

    pos = max(int(y_train.sum()), 1)
    neg = max(int(len(y_train) - y_train.sum()), 1)
    if lgb is not None:
        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=300,
            learning_rate=0.03,
            num_leaves=31,
            subsample=0.85,
            colsample_bytree=0.85,
            class_weight={0: 1, 1: neg / pos},
            random_state=42,
        )
        model.fit(train[FEATURE_COLUMNS], y_train, eval_set=[(valid[FEATURE_COLUMNS], y_valid)], eval_metric="auc")
        model_type = "lightgbm"
    else:
        model = HistGradientBoostingClassifier(
            learning_rate=0.04,
            max_iter=250,
            max_leaf_nodes=31,
            l2_regularization=0.1,
            random_state=42,
        )
        sample_weight = y_train.map({0: 1.0, 1: neg / pos}).astype(float)
        model.fit(train[FEATURE_COLUMNS], y_train, sample_weight=sample_weight)
        model_type = "sklearn_hist_gradient_boosting"

    prob = model.predict_proba(valid[FEATURE_COLUMNS])[:, 1]
    metrics = {
        "train_rows": int(len(train)),
        "valid_rows": int(len(valid)),
        "model_type": model_type,
        "positive_rate_pct": float(data["next_close_limit_up"].mean() * 100),
        "valid_auc": float(roc_auc_score(y_valid, prob)) if y_valid.nunique() > 1 else None,
        "valid_average_precision": float(average_precision_score(y_valid, prob)),
    }
    if lightgbm_error is not None:
        metrics["lightgbm_fallback_reason"] = str(lightgbm_error).splitlines()[0]

    if lgb is not None:
        model.booster_.save_model(str(output_dir / "limit_up_lgbm.txt"))
    else:
        with open(output_dir / "limit_up_sklearn.pkl", "wb") as f:
            pickle.dump(model, f)
    (output_dir / "features.json").write_text(json.dumps(FEATURE_COLUMNS, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


def save_backtest_report(picks: pd.DataFrame, metrics: dict, output_dir: Path | None = None) -> tuple[Path, Path]:
    output_dir = output_dir or REPORT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    picks_path = output_dir / "backtest_picks.csv"
    metrics_path = output_dir / "backtest_metrics.json"
    picks.to_csv(picks_path, index=False, encoding="utf-8-sig")
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return picks_path, metrics_path


def rank_latest_candidates(dataset: pd.DataFrame, top_n: int = 20, model_dir: Path | None = None) -> pd.DataFrame:
    """Rank the latest daily-bar candidates with rule score and optional ML probability."""
    model_dir = model_dir or MODEL_DIR
    latest_date = dataset["date"].max()
    latest = dataset[dataset["date"] == latest_date].copy()
    latest = apply_universe_filters(latest, BacktestConfig(top_n=top_n))
    latest["rule_score"] = rule_score(latest)

    model_path = model_dir / "limit_up_sklearn.pkl"
    if model_path.exists():
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        latest["ml_raw_score"] = model.predict_proba(latest[FEATURE_COLUMNS])[:, 1]
        latest["ml_rank_score"] = latest["ml_raw_score"].rank(pct=True)
    else:
        latest["ml_raw_score"] = np.nan
        latest["ml_rank_score"] = np.nan

    sort_cols = ["ml_rank_score", "rule_score"] if latest["ml_rank_score"].notna().any() else ["rule_score"]
    return latest.sort_values(sort_cols, ascending=False).head(top_n).reset_index(drop=True)
