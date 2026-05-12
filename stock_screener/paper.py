#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper trading ledger and adaptive scoring.

The ledger makes each scan auditable:
scan result -> paper signal -> next scan settlement -> adaptive score.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

os.environ.setdefault("LOKY_MAX_CPU_COUNT", str(os.cpu_count() or 1))


PAPER_DIR = Path(__file__).resolve().parent / "paper"
LEDGER_PATH = PAPER_DIR / "paper_trades.csv"
MODEL_DIR = Path(__file__).resolve().parent / "models"
V3_REVIEW_REPORT_PATH = MODEL_DIR / "paper_v3_review_model.json"

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
        "rule_score_pct", "adaptive_score", "v3_model_score", "v3_all_score",
        "v3_recent_score", "v3_adjustment", "v3_risk_penalty", "v3_notes",
        "final_score", "status",
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
            "v3_model_score": row.get("v3_model_score", np.nan),
            "v3_all_score": row.get("v3_all_score", np.nan),
            "v3_recent_score": row.get("v3_recent_score", np.nan),
            "v3_adjustment": row.get("v3_adjustment", np.nan),
            "v3_risk_penalty": row.get("v3_risk_penalty", np.nan),
            "v3_notes": row.get("v3_notes", ""),
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


def apply_adaptive_scores(
    df_results: pd.DataFrame,
    min_samples: int = 30,
    recent_days: int = 60,
    recent_weight: float = 0.6,
    model_weight: float = 0.35,
) -> tuple[pd.DataFrame, dict]:
    """Apply the V3 review model trained from real settled paper outcomes."""
    out = df_results.copy()
    out["adaptive_score"] = np.nan
    out["v3_model_score"] = np.nan
    out["v3_all_score"] = np.nan
    out["v3_recent_score"] = np.nan
    out["v3_adjustment"] = 0.0
    out["v3_risk_penalty"] = 0.0
    out["v3_notes"] = ""
    out["final_score"] = out["score"].astype(float)

    review = build_v3_review_model(
        min_samples=min_samples,
        recent_days=recent_days,
        recent_weight=recent_weight,
    )
    info = {
        "adaptive_enabled": False,
        "v3_enabled": False,
        "settled_samples": int(review.get("settled_samples", 0)),
        "path": str(LEDGER_PATH),
        "review_model_path": str(V3_REVIEW_REPORT_PATH),
    }
    if not review.get("enabled"):
        info["reason"] = review.get("reason", "not enough settled samples")
        return out, info

    x_now = _result_features(out)
    all_score = _predict_with_model(review.get("all_model"), x_now)
    recent_score = _predict_with_model(review.get("recent_model"), x_now)
    blended = _blend_scores(all_score, recent_score, recent_weight=recent_weight)
    penalty, notes = _apply_risk_rules(x_now, review.get("risk_rules", []))

    baseline_score = float(review.get("baseline_success_rate", 0.5)) * 100
    model_component = blended * 1.05
    base = out["score"].astype(float)
    final = base * (1 - model_weight) + model_component * model_weight - penalty

    out["adaptive_score"] = model_component
    out["v3_model_score"] = blended
    out["v3_all_score"] = all_score
    out["v3_recent_score"] = recent_score
    out["v3_adjustment"] = final - base
    out["v3_risk_penalty"] = penalty
    out["v3_notes"] = notes
    out["final_score"] = final.clip(lower=0, upper=120)

    info.update({
        "adaptive_enabled": True,
        "v3_enabled": True,
        "positive_samples": int(review.get("positive_samples", 0)),
        "negative_samples": int(review.get("negative_samples", 0)),
        "recent_samples": int(review.get("recent_samples", 0)),
        "baseline_success_rate_pct": baseline_score,
        "risk_rule_count": len(review.get("risk_rules", [])),
    })
    return out, info


def build_v3_review_model(min_samples: int = 30, recent_days: int = 60, recent_weight: float = 0.6) -> dict:
    """Train the daily review model from settled real scan outcomes and persist diagnostics."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    ledger = load_ledger()
    settled = ledger[ledger.get("status", "") == "settled"].copy() if not ledger.empty else pd.DataFrame()
    settled = settled.dropna(subset=FEATURE_COLUMNS + ["success"]) if not settled.empty else settled
    settled = _coerce_training_frame(settled)

    report = _empty_v3_report(settled, min_samples=min_samples, recent_days=recent_days, recent_weight=recent_weight)
    if len(settled) < min_samples:
        report["reason"] = f"settled samples {len(settled)} < min_samples {min_samples}"
        _write_v3_report(report)
        return report
    if settled["success"].nunique() < 2:
        report["reason"] = "settled samples do not contain both success and failure"
        _write_v3_report(report)
        return report

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        report["reason"] = f"sklearn unavailable: {exc}"
        _write_v3_report(report)
        return report

    all_model = _fit_logistic_model(settled, make_pipeline, StandardScaler, LogisticRegression)
    recent = _recent_training_frame(settled, recent_days)
    recent_model = None
    if len(recent) >= min_samples and recent["success"].nunique() >= 2:
        recent_model = _fit_logistic_model(recent, make_pipeline, StandardScaler, LogisticRegression)

    risk_rules = _build_risk_rules(settled)
    feature_profile = _feature_profile(settled)
    report.update({
        "enabled": True,
        "reason": "",
        "positive_samples": int(settled["success"].sum()),
        "negative_samples": int((1 - settled["success"]).sum()),
        "recent_samples": int(len(recent)),
        "recent_positive_samples": int(recent["success"].sum()) if not recent.empty else 0,
        "recent_negative_samples": int((1 - recent["success"]).sum()) if not recent.empty else 0,
        "baseline_success_rate": float(settled["success"].mean()),
        "recent_success_rate": float(recent["success"].mean()) if not recent.empty else np.nan,
        "feature_profile": feature_profile,
        "risk_rules": risk_rules,
    })
    _write_v3_report(report)
    report["all_model"] = all_model
    report["recent_model"] = recent_model
    return report


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
        "v3_review_model": load_v3_review_report(),
        "path": str(LEDGER_PATH),
    }


def load_v3_review_report() -> dict:
    if not V3_REVIEW_REPORT_PATH.exists():
        return {}
    try:
        return json.loads(V3_REVIEW_REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


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


def _coerce_training_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in FEATURE_COLUMNS + ["success"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["signal_dt"] = pd.to_datetime(out.get("signal_date"), errors="coerce")
    return out.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURE_COLUMNS + ["success"])


def _recent_training_frame(settled: pd.DataFrame, recent_days: int) -> pd.DataFrame:
    if "signal_dt" not in settled.columns or settled["signal_dt"].dropna().empty:
        return settled.tail(max(30, recent_days))
    cutoff = settled["signal_dt"].max() - pd.Timedelta(days=recent_days)
    return settled[settled["signal_dt"] >= cutoff].copy()


def _fit_logistic_model(df: pd.DataFrame, make_pipeline, StandardScaler, LogisticRegression):
    model = make_pipeline(
        StandardScaler(),
        LogisticRegression(class_weight="balanced", max_iter=1000, random_state=42),
    )
    model.fit(df[FEATURE_COLUMNS].astype(float), df["success"].astype(int))
    return model


def _predict_with_model(model, x_now: pd.DataFrame) -> np.ndarray:
    if model is None:
        return np.full(len(x_now), np.nan)
    try:
        return model.predict_proba(x_now[FEATURE_COLUMNS].astype(float))[:, 1] * 100
    except Exception:
        return np.full(len(x_now), np.nan)


def _blend_scores(all_score: np.ndarray, recent_score: np.ndarray, recent_weight: float) -> np.ndarray:
    all_score = np.asarray(all_score, dtype=float)
    recent_score = np.asarray(recent_score, dtype=float)
    out = all_score.copy()
    both = np.isfinite(all_score) & np.isfinite(recent_score)
    out[both] = all_score[both] * (1 - recent_weight) + recent_score[both] * recent_weight
    only_recent = ~np.isfinite(all_score) & np.isfinite(recent_score)
    out[only_recent] = recent_score[only_recent]
    out[~np.isfinite(out)] = 50.0
    return out


def _empty_v3_report(settled: pd.DataFrame, min_samples: int, recent_days: int, recent_weight: float) -> dict:
    return {
        "version": "V3",
        "enabled": False,
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "settled_samples": int(len(settled)),
        "positive_samples": 0,
        "negative_samples": 0,
        "recent_days": int(recent_days),
        "recent_weight": float(recent_weight),
        "min_samples": int(min_samples),
        "baseline_success_rate": np.nan,
        "recent_success_rate": np.nan,
        "feature_profile": [],
        "risk_rules": [],
        "reason": "",
    }


def _write_v3_report(report: dict) -> None:
    serializable = {k: v for k, v in report.items() if k not in {"all_model", "recent_model"}}
    cleaned = _json_clean(serializable)
    V3_REVIEW_REPORT_PATH.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def _json_clean(value):
    if isinstance(value, dict):
        return {k: _json_clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    return value


def _feature_profile(settled: pd.DataFrame) -> list[dict]:
    success = settled[settled["success"] == 1]
    failure = settled[settled["success"] == 0]
    rows = []
    for col in FEATURE_COLUMNS:
        s_mean = float(success[col].mean()) if not success.empty else np.nan
        f_mean = float(failure[col].mean()) if not failure.empty else np.nan
        rows.append({
            "feature": col,
            "success_mean": s_mean,
            "failure_mean": f_mean,
            "delta_success_minus_failure": s_mean - f_mean if np.isfinite(s_mean) and np.isfinite(f_mean) else np.nan,
            "success_median": float(success[col].median()) if not success.empty else np.nan,
            "failure_median": float(failure[col].median()) if not failure.empty else np.nan,
        })
    return sorted(rows, key=lambda r: abs(r.get("delta_success_minus_failure") or 0), reverse=True)


def _build_risk_rules(settled: pd.DataFrame, min_bin_samples: int = 10) -> list[dict]:
    baseline_success = float(settled["success"].mean()) if not settled.empty else 0.0
    rules = []
    for col in FEATURE_COLUMNS:
        values = settled[col].astype(float)
        try:
            bins = pd.qcut(values.rank(method="first"), q=3, labels=False, duplicates="drop")
        except Exception:
            continue
        for bucket in sorted(pd.Series(bins).dropna().unique()):
            mask = bins == bucket
            part = settled[mask]
            if len(part) < min_bin_samples:
                continue
            success_rate = float(part["success"].mean())
            if success_rate > baseline_success - 0.12:
                continue
            low = float(part[col].min())
            high = float(part[col].max())
            penalty = min(12.0, max(3.0, (baseline_success - success_rate) * 35))
            direction = "low" if bucket == 0 else "high" if bucket == 2 else "mid"
            rules.append({
                "feature": col,
                "low": low,
                "high": high,
                "bucket": direction,
                "samples": int(len(part)),
                "success_rate": success_rate,
                "baseline_success_rate": baseline_success,
                "penalty": float(penalty),
                "note": f"{_feature_label(col)}处于{_bucket_label(direction)}区间，历史成功率 {success_rate * 100:.1f}%",
            })
    return sorted(rules, key=lambda r: (r["baseline_success_rate"] - r["success_rate"], r["samples"]), reverse=True)[:12]


def _apply_risk_rules(x_now: pd.DataFrame, rules: list[dict]) -> tuple[np.ndarray, list[str]]:
    penalties = np.zeros(len(x_now), dtype=float)
    notes = [[] for _ in range(len(x_now))]
    for rule in rules:
        col = rule.get("feature")
        if col not in x_now.columns:
            continue
        values = pd.to_numeric(x_now[col], errors="coerce")
        mask = (values >= float(rule.get("low", -np.inf))) & (values <= float(rule.get("high", np.inf)))
        for idx in np.where(mask.to_numpy())[0]:
            penalties[idx] += float(rule.get("penalty", 0))
            if len(notes[idx]) < 2:
                notes[idx].append(str(rule.get("note", "")))
    return np.minimum(penalties, 18.0), ["；".join(items) for items in notes]


def _feature_label(col: str) -> str:
    labels = {
        "scan_change_pct": "扫描涨跌幅",
        "scan_volume_ratio": "量比",
        "scan_turnover_rate": "换手率",
        "scan_amplitude": "振幅",
        "scan_vol_vs_ma5": "成交量/5日均量",
        "scan_ma5_dev": "MA5偏离",
        "rule_score": "规则分",
    }
    return labels.get(col, col)


def _bucket_label(bucket: str) -> str:
    return {"low": "偏低", "mid": "中位", "high": "偏高"}.get(bucket, bucket)


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
