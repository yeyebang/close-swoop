#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper trading ledger and adaptive scoring.

The ledger makes each scan auditable:
scan result -> paper signal -> next scan settlement -> adaptive score.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))


PAPER_DIR = Path(__file__).resolve().parent / "paper"
LEDGER_PATH = PAPER_DIR / "paper_trades.csv"

FEATURE_COLUMNS = [
    "scan_change_pct",
    "scan_volume_ratio",
    "scan_turnover_rate",
    "scan_amplitude",
    "scan_vol_vs_ma5",
    "scan_ma5_dev",
    "rule_score",
]

TEXT_COLUMNS = {
    "signal_date", "scan_time", "code", "name", "exit_plan", "planned_exit_date",
    "status", "enrich_notes", "data_source", "market_captured_at", "settle_time",
}


def normalize_code(code) -> str:
    text = "".join(ch for ch in str(code).strip() if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


def load_ledger() -> pd.DataFrame:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    if not LEDGER_PATH.exists():
        return _empty_ledger()
    df = pd.read_csv(LEDGER_PATH, dtype={"code": str})
    if df.empty:
        return _empty_ledger()
    df = _ensure_ledger_schema(df)
    df["code"] = df["code"].map(normalize_code)
    for col in TEXT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("object")
    return df


def save_ledger(df: pd.DataFrame) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(LEDGER_PATH, index=False, encoding="utf-8-sig")


def _empty_ledger() -> pd.DataFrame:
    cols = [
        "signal_date", "scan_time", "rank", "code", "name", "scan_price",
        "exit_plan", "planned_exit_date",
        "scan_change_pct", "scan_volume_ratio", "scan_turnover_rate",
        "scan_amplitude", "scan_vol_vs_ma5", "scan_ma5_dev", "rule_score",
        "rule_score_pct", "adaptive_score", "final_score", "status",
        "enrich_score", "enrich_notes", "data_source", "market_captured_at",
        "settle_time", "same_day_close", "same_day_return_pct",
        "hit_limit_up", "next_exit_price", "next_return_pct", "success",
    ]
    return pd.DataFrame(columns=cols)


def _ensure_ledger_schema(df: pd.DataFrame) -> pd.DataFrame:
    template = _empty_ledger()
    out = df.copy()
    for col in template.columns:
        if col not in out.columns:
            out[col] = "" if col in TEXT_COLUMNS else np.nan
    return out


def append_signals(df_top: pd.DataFrame, scan_time: pd.Timestamp | None = None) -> int:
    """Append today's top picks to the paper ledger, skipping duplicate date+code."""
    if df_top.empty:
        return 0

    scan_time = scan_time or pd.Timestamp.now()
    signal_date = scan_time.strftime("%Y-%m-%d")
    ledger = load_ledger()
    existing = set()
    if not ledger.empty:
        existing = set(zip(ledger["signal_date"].astype(str), ledger["code"].astype(str)))

    rows = []
    for rank, (_, row) in enumerate(df_top.iterrows(), start=1):
        code = normalize_code(row["代码"])
        if (signal_date, code) in existing:
            continue
        rows.append({
            "signal_date": signal_date,
            "scan_time": scan_time.strftime("%Y-%m-%d %H:%M:%S"),
            "rank": rank,
            "code": code,
            "name": row.get("名称", ""),
            "scan_price": row.get("买入价", row.get("最新价", np.nan)),
            "exit_plan": "next_open",
            "planned_exit_date": "",
            "scan_change_pct": row.get("涨跌幅%", np.nan),
            "scan_volume_ratio": row.get("量比", np.nan),
            "scan_turnover_rate": row.get("换手率%", np.nan),
            "scan_amplitude": row.get("振幅%", np.nan),
            "scan_vol_vs_ma5": row.get("vol_vs_ma5", np.nan),
            "scan_ma5_dev": row.get("ma5_dev%", np.nan),
            "rule_score": row.get("score", np.nan),
            "rule_score_pct": row.get("score_pct", np.nan),
            "adaptive_score": row.get("adaptive_score", np.nan),
            "final_score": row.get("final_score", row.get("score", np.nan)),
            "enrich_score": row.get("enrich_score", np.nan),
            "enrich_notes": row.get("enrich_notes", ""),
            "data_source": row.get("数据源", ""),
            "market_captured_at": row.get("行情采集时间", ""),
            "status": "open",
            "settle_time": "",
            "same_day_close": np.nan,
            "same_day_return_pct": np.nan,
            "hit_limit_up": np.nan,
            "next_exit_price": np.nan,
            "next_return_pct": np.nan,
            "success": np.nan,
        })

    if not rows:
        return 0
    new_rows = pd.DataFrame(rows)
    ledger = new_rows if ledger.empty else pd.concat([ledger, new_rows], ignore_index=True)
    save_ledger(ledger)
    return len(rows)


def settle_pending(realtime_df: pd.DataFrame, data_dir: Path, success_return_pct: float = 1.0) -> dict:
    """Settle open paper trades from previous scan days.

    Signals represent an afternoon buy and a next-session-open exit. Settlement
    therefore uses the first available trading day's open after the signal date.
    If that daily bar is not available yet, the signal remains open.
    """
    ledger = load_ledger()
    if ledger.empty or "status" not in ledger.columns:
        return {"settled": 0, "open": 0, "path": str(LEDGER_PATH)}

    now = pd.Timestamp.now()
    today = now.strftime("%Y-%m-%d")
    open_mask = (ledger["status"] == "open") & (ledger["signal_date"].astype(str) < today)
    if not open_mask.any():
        return {"settled": 0, "open": int((ledger["status"] == "open").sum()), "path": str(LEDGER_PATH)}

    settled = 0
    waiting = 0
    for idx in ledger[open_mask].index:
        code = normalize_code(ledger.at[idx, "code"])
        scan_price = pd.to_numeric(ledger.at[idx, "scan_price"], errors="coerce")
        if not np.isfinite(scan_price) or scan_price <= 0:
            continue

        hist = _read_history_for_code(data_dir, code)
        signal_date = str(ledger.at[idx, "signal_date"])
        same_day_close = np.nan
        same_day_return = np.nan
        hit_limit = False
        if not hist.empty:
            day_row = hist[hist["日期"].astype(str) == signal_date]
            if not day_row.empty:
                same_day_close = float(day_row.iloc[-1]["收盘"])
                same_day_return = (same_day_close / scan_price - 1) * 100
                hit_limit = bool(float(day_row.iloc[-1].get("涨跌幅", 0)) >= _limit_threshold(code))

        exit_price = np.nan
        exit_date = ""
        if not hist.empty:
            later = hist[hist["日期"].astype(str) > signal_date]
            if not later.empty:
                exit_row = later.iloc[0]
                exit_price = float(exit_row["开盘"])
                exit_date = str(exit_row["日期"])[:10]
        if not np.isfinite(exit_price):
            waiting += 1
            continue

        next_return = (exit_price / scan_price - 1) * 100 if np.isfinite(exit_price) else np.nan
        success = bool(hit_limit or (np.isfinite(next_return) and next_return >= success_return_pct))

        ledger.at[idx, "status"] = "settled"
        ledger.at[idx, "settle_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
        if "planned_exit_date" in ledger.columns:
            ledger.at[idx, "planned_exit_date"] = exit_date
        ledger.at[idx, "same_day_close"] = same_day_close
        ledger.at[idx, "same_day_return_pct"] = same_day_return
        ledger.at[idx, "hit_limit_up"] = int(hit_limit)
        ledger.at[idx, "next_exit_price"] = exit_price
        ledger.at[idx, "next_return_pct"] = next_return
        ledger.at[idx, "success"] = int(success)
        settled += 1

    save_ledger(ledger)
    return {
        "settled": settled,
        "open": int((ledger["status"] == "open").sum()),
        "waiting_for_next_open": waiting,
        "path": str(LEDGER_PATH),
    }


def apply_adaptive_scores(df_results: pd.DataFrame, min_samples: int = 30) -> tuple[pd.DataFrame, dict]:
    """Blend rule score with a model trained from settled paper outcomes."""
    out = df_results.copy()
    out["adaptive_score"] = np.nan
    out["final_score"] = out["score"].astype(float)

    ledger = load_ledger()
    settled = ledger[ledger.get("status", "") == "settled"].copy() if not ledger.empty else pd.DataFrame()
    settled = settled.dropna(subset=FEATURE_COLUMNS + ["success"]) if not settled.empty else settled
    info = {"adaptive_enabled": False, "settled_samples": int(len(settled)), "path": str(LEDGER_PATH)}
    if len(settled) < min_samples or settled["success"].nunique() < 2:
        return out, info

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        info["reason"] = f"sklearn unavailable: {exc}"
        return out, info

    x_train = settled[FEATURE_COLUMNS].astype(float)
    y_train = settled["success"].astype(int)
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
    )
    model.fit(x_train, y_train)

    x_now = _result_features(out)
    raw_prob = model.predict_proba(x_now[FEATURE_COLUMNS])[:, 1]
    out["adaptive_score"] = pd.Series(raw_prob).rank(pct=True).to_numpy() * 105
    out["final_score"] = out["score"].astype(float) * 0.75 + out["adaptive_score"] * 0.25
    info.update({
        "adaptive_enabled": True,
        "positive_samples": int(y_train.sum()),
        "negative_samples": int((1 - y_train).sum()),
    })
    return out, info


def paper_report() -> dict:
    ledger = load_ledger()
    if ledger.empty:
        return {"rows": 0, "path": str(LEDGER_PATH)}
    settled = ledger[ledger["status"] == "settled"].copy()
    if settled.empty:
        return {"rows": int(len(ledger)), "settled": 0, "open": int((ledger["status"] == "open").sum()), "path": str(LEDGER_PATH)}

    settled["next_return_pct"] = pd.to_numeric(settled["next_return_pct"], errors="coerce")
    settled["hit_limit_up"] = pd.to_numeric(settled["hit_limit_up"], errors="coerce").fillna(0)
    settled["success"] = pd.to_numeric(settled["success"], errors="coerce").fillna(0)
    top5 = settled[pd.to_numeric(settled["rank"], errors="coerce") <= 5]
    return {
        "rows": int(len(ledger)),
        "settled": int(len(settled)),
        "open": int((ledger["status"] == "open").sum()),
        "hit_limit_rate_pct": float(settled["hit_limit_up"].mean() * 100),
        "success_rate_pct": float(settled["success"].mean() * 100),
        "avg_next_return_pct": float(settled["next_return_pct"].mean()),
        "median_next_return_pct": float(settled["next_return_pct"].median()),
        "top5_success_rate_pct": float(top5["success"].mean() * 100) if not top5.empty else np.nan,
        "path": str(LEDGER_PATH),
    }


def _result_features(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "scan_change_pct": pd.to_numeric(df["涨跌幅%"], errors="coerce"),
        "scan_volume_ratio": pd.to_numeric(df["量比"], errors="coerce"),
        "scan_turnover_rate": pd.to_numeric(df["换手率%"], errors="coerce"),
        "scan_amplitude": pd.to_numeric(df["振幅%"], errors="coerce"),
        "scan_vol_vs_ma5": pd.to_numeric(df["vol_vs_ma5"], errors="coerce"),
        "scan_ma5_dev": pd.to_numeric(df["ma5_dev%"], errors="coerce"),
        "rule_score": pd.to_numeric(df["score"], errors="coerce"),
    }).fillna(0)


def _read_history_for_code(data_dir: Path, code: str) -> pd.DataFrame:
    path = data_dir / f"history_{normalize_code(code)}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype={"股票代码": str})
    if "日期" not in df.columns:
        return pd.DataFrame()
    return df.sort_values("日期").reset_index(drop=True)


def _limit_threshold(code: str) -> float:
    if code.startswith(("300", "688")):
        return 19.5
    if code.startswith(("8", "9")):
        return 29.5
    return 9.8
