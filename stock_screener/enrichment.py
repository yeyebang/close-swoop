#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Second-stage candidate enrichment.

This module is intentionally best-effort. THS/Tencent endpoints are useful
signals, but they are not reliable enough to block the main scan.
"""

from __future__ import annotations

import concurrent
import time
from dataclasses import dataclass
from typing import Callable

import pandas as pd
from loguru import logger

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None


def _ak_call(func, timeout=10, **kwargs):
    """带超时的 akshare 调用（enrichment 专用，线程安全）"""
    if ak is None:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("enrichment API 调用超时")


def normalize_code(code) -> str:
    text = "".join(ch for ch in str(code).strip() if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


@dataclass
class EnrichmentConfig:
    enabled: bool = True
    candidate_count: int = 50
    ths_enabled: bool = True
    tencent_tick_enabled: bool = True
    tencent_tick_count: int = 20
    max_bonus: float = 20.0


def enrich_candidates(df: pd.DataFrame, config: dict | None = None) -> tuple[pd.DataFrame, dict]:
    cfg = _make_config(config)
    out = df.copy()
    if out.empty or not cfg.enabled:
        out["enrich_score"] = 0.0
        out["enrich_notes"] = ""
        return out, {"enabled": False}

    out["代码"] = out["代码"].map(normalize_code)
    out["enrich_score"] = 0.0
    out["enrich_notes"] = ""
    info = {"enabled": True, "ths_sources": [], "tick_checked": 0}

    if cfg.ths_enabled and ak is not None:
        ths_codes, ths_info = fetch_ths_signal_codes()
        info["ths_sources"] = ths_info
        for source_name, codes in ths_codes.items():
            if not codes:
                continue
            hit = out["代码"].isin(codes)
            bonus = _ths_bonus(source_name)
            out.loc[hit, "enrich_score"] += bonus
            out.loc[hit, "enrich_notes"] = out.loc[hit, "enrich_notes"].map(
                lambda x, s=source_name: _append_note(x, s)
            )

    if cfg.tencent_tick_enabled and ak is not None:
        tick_limit = min(cfg.tencent_tick_count, len(out))
        for idx in out.head(tick_limit).index:
            code = out.at[idx, "代码"]
            tick_score, tick_note = fetch_tencent_tick_score(code)
            if tick_score:
                out.at[idx, "enrich_score"] += tick_score
                out.at[idx, "enrich_notes"] = _append_note(out.at[idx, "enrich_notes"], tick_note)
            info["tick_checked"] += 1
            time.sleep(0.15)

    out["enrich_score"] = out["enrich_score"].clip(upper=cfg.max_bonus)
    return out, info


def fetch_ths_signal_codes() -> tuple[dict[str, set[str]], list[str]]:
    sources: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        ("同花顺量价齐升", ak.stock_rank_ljqs_ths),
        ("同花顺连续上涨", ak.stock_rank_lxsz_ths),
        ("同花顺突破5日线", lambda: ak.stock_rank_xstp_ths(symbol="5日均线")),
    ]
    signal_codes: dict[str, set[str]] = {}
    loaded: list[str] = []

    for source_name, func in sources:
        try:
            if callable(func):
                df = _ak_call(func, timeout=12)
            else:
                df = func
            codes = _extract_codes(df)
            signal_codes[source_name] = codes
            loaded.append(f"{source_name}:{len(codes)}")
        except TimeoutError:
            logger.warning(f"{source_name}获取超时")
            signal_codes[source_name] = set()
        except Exception as e:
            logger.warning(f"{source_name}获取失败: {e}")
            signal_codes[source_name] = set()

    concept_codes = fetch_hot_concept_codes()
    signal_codes["同花顺热门概念"] = concept_codes
    if concept_codes:
        loaded.append(f"同花顺热门概念:{len(concept_codes)}")
    return signal_codes, loaded


def fetch_hot_concept_codes(limit: int = 5) -> set[str]:
    """Best-effort THS concept constituents.

    Some AKShare versions expose concept list columns differently. If we cannot
    infer hot concepts safely, this function returns an empty set.
    """
    try:
        concept_df = _ak_call(ak.stock_board_concept_name_ths, timeout=12)
    except TimeoutError:
        logger.warning(f"同花顺概念列表获取超时")
        return set()
    except Exception as e:
        logger.warning(f"同花顺概念列表获取失败: {e}")
        return set()
    if concept_df is None or concept_df.empty:
        return set()

    name_col = _find_column(concept_df, ["概念名称", "板块", "名称", "name"])
    if not name_col:
        return set()

    sort_col = _find_column(concept_df, ["涨跌幅", "涨幅", "change"])
    concepts = concept_df.copy()
    if sort_col:
        concepts[sort_col] = pd.to_numeric(concepts[sort_col], errors="coerce")
        concepts = concepts.sort_values(sort_col, ascending=False)

    codes: set[str] = set()
    for name in concepts[name_col].dropna().astype(str).head(limit):
        try:
            info_df = _ak_call(ak.stock_board_concept_info_ths, timeout=10, symbol=name)
            if info_df is not None and not info_df.empty:
                codes.update(_extract_codes(info_df))
        except TimeoutError:
            logger.warning(f"同花顺概念成分获取超时({name})")
        except Exception as e:
            logger.warning(f"同花顺概念成分获取失败({name}): {e}")
    return codes


def fetch_tencent_tick_score(code: str) -> tuple[float, str]:
    symbol = _tencent_symbol(code)
    try:
        df = _ak_call(ak.stock_zh_a_tick_tx_js, timeout=10, symbol=symbol)
        if df is None:
            return 0.0, ""
    except TimeoutError:
        logger.warning(f"腾讯分笔获取超时({code})")
        return 0.0, ""
    except Exception as e:
        logger.warning(f"腾讯分笔获取失败({code}): {e}")
        return 0.0, ""
    if df is None or df.empty:
        return 0.0, ""

    tail = df.tail(80).copy()
    side_col = _find_column(tail, ["性质", "买卖盘", "方向"])
    amount_col = _find_column(tail, ["成交金额", "金额"])
    volume_col = _find_column(tail, ["成交量", "手数", "volume"])

    score = 0.0
    notes: list[str] = []
    if side_col:
        side = tail[side_col].astype(str)
        buy_mask = side.str.contains("买|B|主动买", case=False, regex=True)
        sell_mask = side.str.contains("卖|S|主动卖", case=False, regex=True)
        denom = int(buy_mask.sum() + sell_mask.sum())
        if denom > 0:
            buy_ratio = float(buy_mask.sum() / denom)
            if buy_ratio >= 0.62:
                score += 6
                notes.append(f"腾讯买盘占优{buy_ratio:.0%}")
            elif buy_ratio >= 0.55:
                score += 3
                notes.append(f"腾讯买盘偏强{buy_ratio:.0%}")

    numeric_col = amount_col or volume_col
    if numeric_col:
        values = pd.to_numeric(tail[numeric_col], errors="coerce").dropna()
        if len(values) >= 20:
            recent = values.tail(20).mean()
            base = values.head(max(len(values) - 20, 1)).mean()
            if base > 0 and recent / base >= 1.5:
                score += 4
                notes.append("腾讯尾段成交放大")

    return min(score, 10.0), "、".join(notes)


def _make_config(config: dict | None) -> EnrichmentConfig:
    config = config or {}
    return EnrichmentConfig(
        enabled=bool(config.get("enabled", True)),
        candidate_count=int(config.get("candidate_count", 50)),
        ths_enabled=bool(config.get("ths_enabled", True)),
        tencent_tick_enabled=bool(config.get("tencent_tick_enabled", True)),
        tencent_tick_count=int(config.get("tencent_tick_count", 20)),
        max_bonus=float(config.get("max_bonus", 20)),
    )


def _extract_codes(df: pd.DataFrame) -> set[str]:
    if df is None or df.empty:
        return set()
    code_col = _find_column(df, ["代码", "股票代码", "证券代码", "code"])
    if not code_col:
        return set()
    return {normalize_code(v) for v in df[code_col].dropna().tolist() if normalize_code(v)}


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    lower_map = {str(col).lower(): col for col in df.columns}
    for candidate in candidates:
        if candidate.lower() in lower_map:
            return lower_map[candidate.lower()]
    for col in df.columns:
        text = str(col).lower()
        if any(candidate.lower() in text for candidate in candidates):
            return col
    return None


def _ths_bonus(source_name: str) -> float:
    if "量价齐升" in source_name:
        return 6.0
    if "突破" in source_name:
        return 4.0
    if "连续上涨" in source_name:
        return 3.0
    if "概念" in source_name:
        return 5.0
    return 2.0


def _append_note(existing, note: str) -> str:
    existing = "" if pd.isna(existing) else str(existing)
    if not note:
        return existing
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}、{note}"


def _tencent_symbol(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"
