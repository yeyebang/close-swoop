#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V4 tail-trading workflow.

This module keeps the new 14:00 pool -> 14:20 tracking -> next-day
verification flow separate from the legacy one-shot scanner. Storage is CSV so
the UI and later research jobs can inspect every decision without a database.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from stock_screener.main import (
    BASE_DIR,
    DataFetcher,
    calc_features,
    calc_score,
    load_config,
    normalize_code,
)


V4_DIR = BASE_DIR / "reports" / "v4"
BATCHES_PATH = V4_DIR / "scan_batches.csv"
CANDIDATES_PATH = V4_DIR / "candidates.csv"
EXCLUDED_PATH = V4_DIR / "excluded.csv"
SNAPSHOTS_PATH = V4_DIR / "tracking_snapshots.csv"
VERIFICATIONS_PATH = V4_DIR / "verifications.csv"
FEEDBACK_PATH = V4_DIR / "model_feedback.json"


DISPLAY_COLUMNS: dict[str, str] = {
    "batch_id": "批次编号",
    "trade_date": "交易日期",
    "scan_time": "扫描时间",
    "snapshot_time": "快照时间",
    "verify_time": "验证时间",
    "symbol": "股票代码",
    "name": "股票名称",
    "status": "当前状态",
    "risk_status": "风控状态",
    "risk_reasons": "风控原因",
    "exclude_reason": "剔除原因",
    "exclude_stage": "剔除阶段",
    "include_reasons": "入选原因",
    "decision_reason": "决策解释",
    "current_price": "当前价",
    "buy_price": "买入参考价",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "pre_close": "昨收价",
    "limit_up_price": "涨停价",
    "limit_down_price": "跌停价",
    "change_pct": "当日涨幅%",
    "turnover_rate": "换手率%",
    "volume": "成交量",
    "amount": "成交额",
    "amount_yi": "成交额(亿)",
    "volume_ratio": "量比",
    "amplitude": "振幅%",
    "limit_distance_pct": "距涨停%",
    "near_limit_up": "接近涨停",
    "five_day_return": "五日涨幅%",
    "ten_day_return": "十日涨幅%",
    "upper_shadow_ratio": "上影线比例",
    "close_position": "日内位置%",
    "initial_score": "初筛评分",
    "tracking_score": "跟踪评分",
    "final_score": "最终评分",
    "tail_return_pct": "跟踪涨幅%",
    "tail_amount_delta_yi": "跟踪成交额增量(亿)",
    "tail_volume_delta": "跟踪成交量增量",
    "tail_pullback_pct": "跟踪回撤%",
    "price_trend_tail": "价格趋势",
    "trend_status": "趋势状态",
    "sell_signal": "卖出信号",
    "next_open_price": "次日开盘价",
    "next_open_return": "次日开盘收益%",
    "next_30min_high": "次日30分钟最高价",
    "next_30min_return": "次日30分钟收益%",
    "next_30min_source": "30分钟数据源",
    "next_high_return": "次日最高收益%",
    "next_close_return": "次日收盘收益%",
    "trade_label": "是否达标",
    "failure_reason": "失败原因",
    "market_env": "市场环境",
    "total_scanned": "扫描股票数",
    "excluded_count": "剔除股票数",
    "candidate_count": "入池候选数",
    "final_count": "最终候选数",
}


def v4_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {
        "candidate_count": 50,
        "final_count": 10,
        "min_listing_days": 60,
        "min_price": 3.0,
        "max_price": 50.0,
        "min_amount": 100_000_000,
        "min_turnover_rate": 0.8,
        "max_consecutive_limit_up": 3,
        "max_5d_return": 30.0,
        "max_10d_return": 50.0,
        "max_upper_shadow_ratio": 0.45,
        "exclude_prefixes": ["300", "301", "688", "920"],
        "near_limit_threshold_pct": 5.0,
        "target_open_return_pct": 1.0,
        "target_30min_return_pct": 2.0,
        "verification_minute_period": "1",
        "verification_end_time": "10:00",
    }
    cfg = config or load_config()
    base.update(cfg.get("v4", {}))
    filters = cfg.get("filters", {})
    base["min_price"] = float(filters.get("min_price", base["min_price"]))
    base["max_price"] = float(filters.get("max_price", base["max_price"]))
    return base


def ensure_storage() -> None:
    V4_DIR.mkdir(parents=True, exist_ok=True)


def read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    dtype = {"symbol": str, "batch_id": str}
    try:
        df = pd.read_csv(path, dtype=dtype)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].map(normalize_code)
    return df


def append_table(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_storage()
    if not rows:
        return
    df = pd.DataFrame(rows)
    exists = path.exists()
    df.to_csv(path, mode="a", index=False, header=not exists, encoding="utf-8-sig")


def overwrite_table(path: Path, df: pd.DataFrame) -> None:
    ensure_storage()
    df.to_csv(path, index=False, encoding="utf-8-sig")


def latest_batch_id() -> str:
    batches = read_table(BATCHES_PATH)
    if batches.empty:
        return ""
    batches["_t"] = pd.to_datetime(batches["scan_time"], errors="coerce")
    latest = batches.sort_values("_t").iloc[-1]
    return str(latest["batch_id"])


def create_market_scan(config: dict[str, Any] | None = None, progress: Any | None = None) -> dict[str, Any]:
    """Create a 14:00-style market scan batch."""
    ensure_storage()
    config = config or load_config()
    cfg = v4_config(config)
    now = pd.Timestamp.now()
    batch_id = now.strftime("%Y%m%d_%H%M%S")
    trade_date = now.strftime("%Y-%m-%d")

    if progress:
        progress("V4扫描：正在获取全市场实时数据")
    fetcher = DataFetcher(config)
    realtime = fetcher.get_all_realtime()
    if realtime is None or realtime.empty:
        raise RuntimeError("无法获取实时行情，无法创建 4.0 批次")

    realtime = normalize_realtime(realtime)
    if progress:
        progress(f"V4扫描：获取到 {len(realtime)} 只股票，开始执行风控预筛")
    candidates: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    excluded_count = 0

    prefiltered = realtime.copy()
    prefiltered["_prefilter_score"] = (
        prefiltered["change_pct"].fillna(0).clip(lower=-2, upper=9) * 3
        + prefiltered["volume_ratio"].fillna(0).clip(upper=5) * 8
        + prefiltered["turnover_rate"].fillna(0).clip(upper=25)
        + (prefiltered["amount"].fillna(0) / 100_000_000).clip(upper=5) * 4
    )
    prefiltered = prefiltered.sort_values("_prefilter_score", ascending=False)
    max_fetch = int(config.get("max_history_fetch", 500))
    scan_pool = prefiltered.head(max_fetch)

    for idx, (_, stock) in enumerate(scan_pool.iterrows(), start=1):
        base_risk = base_risk_checks(stock, cfg)
        if base_risk["hard_excluded"]:
            excluded_count += 1
            excluded_rows.append(build_excluded_row(batch_id, trade_date, now, stock, "基础风控", base_risk["reasons"]))
            if progress and (idx == 1 or idx % 50 == 0 or idx == len(scan_pool)):
                progress(f"V4扫描：已处理 {idx}/{len(scan_pool)}，入池 {len(candidates)}，剔除 {excluded_count}")
            continue

        code = normalize_code(stock["code"])
        history = fetcher.get_history(code, int(config.get("backtrack_days", 60)))
        hist_risk = history_risk_checks(history, code, cfg)
        if hist_risk["hard_excluded"]:
            excluded_count += 1
            excluded_rows.append(build_excluded_row(batch_id, trade_date, now, stock, "历史风控", hist_risk["reasons"]))
            if progress and (idx == 1 or idx % 50 == 0 or idx == len(scan_pool)):
                progress(f"V4扫描：已处理 {idx}/{len(scan_pool)}，入池 {len(candidates)}，剔除 {excluded_count}")
            continue

        features = calc_features(stock, history)
        if features is None:
            excluded_count += 1
            excluded_rows.append(build_excluded_row(batch_id, trade_date, now, stock, "特征计算", ["历史特征不足"]))
            if progress and (idx == 1 or idx % 50 == 0 or idx == len(scan_pool)):
                progress(f"V4扫描：已处理 {idx}/{len(scan_pool)}，入池 {len(candidates)}，剔除 {excluded_count}")
            continue
        score, max_score = calc_score(features, stock)
        derived = derive_market_fields(stock, history, features, cfg)
        risk_reasons = base_risk["reasons"] + hist_risk["reasons"] + derived.pop("risk_reasons", [])
        risk_penalty = risk_penalty_from_reasons(risk_reasons)
        initial_score = round(max(0.0, min(100.0, score / max(max_score, 1) * 100 - risk_penalty)), 2)

        candidates.append({
            "batch_id": batch_id,
            "trade_date": trade_date,
            "scan_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": code,
            "name": stock.get("name", ""),
            "status": "初筛",
            "risk_status": "降权" if risk_reasons else "通过",
            "risk_reasons": "；".join(risk_reasons),
            "include_reasons": build_include_reasons(stock, derived),
            "current_price": number(stock.get("last_price")),
            "buy_price": number(stock.get("last_price")),
            "open": number(stock.get("open_price")),
            "high": number(stock.get("high")),
            "low": number(stock.get("low")),
            "pre_close": number(stock.get("pre_close")),
            "volume": number(stock.get("volume")),
            "amount": number(stock.get("amount")),
            "amount_yi": round(number(stock.get("amount")) / 100_000_000, 3),
            "turnover_rate": number(stock.get("turnover_rate")),
            "volume_ratio": number(stock.get("volume_ratio")),
            "amplitude": number(stock.get("amplitude")),
            "change_pct": number(stock.get("change_pct")),
            "initial_score": initial_score,
            "tracking_score": np.nan,
            "final_score": initial_score,
            **derived,
        })
        if progress and (idx == 1 or idx % 50 == 0 or idx == len(scan_pool)):
            progress(f"V4扫描：已处理 {idx}/{len(scan_pool)}，入池 {len(candidates)}，剔除 {excluded_count}")

    candidates_df = pd.DataFrame(candidates)
    if not candidates_df.empty:
        if progress:
            progress("V4扫描：候选池评分排序中")
        candidates_df = candidates_df.sort_values("initial_score", ascending=False).head(int(cfg["candidate_count"]))
        candidates = candidates_df.to_dict(orient="records")

    append_table(CANDIDATES_PATH, candidates)
    append_table(EXCLUDED_PATH, excluded_rows)
    batch_row = {
        "batch_id": batch_id,
        "trade_date": trade_date,
        "scan_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_env": infer_market_env(realtime),
        "total_scanned": int(len(realtime)),
        "excluded_count": int(excluded_count),
        "candidate_count": int(len(candidates)),
        "final_count": 0,
        "status": "已建池",
    }
    append_table(BATCHES_PATH, [batch_row])
    _save_feedback()
    if progress:
        progress(f"V4扫描完成：入池 {len(candidates)} 只，风控剔除 {excluded_count} 只")
    return {
        "batch": batch_row,
        "candidates": to_display_records(pd.DataFrame(candidates)),
        "excluded": to_display_records(pd.DataFrame(excluded_rows).head(100)),
        "message": f"已创建 4.0 候选池：{len(candidates)} 只",
    }


def track_current_batch(batch_id: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_storage()
    config = config or load_config()
    cfg = v4_config(config)
    batch_id = batch_id or latest_batch_id()
    if not batch_id:
        raise RuntimeError("暂无 4.0 扫描批次，请先扫描大盘")

    candidates = read_table(CANDIDATES_PATH)
    pool = candidates[candidates["batch_id"] == batch_id].copy() if not candidates.empty else pd.DataFrame()
    pool = pool[~pool["status"].isin(["淘汰", "已验证"])] if not pool.empty else pool
    if pool.empty:
        raise RuntimeError("当前批次没有可跟踪候选股")

    fetcher = DataFetcher(config)
    realtime = normalize_realtime(fetcher.get_all_realtime())
    if realtime.empty:
        raise RuntimeError("无法获取实时行情，跟踪扫描失败")
    realtime_map = realtime.set_index("code")
    now = pd.Timestamp.now()
    snapshot_rows: list[dict[str, Any]] = []
    updated = candidates.copy()

    for idx, row in pool.iterrows():
        code = normalize_code(row["symbol"])
        if code not in realtime_map.index:
            continue
        rt = realtime_map.loc[code]
        if isinstance(rt, pd.DataFrame):
            rt = rt.iloc[0]
        metrics = build_tracking_metrics(row, rt, cfg)
        snapshot = {
            "batch_id": batch_id,
            "trade_date": row.get("trade_date", now.strftime("%Y-%m-%d")),
            "snapshot_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": code,
            "name": row.get("name", ""),
            **metrics,
        }
        snapshot_rows.append(snapshot)

        mask = (updated["batch_id"].astype(str) == batch_id) & (updated["symbol"].astype(str).map(normalize_code) == code)
        for key in ["current_price", "change_pct", "volume", "amount", "amount_yi", "turnover_rate", "volume_ratio", "limit_distance_pct"]:
            if key in metrics:
                updated.loc[mask, key] = metrics[key]
        updated.loc[mask, "tracking_score"] = metrics["tracking_score"]
        updated.loc[mask, "final_score"] = metrics["final_score"]
        updated.loc[mask, "status"] = metrics["status"]
        updated.loc[mask, "risk_status"] = metrics["risk_status"]
        updated.loc[mask, "risk_reasons"] = merge_reason(row.get("risk_reasons", ""), metrics.get("risk_reasons", ""))

    append_table(SNAPSHOTS_PATH, snapshot_rows)
    mark_final_candidates(updated, batch_id, int(cfg["final_count"]))
    overwrite_table(CANDIDATES_PATH, updated)
    refresh_batch_counts(batch_id)
    _save_feedback()
    return {
        "batch_id": batch_id,
        "snapshots": to_display_records(pd.DataFrame(snapshot_rows)),
        "message": f"已跟踪 {len(snapshot_rows)} 只候选股",
    }


def verify_previous_candidates(batch_id: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_storage()
    config = config or load_config()
    cfg = v4_config(config)
    candidates = read_table(CANDIDATES_PATH)
    if candidates.empty:
        raise RuntimeError("暂无候选股可验证")

    batch_id = batch_id or latest_verifiable_batch(candidates)
    if not batch_id:
        raise RuntimeError("暂无可验证批次")

    existing = read_table(VERIFICATIONS_PATH)
    verified_keys = set()
    if not existing.empty:
        verified_keys = set(zip(existing["batch_id"].astype(str), existing["symbol"].astype(str)))

    pool = candidates[(candidates["batch_id"].astype(str) == batch_id) & (candidates["status"] == "最终候选")]
    if pool.empty:
        pool = candidates[candidates["batch_id"].astype(str) == batch_id].sort_values("final_score", ascending=False).head(int(cfg["final_count"]))

    fetcher = DataFetcher(config)
    rows: list[dict[str, Any]] = []
    waiting = 0
    for _, row in pool.iterrows():
        code = normalize_code(row["symbol"])
        if (batch_id, code) in verified_keys:
            continue
        hist = fetcher.get_history(code, int(config.get("backtrack_days", 60)))
        result = verify_one(row, hist, cfg, config)
        if not result.pop("_ready", True):
            waiting += 1
            continue
        rows.append(result)

    append_table(VERIFICATIONS_PATH, rows)
    if rows:
        updated = candidates.copy()
        for result in rows:
            mask = (updated["batch_id"].astype(str) == batch_id) & (updated["symbol"].astype(str).map(normalize_code) == result["symbol"])
            updated.loc[mask, "status"] = "已验证"
        overwrite_table(CANDIDATES_PATH, updated)
    _save_feedback()
    return {
        "batch_id": batch_id,
        "verifications": to_display_records(pd.DataFrame(rows)),
        "message": f"已验证 {len(rows)} 只候选股" + (f"，{waiting} 只等待次日行情" if waiting else ""),
    }


def get_v4_state(limit: int = 200) -> dict[str, Any]:
    ensure_storage()
    batches = read_table(BATCHES_PATH)
    candidates = read_table(CANDIDATES_PATH)
    excluded = read_table(EXCLUDED_PATH)
    snapshots = read_table(SNAPSHOTS_PATH)
    verifications = read_table(VERIFICATIONS_PATH)
    batch_id = latest_batch_id()
    active_candidates = candidates[candidates["batch_id"] == batch_id].copy() if batch_id and not candidates.empty else pd.DataFrame()
    latest_excluded = excluded[excluded["batch_id"] == batch_id].copy() if batch_id and not excluded.empty else pd.DataFrame()
    latest_snapshots = snapshots[snapshots["batch_id"] == batch_id].copy() if batch_id and not snapshots.empty else pd.DataFrame()
    latest_verifications = verifications[verifications["batch_id"] == batch_id].copy() if batch_id and not verifications.empty else pd.DataFrame()

    if not active_candidates.empty:
        active_candidates = active_candidates.sort_values("final_score", ascending=False).head(limit)
    if not latest_excluded.empty:
        latest_excluded = latest_excluded.head(limit)
    if not latest_snapshots.empty:
        latest_snapshots["_t"] = pd.to_datetime(latest_snapshots["snapshot_time"], errors="coerce")
        latest_snapshots = latest_snapshots.sort_values("_t", ascending=False).drop(columns=["_t"]).head(limit)
    if not latest_verifications.empty:
        latest_verifications = latest_verifications.head(limit)

    summary = build_summary(batches, candidates, snapshots, verifications, batch_id)
    feedback = load_feedback()
    return {
        "summary": summary,
        "latestBatch": table_row_to_display(batches[batches["batch_id"] == batch_id].tail(1)) if batch_id and not batches.empty else None,
        "candidates": to_display_records(active_candidates),
        "excluded": to_display_records(latest_excluded),
        "snapshots": to_display_records(latest_snapshots),
        "verifications": to_display_records(latest_verifications),
        "feedback": feedback,
    }


def normalize_realtime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "代码" in out.columns:
        out = out.rename(columns={
            "代码": "code", "名称": "name", "最新价": "last_price",
            "涨跌幅": "change_pct", "成交量": "volume", "成交额": "amount",
            "换手率": "turnover_rate", "量比": "volume_ratio", "振幅": "amplitude",
            "最高": "high", "最低": "low", "今开": "open_price", "昨收": "pre_close",
        })
    defaults = {
        "name": "", "last_price": 0, "change_pct": 0, "volume": 0, "amount": 0,
        "turnover_rate": 0, "volume_ratio": 0, "amplitude": 0, "high": 0,
        "low": 0, "open_price": 0, "pre_close": 0, "code": "",
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default
    out["code"] = out["code"].map(normalize_code)
    for col in [c for c in defaults if c not in ("name", "code")]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
    return out


def base_risk_checks(row: pd.Series, cfg: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    code = normalize_code(row.get("code"))
    name = str(row.get("name", ""))
    price = number(row.get("last_price"))
    amount = number(row.get("amount"))
    volume = number(row.get("volume"))
    change_pct = number(row.get("change_pct"))
    open_price = number(row.get("open_price"))
    high = number(row.get("high"))
    low = number(row.get("low"))

    if "ST" in name.upper() or "退" in name:
        reasons.append("ST或退市风险")
    if any(code.startswith(prefix) for prefix in cfg.get("exclude_prefixes", [])):
        reasons.append("当前策略暂不处理该板块")
    if price <= 0 or volume <= 0:
        reasons.append("停牌或无有效成交")
    if price < float(cfg["min_price"]):
        reasons.append(f"股价低于{cfg['min_price']}元")
    if price > float(cfg["max_price"]):
        reasons.append(f"股价高于{cfg['max_price']}元")
    if amount < float(cfg["min_amount"]):
        reasons.append(f"成交额低于{float(cfg['min_amount']) / 100_000_000:.1f}亿")
    if number(row.get("turnover_rate")) < float(cfg["min_turnover_rate"]):
        reasons.append(f"换手率低于{cfg['min_turnover_rate']}%")
    if is_one_word_board(open_price, high, low, price, change_pct, code):
        reasons.append("一字涨跌停")
    if change_pct < 0.5:
        reasons.append("当日涨幅不足")
    return {"hard_excluded": bool(reasons), "reasons": reasons}


def build_excluded_row(
    batch_id: str,
    trade_date: str,
    scan_time: pd.Timestamp,
    stock: pd.Series,
    stage: str,
    reasons: list[str],
) -> dict[str, Any]:
    price = number(stock.get("last_price"))
    pre_close = number(stock.get("pre_close")) or price
    limit_up = calc_limit_price(pre_close, normalize_code(stock.get("code"))) if pre_close > 0 else 0
    limit_distance = (limit_up - price) / price * 100 if price > 0 else np.nan
    return {
        "batch_id": batch_id,
        "trade_date": trade_date,
        "scan_time": scan_time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": normalize_code(stock.get("code")),
        "name": stock.get("name", ""),
        "status": "已剔除",
        "risk_status": "剔除",
        "exclude_stage": stage,
        "exclude_reason": "；".join(reasons),
        "current_price": round(price, 3),
        "change_pct": round(number(stock.get("change_pct")), 3),
        "turnover_rate": round(number(stock.get("turnover_rate")), 3),
        "volume_ratio": round(number(stock.get("volume_ratio")), 3),
        "amount_yi": round(number(stock.get("amount")) / 100_000_000, 3),
        "limit_distance_pct": round(limit_distance, 3) if np.isfinite(limit_distance) else np.nan,
    }


def history_risk_checks(hist: pd.DataFrame, code: str, cfg: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if hist is None or hist.empty:
        return {"hard_excluded": True, "reasons": ["缺少历史行情"]}
    if len(hist) < int(cfg["min_listing_days"]):
        reasons.append(f"上市/历史样本不足{cfg['min_listing_days']}日")
    df = hist.copy()
    for col in ["收盘", "涨跌幅", "最高", "最低", "开盘"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "涨跌幅" in df.columns:
        recent_limit = (df["涨跌幅"].tail(5) >= limit_threshold(code)).sum()
        if recent_limit >= int(cfg["max_consecutive_limit_up"]):
            reasons.append(f"近期连续涨停达到{recent_limit}天")
    if "收盘" in df.columns and len(df) >= 11:
        close = df["收盘"].astype(float)
        ret_10 = (close.iloc[-1] / close.iloc[-11] - 1) * 100
        if ret_10 > float(cfg["max_10d_return"]):
            reasons.append(f"近10日涨幅超过{cfg['max_10d_return']}%")
    return {"hard_excluded": bool(reasons), "reasons": reasons}


def derive_market_fields(row: pd.Series, hist: pd.DataFrame, features: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    price = number(row.get("last_price"))
    pre_close = number(row.get("pre_close")) or price
    high = number(row.get("high")) or price
    low = number(row.get("low")) or price
    close_position = (price - low) / (high - low) * 100 if high > low else 50
    upper_shadow_ratio = (high - price) / price if price > 0 else 0
    limit_up = calc_limit_price(pre_close, normalize_code(row.get("code")))
    limit_down = calc_limit_price(pre_close, normalize_code(row.get("code")), down=True)
    limit_distance = (limit_up - price) / price * 100 if price > 0 else 999
    five = number(features.get("return_5d"))
    ten = number(features.get("return_10d"))
    risk_reasons: list[str] = []
    if five > float(cfg["max_5d_return"]):
        risk_reasons.append(f"近5日涨幅超过{cfg['max_5d_return']}%")
    if upper_shadow_ratio > float(cfg["max_upper_shadow_ratio"]):
        risk_reasons.append("上影线过长")
    if number(row.get("amplitude")) > 12:
        risk_reasons.append("当日振幅偏大")
    return {
        "limit_up_price": round(limit_up, 3),
        "limit_down_price": round(limit_down, 3),
        "limit_distance_pct": round(limit_distance, 3),
        "near_limit_up": bool(limit_distance <= float(cfg["near_limit_threshold_pct"])),
        "five_day_return": round(five, 3),
        "ten_day_return": round(ten, 3),
        "upper_shadow_ratio": round(upper_shadow_ratio, 4),
        "close_position": round(close_position, 2),
        "risk_reasons": risk_reasons,
    }


def build_tracking_metrics(base: pd.Series, rt: pd.Series, cfg: dict[str, Any]) -> dict[str, Any]:
    start_price = number(base.get("buy_price")) or number(base.get("current_price"))
    current = number(rt.get("last_price"))
    start_amount = number(base.get("amount"))
    current_amount = number(rt.get("amount"))
    start_volume = number(base.get("volume"))
    current_volume = number(rt.get("volume"))
    high = max(number(rt.get("high")), current)
    low = number(rt.get("low")) or current
    tail_return = (current / start_price - 1) * 100 if start_price > 0 else 0
    amount_delta = max(0.0, current_amount - start_amount)
    volume_delta = max(0.0, current_volume - start_volume)
    limit_up = number(base.get("limit_up_price")) or calc_limit_price(number(rt.get("pre_close")), normalize_code(rt.get("code")))
    limit_distance = (limit_up - current) / current * 100 if current > 0 else 999
    pullback = (high - current) / current * 100 if current > 0 else 0
    tracking_score = score_tracking(tail_return, amount_delta, volume_delta, limit_distance, pullback)
    risk_reasons: list[str] = []
    status = "观察"
    trend_status = "横盘"
    if tail_return >= 1 and amount_delta >= 20_000_000 and pullback < 1.5:
        status = "增强"
        trend_status = "量价增强"
    if limit_distance <= float(cfg["near_limit_threshold_pct"]):
        status = "接近涨停"
    if tail_return < -0.5:
        status = "减弱"
        trend_status = "跟踪回落"
        risk_reasons.append("跟踪期价格回落")
    if amount_delta >= 30_000_000 and tail_return < 0.3:
        status = "放量滞涨"
        trend_status = "放量不涨"
        risk_reasons.append("放量滞涨")
    if pullback > 2.5:
        status = "冲高回落"
        trend_status = "冲高回落"
        risk_reasons.append("跟踪回撤偏大")
    risk_penalty = risk_penalty_from_reasons(risk_reasons)
    initial_score = number(base.get("initial_score"))
    final_score = max(0, min(100, initial_score * 0.4 + tracking_score * 0.6 - risk_penalty))
    if final_score < 45 or status in {"减弱", "放量滞涨", "冲高回落"}:
        status = "淘汰"
    return {
        "current_price": round(current, 3),
        "change_pct": round(number(rt.get("change_pct")), 3),
        "volume": round(current_volume, 3),
        "amount": round(current_amount, 3),
        "amount_yi": round(current_amount / 100_000_000, 3),
        "turnover_rate": round(number(rt.get("turnover_rate")), 3),
        "volume_ratio": round(number(rt.get("volume_ratio")), 3),
        "limit_distance_pct": round(limit_distance, 3),
        "tail_return_pct": round(tail_return, 3),
        "tail_amount_delta_yi": round(amount_delta / 100_000_000, 3),
        "tail_volume_delta": round(volume_delta, 3),
        "tail_pullback_pct": round(pullback, 3),
        "price_trend_tail": "向上" if tail_return > 0.5 else ("回落" if tail_return < -0.3 else "震荡"),
        "trend_status": trend_status,
        "status": status,
        "risk_status": "降权" if risk_reasons else "通过",
        "risk_reasons": "；".join(risk_reasons),
        "tracking_score": round(tracking_score, 2),
        "final_score": round(final_score, 2),
        "decision_reason": build_decision_reason(status, trend_status, tail_return, amount_delta, limit_distance, pullback, risk_reasons),
    }


def build_decision_reason(
    status: str,
    trend_status: str,
    tail_return: float,
    amount_delta: float,
    limit_distance: float,
    pullback: float,
    risk_reasons: list[str],
) -> str:
    reasons = []
    if status == "最终候选":
        reasons.append("综合评分进入最终候选")
    if tail_return >= 1:
        reasons.append(f"跟踪期上涨{tail_return:.2f}%")
    elif tail_return < -0.3:
        reasons.append(f"跟踪期回落{abs(tail_return):.2f}%")
    if amount_delta >= 50_000_000:
        reasons.append(f"跟踪成交额增加{amount_delta / 100_000_000:.2f}亿")
    elif amount_delta <= 0:
        reasons.append("跟踪期成交未继续放大")
    if 1 <= limit_distance <= 5:
        reasons.append(f"距涨停{limit_distance:.2f}%，仍有冲板空间")
    elif limit_distance > 8:
        reasons.append(f"距涨停{limit_distance:.2f}%，冲板距离偏远")
    if pullback > 2:
        reasons.append(f"冲高回撤{pullback:.2f}%")
    if trend_status not in {"横盘", ""}:
        reasons.append(trend_status)
    reasons.extend(risk_reasons)
    return "；".join(dict.fromkeys([r for r in reasons if r]))


def verify_one(
    candidate: pd.Series,
    hist: pd.DataFrame,
    cfg: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal_date = str(candidate.get("trade_date", ""))[:10]
    code = normalize_code(candidate.get("symbol"))
    buy_price = number(candidate.get("current_price")) or number(candidate.get("buy_price"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    base = {
        "batch_id": str(candidate.get("batch_id", "")),
        "trade_date": signal_date,
        "verify_time": now,
        "symbol": code,
        "name": candidate.get("name", ""),
        "buy_price": round(buy_price, 3),
    }
    if hist is None or hist.empty or buy_price <= 0:
        return {**base, "_ready": False, "failure_reason": "数据不足"}
    df = hist.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
    later = df[df["日期"].astype(str) > signal_date]
    if later.empty:
        return {**base, "_ready": False, "failure_reason": "尚无次日行情"}
    row = later.iloc[0]
    verify_date = str(row.get("日期"))[:10]
    open_price = number(row.get("开盘"))
    day_high_price = number(row.get("最高"))
    close_price = number(row.get("收盘"))
    minute_high_price, minute_source = get_next_30min_high(
        code,
        verify_date,
        fallback_high=day_high_price,
        cfg=cfg,
        config=config,
    )
    open_ret = (open_price / buy_price - 1) * 100 if buy_price > 0 else np.nan
    high_ret = (day_high_price / buy_price - 1) * 100 if buy_price > 0 else np.nan
    next_30min_ret = (minute_high_price / buy_price - 1) * 100 if buy_price > 0 and minute_high_price > 0 else np.nan
    close_ret = (close_price / buy_price - 1) * 100 if buy_price > 0 else np.nan
    label = int(open_ret >= float(cfg["target_open_return_pct"]) or next_30min_ret >= float(cfg["target_30min_return_pct"]))
    failure = "" if label else classify_failure(open_ret, next_30min_ret, close_ret)
    return {
        **base,
        "verify_date": verify_date,
        "next_open_price": round(open_price, 3),
        "next_open_return": round(open_ret, 3),
        "next_30min_high": round(minute_high_price, 3),
        "next_30min_return": round(next_30min_ret, 3),
        "next_30min_source": minute_source,
        "next_high_return": round(high_ret, 3),
        "next_close_return": round(close_ret, 3),
        "sell_signal": "次日早盘卖出",
        "trade_label": label,
        "failure_reason": failure,
    }


def get_next_30min_high(
    code: str,
    verify_date: str,
    fallback_high: float,
    cfg: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """Return 09:30-end_time high with daily high fallback.

    The strategy sells during the first half hour. When minute data is
    unavailable, returning daily high would overstate precision, so the source
    field explicitly marks the fallback as daily proxy.
    """
    minute_df = fetch_verify_minute_data(code, verify_date, cfg, config)
    if minute_df.empty:
        return fallback_high, "日K最高价代理"
    window = slice_morning_window(minute_df, verify_date, str(cfg.get("verification_end_time", "10:00")))
    if window.empty:
        return fallback_high, "日K最高价代理"
    high_col = first_existing_column(window, ["最高", "high"])
    if not high_col:
        return fallback_high, "日K最高价代理"
    high = pd.to_numeric(window[high_col], errors="coerce").dropna()
    if high.empty:
        return fallback_high, "日K最高价代理"
    return float(high.max()), "分钟K 09:30-10:00"


def fetch_verify_minute_data(
    code: str,
    verify_date: str,
    cfg: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    period = str(cfg.get("verification_minute_period", "1"))
    date_key = verify_date.replace("-", "")
    cache_dir = V4_DIR / "minute_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"minute_{normalize_code(code)}_{period}_{date_key}.csv"
    if cache_path.exists():
        try:
            return pd.read_csv(cache_path)
        except Exception:
            pass

    try:
        from stock_screener.data_fetcher import DataFetcher as MinuteFetcher

        minute_fetcher = MinuteFetcher(config or load_config())
        df = minute_fetcher.get_minute_history_by_date(code, verify_date, period=period)
    except Exception as exc:
        logger.warning(f"获取 {code} {verify_date} 分钟K失败: {exc}")
        df = pd.DataFrame()

    if df is not None and not df.empty:
        df.to_csv(cache_path, index=False, encoding="utf-8-sig")
        return df
    return pd.DataFrame()


def slice_morning_window(df: pd.DataFrame, verify_date: str, end_time: str) -> pd.DataFrame:
    time_col = first_existing_column(df, ["时间", "datetime", "日期"])
    if not time_col:
        return pd.DataFrame()
    out = df.copy()
    out[time_col] = pd.to_datetime(out[time_col], errors="coerce")
    out = out.dropna(subset=[time_col])
    if out.empty:
        return out
    start = pd.Timestamp(f"{verify_date} 09:30:00")
    end = pd.Timestamp(f"{verify_date} {end_time}:00" if len(end_time) == 5 else f"{verify_date} {end_time}")
    return out[(out[time_col] >= start) & (out[time_col] <= end)]


def first_existing_column(df: pd.DataFrame, columns: list[str]) -> str:
    for col in columns:
        if col in df.columns:
            return col
    return ""


def mark_final_candidates(df: pd.DataFrame, batch_id: str, final_count: int) -> None:
    mask = df["batch_id"].astype(str) == batch_id
    eligible = df[mask & ~df["status"].isin(["淘汰", "已验证"])].copy()
    if eligible.empty:
        return
    top_idx = eligible.sort_values("final_score", ascending=False).head(final_count).index
    df.loc[top_idx, "status"] = "最终候选"
    for idx in top_idx:
        existing = str(df.at[idx, "decision_reason"]) if "decision_reason" in df.columns else ""
        if existing and existing.lower() != "nan":
            df.at[idx, "decision_reason"] = "综合评分进入最终候选；" + existing
        else:
            df.at[idx, "decision_reason"] = "综合评分进入最终候选"


def refresh_batch_counts(batch_id: str) -> None:
    batches = read_table(BATCHES_PATH)
    candidates = read_table(CANDIDATES_PATH)
    if batches.empty or candidates.empty:
        return
    mask = batches["batch_id"].astype(str) == batch_id
    pool = candidates[candidates["batch_id"].astype(str) == batch_id]
    batches.loc[mask, "candidate_count"] = int(len(pool))
    batches.loc[mask, "final_count"] = int((pool["status"] == "最终候选").sum())
    batches.loc[mask, "status"] = "跟踪中" if (pool["status"] == "最终候选").sum() == 0 else "已决策"
    overwrite_table(BATCHES_PATH, batches)


def latest_verifiable_batch(candidates: pd.DataFrame) -> str:
    if candidates.empty:
        return ""
    batches = read_table(BATCHES_PATH)
    if batches.empty:
        return ""
    batches["_t"] = pd.to_datetime(batches["scan_time"], errors="coerce")
    for _, batch in batches.sort_values("_t", ascending=False).iterrows():
        bid = str(batch["batch_id"])
        pool = candidates[candidates["batch_id"].astype(str) == bid]
        if not pool.empty and (pool["status"] == "最终候选").any():
            return bid
    return str(batches.sort_values("_t").iloc[-1]["batch_id"])


def build_summary(batches: pd.DataFrame, candidates: pd.DataFrame, snapshots: pd.DataFrame, verifications: pd.DataFrame, batch_id: str) -> dict[str, Any]:
    pool = candidates[candidates["batch_id"] == batch_id] if batch_id and not candidates.empty else pd.DataFrame()
    verified = verifications if not verifications.empty else pd.DataFrame()
    success_rate = None
    avg_open = None
    if not verified.empty and "trade_label" in verified.columns:
        labels = pd.to_numeric(verified["trade_label"], errors="coerce").dropna()
        if not labels.empty:
            success_rate = round(float(labels.mean() * 100), 2)
        if "next_open_return" in verified.columns:
            avg_open = round(float(pd.to_numeric(verified["next_open_return"], errors="coerce").mean()), 3)
    return {
        "批次编号": batch_id,
        "候选股数量": int(len(pool)),
        "剔除股票数量": int(read_table(EXCLUDED_PATH).query("batch_id == @batch_id").shape[0]) if batch_id and EXCLUDED_PATH.exists() else 0,
        "最终候选数量": int((pool["status"] == "最终候选").sum()) if not pool.empty and "status" in pool.columns else 0,
        "待验证数量": int((pool["status"] == "最终候选").sum()) if not pool.empty and "status" in pool.columns else 0,
        "已验证数量": int(len(verified)),
        "历史达标率": success_rate,
        "平均开盘收益": avg_open,
        "跟踪快照数量": int(len(snapshots[snapshots["batch_id"] == batch_id])) if batch_id and not snapshots.empty else 0,
    }


def _save_feedback() -> None:
    verifications = read_table(VERIFICATIONS_PATH)
    candidates = read_table(CANDIDATES_PATH)
    feedback = {
        "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "样本数量": 0,
        "成功样本数": 0,
        "失败样本数": 0,
        "成功率": None,
        "主要成功特征": [],
        "主要失败特征": [],
        "建议提高权重": [],
        "建议降低权重": [],
        "指标对比": [],
    }
    if not verifications.empty and "trade_label" in verifications.columns:
        labels = pd.to_numeric(verifications["trade_label"], errors="coerce").fillna(0)
        success = int(labels.sum())
        total = int(len(labels))
        feedback.update({
            "样本数量": total,
            "成功样本数": success,
            "失败样本数": total - success,
            "成功率": round(success / max(total, 1) * 100, 2),
        })
        if total >= 3 and not candidates.empty:
            joined = verifications.merge(candidates, on=["batch_id", "symbol"], how="left", suffixes=("", "_candidate"))
            comparisons = compare_success_failure(joined)
            feedback["指标对比"] = comparisons
            feedback["主要成功特征"] = [item["指标"] for item in comparisons if item["成功均值"] > item["失败均值"]][:5]
            feedback["主要失败特征"] = [item["指标"] for item in comparisons if item["成功均值"] < item["失败均值"]][:5]
            feedback["建议提高权重"] = suggest_weight_up(comparisons)
            feedback["建议降低权重"] = suggest_weight_down(comparisons)
    ensure_storage()
    FEEDBACK_PATH.write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8")


def compare_success_failure(df: pd.DataFrame) -> list[dict[str, Any]]:
    label = pd.to_numeric(df["trade_label"], errors="coerce").fillna(0)
    metrics = [
        ("初筛评分", "initial_score"),
        ("跟踪评分", "tracking_score"),
        ("最终评分", "final_score"),
        ("当日涨幅", "change_pct"),
        ("距涨停", "limit_distance_pct"),
        ("五日涨幅", "five_day_return"),
        ("十日涨幅", "ten_day_return"),
        ("上影线比例", "upper_shadow_ratio"),
        ("日内位置", "close_position"),
    ]
    rows = []
    for label_name, col in metrics:
        if col not in df.columns:
            continue
        values = pd.to_numeric(df[col], errors="coerce")
        success_values = values[label == 1].dropna()
        failure_values = values[label == 0].dropna()
        if success_values.empty or failure_values.empty:
            continue
        rows.append({
            "指标": label_name,
            "成功均值": round(float(success_values.mean()), 4),
            "失败均值": round(float(failure_values.mean()), 4),
            "差值": round(float(success_values.mean() - failure_values.mean()), 4),
        })
    return sorted(rows, key=lambda item: abs(item["差值"]), reverse=True)


def suggest_weight_up(comparisons: list[dict[str, Any]]) -> list[str]:
    preferred = []
    for item in comparisons:
        name = item["指标"]
        delta = number(item["差值"])
        if delta > 0 and name in {"跟踪评分", "最终评分", "日内位置", "当日涨幅"}:
            preferred.append(name)
        if delta < 0 and name in {"距涨停", "上影线比例"}:
            preferred.append(f"{name}更低")
    return preferred[:5] or ["跟踪期涨幅", "跟踪成交额增量", "量价配合"]


def suggest_weight_down(comparisons: list[dict[str, Any]]) -> list[str]:
    lowered = []
    for item in comparisons:
        name = item["指标"]
        delta = number(item["差值"])
        if delta < 0 and name in {"五日涨幅", "十日涨幅", "上影线比例"}:
            lowered.append(name)
        if delta > 0 and name in {"距涨停"}:
            lowered.append(f"{name}过高")
    return lowered[:5] or ["单纯当日涨幅", "高位过热票"]


def load_feedback() -> dict[str, Any]:
    if not FEEDBACK_PATH.exists():
        _save_feedback()
    try:
        return json.loads(FEEDBACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def to_display_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    out = df.copy()
    columns = [c for c in out.columns if not c.startswith("_")]
    out = out[columns].rename(columns=DISPLAY_COLUMNS)
    cleaned = out.where(pd.notna(out), None)
    return [{k: clean_value(v) for k, v in row.items()} for row in cleaned.to_dict(orient="records")]


def table_row_to_display(df: pd.DataFrame) -> dict[str, Any] | None:
    rows = to_display_records(df)
    return rows[0] if rows else None


def clean_value(value: Any) -> Any:
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value


def number(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def calc_limit_price(pre_close: float, code: str, down: bool = False) -> float:
    rate = 0.1
    if code.startswith(("300", "301", "688")):
        rate = 0.2
    elif code.startswith(("4", "8", "9")):
        rate = 0.3
    return round(pre_close * (1 - rate if down else 1 + rate), 2)


def limit_threshold(code: str) -> float:
    if code.startswith(("300", "301", "688")):
        return 19.5
    if code.startswith(("4", "8", "9")):
        return 29.0
    return 9.6


def is_one_word_board(open_price: float, high: float, low: float, close: float, change_pct: float, code: str) -> bool:
    if min(open_price, high, low, close) <= 0:
        return False
    same = max(open_price, high, low, close) - min(open_price, high, low, close) <= 0.01
    return same and (change_pct >= limit_threshold(code) or change_pct <= -9.5)


def risk_penalty_from_reasons(reasons: list[str] | str) -> float:
    if isinstance(reasons, str):
        reasons = [r for r in reasons.split("；") if r]
    return min(30.0, len(reasons) * 6.0)


def build_include_reasons(row: pd.Series, derived: dict[str, Any]) -> str:
    reasons = []
    if number(row.get("change_pct")) >= 2:
        reasons.append("当日涨幅活跃")
    if number(row.get("amount")) >= 100_000_000:
        reasons.append("成交额达标")
    if number(row.get("turnover_rate")) >= 2:
        reasons.append("换手活跃")
    if number(row.get("volume_ratio")) >= 1.2:
        reasons.append("量比放大")
    if derived.get("near_limit_up"):
        reasons.append("接近涨停")
    return "；".join(reasons) or "基础条件达标"


def infer_market_env(realtime: pd.DataFrame) -> str:
    if realtime.empty or "change_pct" not in realtime.columns:
        return "未知"
    avg = float(pd.to_numeric(realtime["change_pct"], errors="coerce").mean())
    if avg <= -2:
        return "暂停"
    if avg <= -1:
        return "谨慎"
    return "正常"


def score_tracking(tail_return: float, amount_delta: float, volume_delta: float, limit_distance: float, pullback: float) -> float:
    score = 40.0
    if tail_return >= 1.5:
        score += 25
    elif tail_return >= 0.8:
        score += 18
    elif tail_return >= 0.3:
        score += 10
    elif tail_return < -0.5:
        score -= 20
    if amount_delta >= 100_000_000:
        score += 20
    elif amount_delta >= 50_000_000:
        score += 14
    elif amount_delta >= 20_000_000:
        score += 8
    if volume_delta > 0:
        score += 6
    if 1 <= limit_distance <= 5:
        score += 12
    elif limit_distance < 0.5:
        score -= 5
    elif limit_distance > 8:
        score -= 8
    if pullback > 2:
        score -= 15
    return max(0.0, min(100.0, score))


def merge_reason(existing: Any, new: Any) -> str:
    parts = []
    for text in [existing, new]:
        for item in str(text or "").split("；"):
            item = item.strip()
            if item and item not in parts and item.lower() != "nan":
                parts.append(item)
    return "；".join(parts)


def classify_failure(open_ret: float, high_ret: float, close_ret: float) -> str:
    if open_ret < 0:
        return "低开"
    if high_ret < 1:
        return "平开无冲高"
    if open_ret > 0 and close_ret < open_ret:
        return "高开低走"
    return "收益未达标"
