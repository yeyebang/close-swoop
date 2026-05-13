#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股尾盘涨停扫描器
每日14:00自动扫描，筛选尾盘拉升、有涨停潜力的股票
规则评分 + 次日卖出策略
"""

import concurrent
import sys
import os
import json
import time
import pandas as pd
import requests as _requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger

from stock_screener.research import (
    BacktestConfig,
    LEGACY_DATA_DIR,
    backtest_rules,
    build_dataset,
    resolve_data_dir,
    save_backtest_report,
    rank_latest_candidates,
    train_lightgbm,
)
from stock_screener.minute_backtest import (
    MinuteBacktestConfig,
    build_minute_dataset,
    backtest_minute_rules,
    save_minute_backtest_report,
    train_minute_model,
    predict_with_minute_model,
    MINUTE_FEATURE_COLUMNS,
    load_minute_model,
    rule_score_minute,
)
from stock_screener.paper import (
    append_signals,
    apply_adaptive_scores,
    paper_report,
    settle_pending,
)
from stock_screener.enrichment import enrich_candidates

# 触发 data_fetcher 模块加载，注入 resilient session（解决东方财富断连问题）
import stock_screener.data_fetcher  # noqa: F401

try:
    import akshare as ak
except ImportError:
    print("需要安装 akshare，运行: pip install akshare")
    sys.exit(1)


# ==================== 超时工具 ====================

def ak_call(func, timeout=15, **kwargs):
    """带超时的 akshare 调用，线程安全，兼容 macOS"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func, **kwargs)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("akshare API 调用超时")


# ==================== 配置 ====================

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR.parent / "config.json"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"


def load_config():
    """加载配置文件"""
    default_config = {
        "scan_hour": 14,
        "scan_minute": 0,
        "backtrack_days": 30,
        "top_n": 10,
        "max_history_fetch": 500,
        "realtime_cache_ttl_seconds": 120,
        "history_primary_max_attempts": 1,
        "history_primary_failure_threshold": 5,
        "enrichment": {
            "enabled": True,
            "candidate_count": 50,
            "ths_enabled": True,
            "tencent_tick_enabled": True,
            "tencent_tick_count": 20,
            "max_bonus": 20,
        },
        "paper": {
            "enabled": True,
            "success_return_pct": 1.0,
            "adaptive_min_samples": 30,
        },
        "filters": {
            "min_volume": 10000,
            "max_price": 50,
            "min_price": 3,
            "exclude_st": True,
            "exclude_new": True,
        }
    }
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        default_config.update(config)
    data_dir = Path(default_config.get("data_dir", DATA_DIR))
    if not data_dir.is_absolute():
        data_dir = BASE_DIR.parent / data_dir
    default_config["resolved_data_dir"] = str(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return default_config


LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
logger.remove()
logger.add(LOG_DIR / "scanner_{time:YYYY-MM-DD}.log", rotation="10 MB", retention="30 days", level="INFO", encoding="utf-8")
logger.add(sys.stdout, format=LOG_FORMAT, level="INFO")


# ==================== 数据获取 ====================

def normalize_code(code):
    """Normalize A-share code to 6 digits."""
    text = "".join(ch for ch in str(code).strip() if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


class DataFetcher:
    """从AKShare获取A股数据"""

    def __init__(self, config):
        self.config = config
        self.data_dir = resolve_data_dir(config)
        self.data_dir.mkdir(exist_ok=True)
        self.legacy_data_dir = LEGACY_DATA_DIR if LEGACY_DATA_DIR != self.data_dir and LEGACY_DATA_DIR.exists() else None
        self.history_refresh_disabled = False
        self.history_refresh_failures = 0
        self.history_primary_disabled = False
        self.history_primary_failures = 0
        logger.info(f"数据缓存目录: {self.data_dir}")
        if self.legacy_data_dir:
            logger.info(f"历史兼容缓存目录: {self.legacy_data_dir}")

    def _ensure_columns(self, df, source=None, refresh_captured_at=True):
        """统一列名映射"""
        col_map = {
            "代码": "code", "名称": "name", "最新价": "last_price",
            "涨跌幅": "change_pct", "涨跌额": "change_amt", "成交量": "volume",
            "成交额": "amount", "振幅": "amplitude", "最高": "high",
            "最低": "low", "今开": "open_price", "昨收": "pre_close",
            "量比": "volume_ratio", "换手率": "turnover_rate",
        }
        df = df.rename(columns=col_map)
        if "code" in df.columns:
            df["code"] = df["code"].map(normalize_code)
        if refresh_captured_at or "采集时间" not in df.columns:
            df["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if source is not None:
            df["数据源"] = source
        elif "数据源" not in df.columns:
            df["数据源"] = "unknown"
        df.drop(columns=["序号"], errors="ignore", inplace=True)

        defaults = {
            "name": "",
            "last_price": 0,
            "change_pct": 0,
            "change_amt": 0,
            "volume": 0,
            "amount": 0,
            "amplitude": 0,
            "high": 0,
            "low": 0,
            "open_price": 0,
            "pre_close": 0,
            "volume_ratio": 0,
            "turnover_rate": 0,
        }
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default
        for col in defaults:
            if col != "name":
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df

    @staticmethod
    def _format_cache_age(age_seconds):
        if age_seconds is None or pd.isna(age_seconds):
            return "未知"
        return f"{age_seconds:.0f}s"

    def _read_realtime_cache(self, cache_file, today_only=True, max_age_seconds=None):
        if not cache_file.exists():
            return pd.DataFrame(), None, None
        try:
            df = pd.read_csv(cache_file)
            cached_at = pd.to_datetime(df["采集时间"].iloc[0], errors="coerce") if "采集时间" in df.columns else None
            if cached_at is not None and pd.isna(cached_at):
                cached_at = None
            age_seconds = None
            if cached_at is not None:
                age_seconds = (datetime.now() - cached_at.to_pydatetime()).total_seconds()
            if today_only and (cached_at is None or cached_at.strftime("%Y-%m-%d") != datetime.now().strftime("%Y-%m-%d")):
                return pd.DataFrame(), cached_at, age_seconds
            if max_age_seconds is not None and (age_seconds is None or age_seconds > max_age_seconds):
                return pd.DataFrame(), cached_at, age_seconds
            return self._ensure_columns(df, refresh_captured_at=False), cached_at, age_seconds
        except Exception as e:
            logger.warning(f"读取实时缓存失败: {e}")
            return pd.DataFrame(), None, None

    # ---- 腾讯直连行情 ----
    _TX_FIELDS = {
        "name": 1, "code": 2, "last_price": 3, "pre_close": 4,
        "open_price": 5, "volume": 6, "change_amt": 31, "change_pct": 32,
        "high": 33, "low": 34, "amount": 37, "turnover_rate": 38,
        "amplitude": 43, "volume_ratio": 49,
    }
    _TX_HEADERS = {
        "Referer": "https://finance.qq.com",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }

    @staticmethod
    def _to_tx_prefix(code: str) -> str:
        if code.startswith(("6", "9")):
            return "sh" + code
        if code.startswith(("4", "8")):
            return "bj" + code
        return "sz" + code

    def _get_all_codes_tx(self) -> list:
        cache_file = self.data_dir / "cache_all_codes.json"
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                cached_at = datetime.fromisoformat(payload["cached_at"])
                if (datetime.now() - cached_at).total_seconds() < 86400:
                    return payload["codes"]
                # 过期但有缓存，先保留备用
                stale_codes = payload.get("codes", [])
            except Exception:
                stale_codes = []
        else:
            stale_codes = []
        try:
            df = ak.stock_info_a_code_name()
            codes = [self._to_tx_prefix(str(r).zfill(6)) for r in df["code"].tolist()]
            cache_file.write_text(
                json.dumps({"cached_at": datetime.now().isoformat(), "codes": codes}, ensure_ascii=False),
                encoding="utf-8",
            )
            return codes
        except Exception as e:
            logger.warning(f"获取股票代码列表失败: {e}")
            if stale_codes:
                logger.warning(f"使用过期代码缓存（{len(stale_codes)} 只）")
                return stale_codes
            return []

    def _fetch_tencent_batch(self, batch: list) -> list:
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            resp = _requests.get(url, headers=self._TX_HEADERS, timeout=8)
            resp.raise_for_status()
        except Exception as e:
            logger.debug(f"腾讯行情批次失败: {e}")
            return []
        rows = []
        for line in resp.text.strip().split("\n"):
            if "~" not in line or "=" not in line:
                continue
            try:
                parts = line.split('"')[1].split("~")
                if len(parts) < 50 or not parts[3]:
                    continue
                row = {col: parts[idx] if idx < len(parts) else "" for col, idx in self._TX_FIELDS.items()}
                row["code"] = normalize_code(row["code"])
                row["amount"] = float(row["amount"]) * 10000 if row["amount"] else 0.0
                rows.append(row)
            except Exception:
                continue
        return rows

    def _fetch_tencent_realtime(self) -> pd.DataFrame:
        codes = self._get_all_codes_tx()
        if not codes:
            logger.warning("腾讯行情: 股票代码列表为空，跳过")
            return pd.DataFrame()
        batches = [codes[i: i + 100] for i in range(0, len(codes), 100)]
        all_rows = []
        failed = 0
        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                for rows in pool.map(self._fetch_tencent_batch, batches):
                    if rows:
                        all_rows.extend(rows)
                    else:
                        failed += 1
        except Exception as e:
            logger.warning(f"腾讯行情并发拉取异常: {e}")
            return pd.DataFrame()
        logger.info(f"腾讯行情批次: 成功 {len(batches)-failed}/{len(batches)}, 得到 {len(all_rows)} 条")
        if not all_rows:
            logger.warning("腾讯行情: 所有批次均返回空数据")
            return pd.DataFrame()
        df = pd.DataFrame(all_rows)
        for col in [c for c in df.columns if c not in ("name", "code")]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df["name"] = df["name"].fillna("")
        df["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df["数据源"] = "腾讯"
        logger.info(f"腾讯行情获取成功: {len(df)} 条")
        return df

    def get_all_realtime(self):
        """获取全市场实时行情（腾讯直连优先，新浪备用）"""
        logger.info("正在获取全市场实时数据...")
        cache_file = self.data_dir / "realtime.csv"
        ttl_seconds = int(self.config.get("realtime_cache_ttl_seconds", 120))

        cached_df, cached_at, cache_age = self._read_realtime_cache(
            cache_file, today_only=True, max_age_seconds=ttl_seconds,
        )
        if cached_df.empty and self.legacy_data_dir is not None:
            legacy_cached_df, legacy_cached_at, legacy_cache_age = self._read_realtime_cache(
                self.legacy_data_dir / "realtime.csv", today_only=True, max_age_seconds=ttl_seconds,
            )
            if not legacy_cached_df.empty:
                cached_df, cached_at, cache_age = legacy_cached_df, legacy_cached_at, legacy_cache_age
        if not cached_df.empty:
            logger.info(f"使用缓存的实时数据: {cached_at}，缓存年龄 {self._format_cache_age(cache_age)}，数据源 {cached_df['数据源'].iloc[0]}")
            return cached_df
        if cached_at is not None:
            logger.info(f"实时缓存已过期: {cached_at}，重新拉取")

        # 腾讯直连（主力）
        try:
            df = self._fetch_tencent_realtime()
        except Exception as e:
            logger.warning(f"腾讯行情异常: {e}")
            df = pd.DataFrame()
        if not df.empty:
            df.to_csv(cache_file, index=False, encoding="utf-8-sig")
            return df

        # 新浪（备用）
        logger.warning("腾讯行情失败，降级到新浪")
        for attempt in range(1, 4):
            try:
                logger.info(f"尝试从新浪获取实时行情({attempt}/3)...")
                df = ak_call(ak.stock_zh_a_spot, timeout=30)
                df = self._ensure_columns(df, source="新浪")
                if not df.empty:
                    logger.info(f"成功从新浪获取 {len(df)} 只股票实时数据")
                    df.to_csv(cache_file, index=False, encoding="utf-8-sig")
                    return df
            except Exception as e:
                logger.warning(f"新浪实时行情获取失败({attempt}/3): {e}")
                time.sleep(1)

        stale_df, stale_at, stale_age = self._read_realtime_cache(cache_file, today_only=False)
        if stale_df.empty and self.legacy_data_dir is not None:
            stale_df, stale_at, stale_age = self._read_realtime_cache(
                self.legacy_data_dir / "realtime.csv", today_only=False,
            )
        if not stale_df.empty:
            source = stale_df["数据源"].iloc[0] if "数据源" in stale_df.columns else "unknown"
            logger.error(f"实时行情接口全部失败，使用最近缓存: {stale_at}，缓存年龄 {self._format_cache_age(stale_age)}，数据源 {source}")
            return stale_df

        logger.error("获取实时行情全部失败")
        return pd.DataFrame()

    def get_history(self, code, days=30):
        """获取个股历史日K线（前复权）"""
        code = normalize_code(code)
        cache_file = self.data_dir / f"history_{code}.csv"
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        max_cache_age_days = int(self.config.get("max_history_cache_age_days", 7))
        cached_df, cached_path = self._read_history_cache(code)

        if len(cached_df) >= 5 and self._history_cache_is_fresh(cached_df, max_cache_age_days):
            return cached_df
        if len(cached_df) >= 5:
            last_date = self._last_history_date(cached_df)
            logger.info(f"{code} 历史缓存较旧({last_date})，尝试刷新")

        if self.history_refresh_disabled:
            if len(cached_df) >= 5:
                logger.debug(f"{code} 历史接口本轮已熔断，使用缓存: {cached_path}")
                return cached_df
            logger.debug(f"{code} 历史接口本轮已熔断，且无可用缓存")
            return pd.DataFrame()

        last_error = None
        # 主源：腾讯日K（与实时行情同一通道，稳定快速）
        if not self.history_primary_disabled:
            attempts = max(1, int(self.config.get("history_primary_max_attempts", 1)))
            for attempt in range(1, attempts + 1):
                try:
                    df = self._fetch_history_from_tencent(code, days=days)
                    if not df.empty:
                        df.to_csv(cache_file, index=False, encoding="utf-8-sig")
                        logger.debug(f"获取 {code} 历史数据成功: 腾讯 {len(df)} 行")
                        self.history_primary_failures = 0
                        self.history_refresh_failures = 0
                        return df
                    last_error = f"{code} 腾讯返回空数据"
                except Exception as e:
                    last_error = e
                    logger.debug(f"{code} 腾讯历史源失败({attempt}/{attempts}): {e}")
                if attempt < attempts:
                    time.sleep(0.5)
            self._record_history_primary_failure(last_error)
            logger.info(f"{code} 腾讯历史源失败，改用新浪备用源")
        else:
            logger.debug(f"{code} 腾讯历史源本轮已暂停，直接使用新浪备用源")

        fallback = self._fetch_history_from_sina(code, start_date, end_date)
        if not fallback.empty:
            fallback.to_csv(cache_file, index=False, encoding="utf-8-sig")
            logger.info(f"获取 {code} 历史数据成功: 新浪备用源 {len(fallback)} 行")
            self.history_refresh_failures = 0
            return fallback

        self._record_history_refresh_failure()
        if len(cached_df) >= 5:
            last_date = self._last_history_date(cached_df)
            logger.warning(f"获取 {code} 历史数据全部失败，使用旧缓存({last_date}): {last_error}")
            return cached_df

        logger.warning(f"获取 {code} 历史数据全部失败，无法参与本轮评分: {last_error}")
        return pd.DataFrame()

    def _read_history_cache(self, code):
        paths = [self.data_dir / f"history_{code}.csv"]
        if self.legacy_data_dir is not None:
            legacy_path = self.legacy_data_dir / f"history_{code}.csv"
            if legacy_path not in paths:
                paths.append(legacy_path)
        for path in paths:
            if not path.exists():
                continue
            try:
                df = pd.read_csv(path)
                if len(df) >= 5:
                    return df, path
            except Exception as e:
                logger.warning(f"读取 {code} 历史缓存失败 [{path}]: {e}")
        return pd.DataFrame(), None

    def _record_history_refresh_failure(self):
        self.history_refresh_failures += 1
        threshold = int(self.config.get("history_refresh_failure_threshold", 3))
        if self.history_refresh_failures >= threshold and not self.history_refresh_disabled:
            self.history_refresh_disabled = True
            logger.error(
                f"历史行情接口连续失败 {self.history_refresh_failures} 只，本轮停止刷新历史数据，"
                "后续股票只使用本地缓存，避免扫描被外部接口拖垮"
            )

    def _record_history_primary_failure(self, last_error):
        self.history_primary_failures += 1
        threshold = int(self.config.get("history_primary_failure_threshold", 5))
        if self.history_primary_failures >= threshold and not self.history_primary_disabled:
            self.history_primary_disabled = True
            logger.info(
                f"东方财富历史源连续断开 {self.history_primary_failures} 只，本轮扫描改用新浪历史源。"
                f"最近一次原因: {last_error}"
            )

    @staticmethod
    def _last_history_date(df):
        if df.empty:
            return None
        for col in ["日期", "date"]:
            if col in df.columns:
                values = pd.to_datetime(df[col], errors="coerce").dropna()
                if not values.empty:
                    return values.max().date()
        return None

    def _history_cache_is_fresh(self, df, max_age_days):
        last_date = self._last_history_date(df)
        if last_date is None:
            return False
        return (datetime.now().date() - last_date).days <= max_age_days

    @staticmethod
    def _daily_symbol(code):
        if code.startswith("6"):
            return f"sh{code}"
        if code.startswith(("0", "2", "3")):
            return f"sz{code}"
        if code.startswith(("4", "8", "9")):
            return f"bj{code}"
        return code

    def _fetch_history_from_tencent(self, code, days=30):
        """腾讯前复权日K (web.ifzq.gtimg.cn)"""
        sym = self._to_tx_prefix(code)
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=days * 2 + 10)).strftime("%Y-%m-%d")
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={sym},day,{start},{end},{days + 5},qfq")
        try:
            resp = _requests.get(
                url,
                headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            node = data.get("data", {}).get(sym, {})
            items = node.get("qfqday") or node.get("day") or []
            rows = []
            for item in items:
                if len(item) < 6:
                    continue
                try:
                    rows.append({
                        "日期": str(item[0])[:10],
                        "开盘": float(item[1]),
                        "收盘": float(item[2]),
                        "最高": float(item[3]),
                        "最低": float(item[4]),
                        "成交量": float(item[5]),
                    })
                except (ValueError, TypeError):
                    continue
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            return self._ensure_history_columns(df)
        except Exception as e:
            logger.debug(f"{code} 腾讯历史源失败: {e}")
            return pd.DataFrame()

    def _fetch_history_from_sina(self, code, start_date, end_date):
        try:
            df = ak_call(
                ak.stock_zh_a_daily,
                timeout=15,
                symbol=self._daily_symbol(code),
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
            )
            if df is None or df.empty:
                return pd.DataFrame()
            return self._ensure_history_columns(df)
        except Exception as e:
            logger.warning(f"获取 {code} 新浪历史数据失败: {e}")
            return pd.DataFrame()

    @staticmethod
    def _ensure_history_columns(df):
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.copy()
        if isinstance(df.index, pd.DatetimeIndex) and "date" not in df.columns and "日期" not in df.columns:
            df = df.reset_index().rename(columns={"index": "date"})
        col_map = {
            "date": "日期",
            "open": "开盘",
            "close": "收盘",
            "high": "最高",
            "low": "最低",
            "volume": "成交量",
            "amount": "成交额",
            "outstanding_share": "流通股本",
            "turnover": "换手率",
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if "日期" in df.columns:
            df["日期"] = pd.to_datetime(df["日期"], errors="coerce").dt.strftime("%Y-%m-%d")
        if "换手率" not in df.columns:
            df["换手率"] = 0
        for col in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return df


# ==================== 特征工程 ====================

def calc_features(stock_row, history_df):
    """为一只股票计算所有技术指标特征"""
    if history_df is None or len(history_df) < 5:
        return None

    required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "换手率"]
    if not all(c in history_df.columns for c in required):
        return None

    hist = history_df[required].copy().tail(30)
    curr = hist.iloc[-1]
    if len(hist) < 10:
        return None

    features = {}

    # === 实时数据中已有的字段 ===
    features["volume_ratio"] = float(stock_row.get("volume_ratio", 0) or 0)
    features["turnover_rate"] = float(stock_row.get("turnover_rate", 0) or 0)
    features["change_pct"] = float(stock_row.get("change_pct", 0) or 0)
    features["amplitude"] = float(stock_row.get("amplitude", 0) or 0)
    features["last_price"] = float(stock_row.get("last_price", 0) or 0)
    features["pre_close"] = float(stock_row.get("pre_close", 0) or 0)

    # === 从历史数据计算的特征 ===
    # 1. MA5 / MA10 / MA20 偏离度
    features["ma5_dev"] = (curr["收盘"] / hist["收盘"].iloc[-5:].mean() - 1) * 100
    features["ma10_dev"] = (curr["收盘"] / hist["收盘"].iloc[-10:].mean() - 1) * 100
    features["ma20_dev"] = (curr["收盘"] / hist["收盘"].iloc[-20:].mean() - 1) * 100 if len(hist) >= 20 else 0

    # 2. 量比 vs 过去N日均值
    vol_ma5 = hist["成交量"].iloc[-5:-1].mean()
    features["vol_vs_ma5"] = curr["成交量"] / max(vol_ma5, 1)

    # 3. 换手率趋势
    turn_ma5 = hist["换手率"].iloc[-5:-1].mean()
    features["turn_vs_ma5"] = curr["换手率"] / max(turn_ma5, 0.1)

    # 4. 近N日涨幅
    if len(hist) >= 5:
        features["return_5d"] = (curr["收盘"] / hist["收盘"].iloc[-5] - 1) * 100
    if len(hist) >= 10:
        features["return_10d"] = (curr["收盘"] / hist["收盘"].iloc[-10] - 1) * 100
    else:
        features["return_10d"] = 0

    # 5. 波动率标准差
    if len(hist) >= 10:
        features["volatility_10d"] = hist["换手率"].iloc[-10:].std()
    else:
        features["volatility_10d"] = 0

    # 6. 均线多头排列
    ma_vals = []
    for n in [5, 10, 20]:
        if len(hist) >= n:
            ma_vals.append(hist["收盘"].iloc[-n:].mean())
    features["ma_bull_aligned"] = (len(ma_vals) >= 3 and
                                   ma_vals[0] > ma_vals[1] > ma_vals[2] if len(ma_vals) == 3 else 0)
    features["ma_bull_aligned"] = 1 if (len(ma_vals) >= 3 and
                                        ma_vals[0] > ma_vals[1] > ma_vals[2]) else 0

    # 7. 价格位置（相对N日高低点）
    if len(hist) >= 5:
        h5 = hist["最高"].iloc[-5:].max()
        l5 = hist["最低"].iloc[-5:].min()
        if h5 > l5:
            features["price_position"] = (curr["收盘"] - l5) / (h5 - l5) * 100
        else:
            features["price_position"] = 50
    else:
        features["price_position"] = 50

    return features


# ==================== 评分系统 ====================

def calc_score(features, stock_row):
    """
    基于规则给股票打分（满分100）
    关注：尾盘拉升形态、放量、活跃度
    """
    if features is None:
        return 0, 105

    score = 0
    pct = lambda c, t: int(c / t * 100)  # 百分比

    # ---------- 1. 当前涨幅 (25分) ----------
    # 目标：3%-7%，有一定涨幅但还没涨停
    chg = features["change_pct"]
    if 3.5 <= chg <= 7.0:
        score += 25      # 最佳区间
    elif 2.0 <= chg < 3.5:
        score += 18      # 偏保守
    elif 7.0 < chg <= 9.0:
        score += 20      # 接近涨停，还有空间
    elif 0.5 <= chg < 2.0:
        score += 12      # 涨得少，尾盘有拉升潜力
    elif -2.0 < chg < 0.5:
        score += 5       # 微跌/平盘，需要强放量
    else:
        score += 0       # 涨跌太多都不好

    # ---------- 2. 量比 (20分) ----------
    vol_ratio = features["volume_ratio"]
    if vol_ratio >= 2.5:
        score += 20
    elif vol_ratio >= 2.0:
        score += 17
    elif vol_ratio >= 1.5:
        score += 13
    elif vol_ratio >= 1.2:
        score += 8
    elif vol_ratio >= 1.0:
        score += 4
    else:
        score += 0

    # ---------- 3. 换手率 (15分) ----------
    turn = features["turnover_rate"]
    if 5.0 <= turn <= 15.0:
        score += 15      # 最佳活跃度
    elif 3.0 <= turn < 5.0:
        score += 12
    elif 15.0 < turn <= 25.0:
        score += 10      # 偏高但可能热门股
    elif 2.0 <= turn < 3.0:
        score += 6
    else:
        score += 0

    # ---------- 4. 放量确认 (15分) ----------
    vol_vs_ma5 = features.get("vol_vs_ma5", 1)
    if vol_vs_ma5 >= 3.0:
        score += 15
    elif vol_vs_ma5 >= 2.0:
        score += 12
    elif vol_vs_ma5 >= 1.5:
        score += 8
    elif vol_vs_ma5 >= 1.0:
        score += 3

    # ---------- 5. 换手率趋势 (10分) ----------
    turn_vs_ma5 = features.get("turn_vs_ma5", 1)
    if turn_vs_ma5 >= 1.5:
        score += 10
    elif turn_vs_ma5 >= 1.2:
        score += 7
    elif turn_vs_ma5 >= 1.0:
        score += 3

    # ---------- 6. 近5日涨幅（温和上涨）(10分) ----------
    ret_5d = features.get("return_5d", 0)
    if 3.0 <= ret_5d <= 12.0:
        score += 10
    elif 0 <= ret_5d < 3.0:
        score += 8
    elif -3.0 < ret_5d < 0:
        score += 5       # 超跌反弹逻辑

    # ---------- 7. 均线多头排列 (5分) ----------
    if features.get("ma_bull_aligned", 0) == 1:
        score += 5

    # ---------- 8. 价格位置（在N日较高处）(5分) ----------
    pos = features.get("price_position", 50)
    if 60 <= pos <= 95:  # 偏高但不摸顶
        score += 5
    elif 40 <= pos < 60:
        score += 3

    return score, 105


# ==================== 主扫描流程 ====================

def scan_stocks(config):
    """执行完整的扫描流程"""
    scan_time = pd.Timestamp.now()
    logger.info("=" * 60)
    logger.info("🚀 A股尾盘涨停扫描器 启动")
    logger.info(f"扫描时间: {scan_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    fetcher = DataFetcher(config)
    filters = config.get("filters", {})

    # --- Step 1: 获取全市场实时数据 ---
    df_all = fetcher.get_all_realtime()
    if df_all is None or len(df_all) == 0:
        logger.error("无法获取A股实时数据，程序退出")
        return pd.DataFrame()

    logger.info(f"获取到 {len(df_all)} 只股票")
    data_source = str(df_all["数据源"].iloc[0]) if "数据源" in df_all.columns and not df_all.empty else "unknown"
    data_captured_at = str(df_all["采集时间"].iloc[0]) if "采集时间" in df_all.columns and not df_all.empty else ""
    logger.info(f"本次行情数据源: {data_source}，采集时间: {data_captured_at}")

    # --- Step 1.5: 复盘上一次虚拟盘推荐 ---
    paper_cfg = config.get("paper", {})
    if paper_cfg.get("enabled", True):
        fallback_dirs = [fetcher.legacy_data_dir] if fetcher.legacy_data_dir else []
        settle_info = settle_pending(
            df_all,
            fetcher.data_dir,
            success_return_pct=float(paper_cfg.get("success_return_pct", 1.0)),
            fallback_data_dirs=fallback_dirs,
        )
        if settle_info.get("settled", 0) > 0:
            logger.info(f"虚拟盘复盘完成: 结算 {settle_info['settled']} 条，未结算 {settle_info['open']} 条")
        else:
            logger.info(f"虚拟盘暂无可复盘记录，未结算 {settle_info.get('open', 0)} 条")

    # --- Step 2: 基本过滤 ---
    df_filtered = df_all.copy()

    # 排除ST
    if filters.get("exclude_st", True):
        df_filtered = df_filtered[~df_filtered["name"].astype(str).str.contains("ST", na=False)]
        logger.info(f"排除ST后: {len(df_filtered)} 只")

    # 排除科创板688 / 创业板300
    df_filtered = df_filtered[~df_filtered["code"].astype(str).str.startswith(("688", "300", "301", "920"))]
    logger.info(f"排除科创板/创业板/北交所后: {len(df_filtered)} 只")

    # 价格区间
    df_filtered = df_filtered[(df_filtered["last_price"] >= filters.get("min_price", 3)) &
                              (df_filtered["last_price"] <= filters.get("max_price", 50))]
    logger.info(f"价格过滤({filters.get('min_price', 3)}-{filters.get('max_price', 50)}元): {len(df_filtered)} 只")

    # 排除当日涨跌停
    df_filtered = df_filtered[
        (df_filtered["change_pct"] < 9.8) &
        (df_filtered["change_pct"] > -4.0)
    ]
    logger.info(f"排除涨跌停后: {len(df_filtered)} 只")

    if df_filtered.empty:
        logger.warning("没有满足基本条件的股票")
        return pd.DataFrame()

    # 先用实时字段做轻量预排序，再拉历史数据。这样不会对几千只股票逐个请求历史K线。
    max_history_fetch = int(config.get("max_history_fetch", 500))
    df_filtered = df_filtered.assign(
        _prefilter_score=(
            df_filtered["change_pct"].fillna(0).clip(lower=-2, upper=9) * 3
            + df_filtered["volume_ratio"].fillna(0).clip(upper=5) * 8
            + df_filtered["turnover_rate"].fillna(0).clip(upper=25)
        )
    ).sort_values("_prefilter_score", ascending=False)
    if len(df_filtered) > max_history_fetch:
        logger.info(f"实时预筛后仅处理前 {max_history_fetch}/{len(df_filtered)} 只，避免历史接口过载")
        df_filtered = df_filtered.head(max_history_fetch)

    # --- Step 3: 逐只股票计算特征 + 评分 ---
    results = []
    total = len(df_filtered)

    for i, (_, stock) in enumerate(df_filtered.iterrows()):
        code = normalize_code(stock["code"])

        try:
            # 获取历史数据
            history = fetcher.get_history(code, config.get("backtrack_days", 30))

            # 计算特征
            features = calc_features(stock, history)
            if features is None:
                continue

            # 打分
            s, max_s = calc_score(features, stock)
            pct = int(s / max_s * 100)

            results.append({
                "信号时间": scan_time.strftime("%Y-%m-%d %H:%M:%S"),
                "代码": code,
                "名称": str(stock.get("name", "")),
                "最新价": round(float(stock.get("last_price", 0)), 2),
                "买入价": round(float(stock.get("last_price", 0)), 2),
                "计划卖出": "次日开盘",
                "涨跌幅%": round(float(stock.get("change_pct", 0)), 2),
                "量比": round(float(stock.get("volume_ratio", 0)), 2),
                "换手率%": round(float(stock.get("turnover_rate", 0)), 2),
                "振幅%": round(float(stock.get("amplitude", 0)), 2),
                "成交量": int(stock.get("volume", 0)),
                "成交额(万)": round(float(stock.get("amount", 0)) / 10000, 1),
                "ma5_dev%": round(features.get("ma5_dev", 0), 2),
                "vol_vs_ma5": round(features.get("vol_vs_ma5", 0), 2),
                "score": s,
                "score_pct": pct,
                "数据源": data_source,
                "行情采集时间": data_captured_at,
            })

            if i > 0 and i % 200 == 0:
                logger.info(f"已处理 {i}/{total} ({i / total * 100:.0f}%)")

        except Exception as e:
            # 记录前5个错误以便排查，之后每200个记录一次避免日志刷屏
            if not hasattr(scan_stocks, "_error_count"):
                scan_stocks._error_count = 0  # type: ignore
            scan_stocks._error_count += 1  # type: ignore
            if scan_stocks._error_count <= 5 or i % 200 == 0:  # type: ignore
                logger.warning(f"处理 {code} 异常 ({scan_stocks._error_count}): {e}")  # type: ignore
            continue

    if not results:
        logger.warning("没有生成有效结果")
        return pd.DataFrame()

    # --- Step 4: 排序并输出 Top N ---
    df_results = pd.DataFrame(results)
    adaptive_info = {"adaptive_enabled": False, "settled_samples": 0}
    if paper_cfg.get("enabled", True):
        df_results, adaptive_info = apply_adaptive_scores(
            df_results,
            min_samples=int(paper_cfg.get("adaptive_min_samples", 30)),
            recent_days=int(paper_cfg.get("v3_recent_days", 60)),
            recent_weight=float(paper_cfg.get("v3_recent_weight", 0.6)),
            model_weight=float(paper_cfg.get("v3_model_weight", 0.35)),
        )
    sort_col = "final_score" if "final_score" in df_results.columns else "score"
    df_results = df_results.sort_values(sort_col, ascending=False).reset_index(drop=True)
    if adaptive_info.get("adaptive_enabled"):
        logger.info(
            f"V3复盘修正已启用: 已复盘样本 {adaptive_info['settled_samples']} 条，"
            f"正样本 {adaptive_info.get('positive_samples', 0)} 条，"
            f"风险规则 {adaptive_info.get('risk_rule_count', 0)} 条"
        )
    else:
        logger.info(
            f"V3复盘修正未启用: 已复盘样本 {adaptive_info.get('settled_samples', 0)} 条，"
            f"原因: {adaptive_info.get('reason', '样本不足')}，先使用规则分"
        )

    enrichment_cfg = config.get("enrichment", {})
    if enrichment_cfg.get("enabled", True):
        enrich_count = min(int(enrichment_cfg.get("candidate_count", 50)), len(df_results))
        logger.info(f"开始二次增强: 对 Top {enrich_count} 候选做同花顺/腾讯验证")
        enriched_top, enrich_info = enrich_candidates(df_results.head(enrich_count), enrichment_cfg)
        rest = df_results.iloc[enrich_count:].copy()
        if not rest.empty:
            rest["enrich_score"] = 0.0
            rest["enrich_notes"] = ""
        df_results = pd.concat([enriched_top, rest], ignore_index=True)
        base_final = df_results["final_score"] if "final_score" in df_results.columns else df_results["score"]
        df_results["final_score"] = base_final.astype(float) + df_results["enrich_score"].fillna(0).astype(float)
        df_results = df_results.sort_values("final_score", ascending=False).reset_index(drop=True)
        logger.info(
            f"二次增强完成: 同花顺源 {enrich_info.get('ths_sources', [])}，"
            f"腾讯分笔检查 {enrich_info.get('tick_checked', 0)} 只"
        )

    top_n = min(config.get("top_n", 10), len(df_results))
    df_top = df_results.head(top_n)

    # --- 输出结果 ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("⭐ 扫描完成！Top {} 候选股（评分越高越有潜力）".format(top_n))
    logger.info("=" * 60)

    print()
    print("╔" + "═" * 58 + "╗")
    print("║  📊 A股尾盘涨停扫描器 - {}".format(datetime.now().strftime("%Y-%m-%d")) + " " * (15 - len(datetime.now().strftime("%Y-%m-%d"))) + "  ║")
    print("╚" + "═" * 58 + "╝")
    print()

    # 格式化输出
    print("虚拟盘建议字段: 代码 / 名称 / 观察价 / 扫描时间 / 评分")
    print("-" * 62)
    for _, row in df_top.iterrows():
        display_score = row.get("final_score", row["score"])
        emoji = "🔥" if display_score >= 75 else "⭐" if display_score >= 60 else "📈"
        print(f"{emoji} {row['代码']}  {row['名称']}")
        print(f"   现价: ¥{row['最新价']}  涨跌: {row['涨跌幅%']}%  "
              f"量比: {row['量比']}  换手: {row['换手率%']}%  "
              f"振幅: {row['振幅%']}%")
        print(f"   评分: {row['score']}/105 ({row['score_pct']}%)  "
              f"量比MA5: {row['vol_vs_ma5']}  MA5偏离: {row['ma5_dev%']}%")
        if pd.notna(row.get("adaptive_score", pd.NA)):
            print(f"   V3修正分: {row['adaptive_score']:.1f}/105  最终分: {row['final_score']:.1f}/105")
        if row.get("v3_notes", ""):
            print(f"   V3提示: {row.get('v3_notes', '')}")
        if row.get("enrich_score", 0):
            print(f"   增强分: +{row['enrich_score']:.1f}  {row.get('enrich_notes', '')}")
        print()

    # --- Step 5: 保存CSV ---
    csv_path = BASE_DIR / f"results_{scan_time.strftime('%Y%m%d_%H%M%S')}.csv"
    df_top.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"完整结果已保存到: {csv_path}")

    # 也存个完整版
    df_results.to_csv(BASE_DIR / f"results_all_{scan_time.strftime('%Y%m%d_%H%M%S')}.csv",
                      index=False, encoding="utf-8-sig")

    if paper_cfg.get("enabled", True):
        added = append_signals(df_top, scan_time)
        logger.info(f"虚拟盘记录新增 {added} 条: {BASE_DIR / 'paper' / 'paper_trades.csv'}")

    return df_results


# ==================== 定时任务 ====================

def auto_scan(config):
    """每日定时扫描"""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        print("需要安装 APScheduler: pip install apscheduler")
        return

    scheduler = BlockingScheduler()
    scheduler.add_job(scan_stocks, "cron",
                     hour=config["scan_hour"],
                     minute=config["scan_minute"],
                     args=[config],
                     id="stock_scan",
                     replace_existing=True)

    logger.info(f"定时扫描已设置: 每天 {config['scan_hour']:02d}:{config['scan_minute']:02d}")
    logger.info("按 Ctrl+C 退出")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("定时扫描已停止")


def run_backtest(config, args):
    """Run the daily proxy backtest against cached history files."""
    top_n = int(args[1]) if len(args) > 1 else int(config.get("top_n", 20))
    data_dir = resolve_data_dir(config)
    logger.info(f"加载历史数据目录: {data_dir}")
    dataset = build_dataset(data_dir)
    if dataset.empty:
        logger.error("没有可用历史数据，无法回测")
        return

    cfg = BacktestConfig(
        top_n=top_n,
        min_price=float(config.get("filters", {}).get("min_price", 3)),
        max_price=float(config.get("filters", {}).get("max_price", 50)),
    )
    picks, metrics = backtest_rules(dataset, cfg)
    picks_path, metrics_path = save_backtest_report(picks, metrics)

    print("\n日线代理回测结果（非真实14:00买入回测）")
    print("=" * 52)
    print(f"样本行数: {len(dataset)}")
    print(f"交易日数: {metrics.get('days', 0)}")
    print(f"候选数量: {metrics.get('picks', 0)}")
    print(f"触及涨停率: {metrics.get('touch_limit_rate_pct', 0):.2f}%")
    print(f"收盘涨停率: {metrics.get('close_limit_rate_pct', 0):.2f}%")
    print(f"次日开盘胜率: {metrics.get('win_rate_pct', 0):.2f}%")
    print(f"平均次日开盘收益: {metrics.get('avg_daily_return_pct', 0):.3f}%")
    print(f"累计代理收益: {metrics.get('total_return_pct', 0):.2f}%")
    print(f"最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"\n候选明细: {picks_path}")
    print(f"指标报告: {metrics_path}")


def run_minute_backtest(config, args):
    """Run the minute-level backtest with proper time alignment."""
    top_n = int(args[1]) if len(args) > 1 else int(config.get("top_n", 20))
    data_dir = resolve_data_dir(config)
    logger.info(f"分钟级回测: 加载历史数据目录 {data_dir}")

    cfg = MinuteBacktestConfig(
        top_n=top_n,
        min_price=float(config.get("filters", {}).get("min_price", 3)),
        max_price=float(config.get("filters", {}).get("max_price", 50)),
    )

    dataset = build_minute_dataset(data_dir, cfg)
    if dataset.empty:
        logger.error("没有可用分钟级数据，无法回测")
        return

    logger.info(f"构建数据集: {len(dataset)} 行")

    picks, metrics = backtest_minute_rules(dataset, cfg)
    picks_path, metrics_path = save_minute_backtest_report(picks, metrics)

    print("\n分钟级回测结果（14:00选股 -> 次日9:30开盘验证收益）")
    print("=" * 60)
    print(f"样本行数: {len(dataset)}")
    print(f"交易日数: {metrics.get('days', 0)}")
    print(f"候选数量: {metrics.get('picks', 0)}")
    print(f"次日高开率(>0.5%): {metrics.get('strong_open_rate_pct', 0):.2f}%")
    print(f"次日开盘胜率: {metrics.get('win_rate_pct', 0):.2f}%")
    print(f"平均次日开盘收益: {metrics.get('avg_next_open_return_pct', 0):.3f}%")
    print(f"累计收益: {metrics.get('total_return_pct', 0):.2f}%")
    print(f"最大回撤: {metrics.get('max_drawdown_pct', 0):.2f}%")
    print(f"\n候选明细: {picks_path}")
    print(f"指标报告: {metrics_path}")


def run_minute_train(config, args):
    """Train ML model from minute-level dataset."""
    data_dir = resolve_data_dir(config)
    logger.info(f"分钟级模型训练: 加载数据目录 {data_dir}")

    cfg = MinuteBacktestConfig()
    dataset = build_minute_dataset(data_dir, cfg)
    if dataset.empty:
        logger.error("没有可用分钟级数据，无法训练")
        return

    logger.info(f"数据集: {len(dataset)} 行")
    metrics = train_minute_model(dataset)

    print("\n分钟级监督学习模型训练完成")
    print("=" * 45)
    for key, value in metrics.items():
        print(f"{key}: {value}")


def run_minute_candidates(config, args):
    """Rank candidates using minute-level model."""
    top_n = int(args[1]) if len(args) > 1 else int(config.get("top_n", 20))
    data_dir = resolve_data_dir(config)
    logger.info(f"分钟级候选排序: 加载数据目录 {data_dir}")

    cfg = MinuteBacktestConfig(top_n=top_n)
    dataset = build_minute_dataset(data_dir, cfg)
    if dataset.empty:
        logger.error("没有可用分钟级数据")
        return

    df = predict_with_minute_model(dataset)
    df["rule_score"] = rule_score_minute(df.tail(100)) if len(df) > 0 else 0

    latest_date = df["date"].max()
    latest = df[df["date"] == latest_date].copy()
    latest = latest.sort_values("ml_score", ascending=False, na_position="last").head(top_n)

    out_path = BASE_DIR / "reports" / "minute_candidates.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    latest.to_csv(out_path, index=False, encoding="utf-8-sig")

    cols = ["date", "code", "price", "change_pct_1400", "late_30m_return", "vol_acceleration", "rule_score", "ml_score"]
    view_cols = [c for c in cols if c in latest.columns]
    print("\n分钟级候选股（14:00时刻预测当天涨停概率）")
    print("=" * 70)
    print(latest[view_cols].to_string(index=False))
    print(f"\n已保存: {out_path}")


def run_train(config):
    """Train a LightGBM ranker/classifier from cached daily history."""
    data_dir = resolve_data_dir(config)
    logger.info(f"加载历史数据目录: {data_dir}")
    dataset = build_dataset(data_dir)
    if dataset.empty:
        logger.error("没有可用历史数据，无法训练")
        return
    metrics = train_lightgbm(dataset)
    print("\n监督学习模型训练完成")
    print("=" * 40)
    for key, value in metrics.items():
        print(f"{key}: {value}")


def run_candidates(config, args):
    """Rank latest cached daily-bar candidates with rule score and ML rank score."""
    top_n = int(args[1]) if len(args) > 1 else int(config.get("top_n", 20))
    data_dir = resolve_data_dir(config)
    dataset = build_dataset(data_dir)
    if dataset.empty:
        logger.error("没有可用历史数据，无法生成候选")
        return
    candidates = rank_latest_candidates(dataset, top_n=top_n)
    out_path = BASE_DIR / "reports" / "latest_candidates.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(out_path, index=False, encoding="utf-8-sig")

    cols = ["date", "code", "close", "change_pct", "turnover_rate", "volume_ratio_5", "rule_score", "ml_rank_score", "ml_raw_score"]
    view = candidates[cols].copy()
    view["date"] = view["date"].dt.strftime("%Y-%m-%d")
    view["ml_rank_score"] = view["ml_rank_score"].map(lambda x: "" if pd.isna(x) else f"{x:.4f}")
    view["ml_raw_score"] = view["ml_raw_score"].map(lambda x: "" if pd.isna(x) else f"{x:.4f}")
    print("\n最新缓存候选股（日线代理，不等于14:00实时信号）")
    print("=" * 64)
    print(view.to_string(index=False))
    print(f"\n已保存: {out_path}")


def run_paper_report():
    """Print paper trading performance summary."""
    report = paper_report()
    print("\n虚拟盘复盘报告")
    print("=" * 40)
    if report.get("rows", 0) == 0:
        print("暂无虚拟盘记录。先运行 python run.py scan。")
        print(f"台账路径: {report.get('path')}")
        return
    for key, value in report.items():
        if key == "path":
            continue
        if isinstance(value, float):
            print(f"{key}: {value:.3f}")
        else:
            print(f"{key}: {value}")
    print(f"台账路径: {report.get('path')}")


def run_explain(config, args):
    """Ask local Ollama to explain the latest scan or backtest candidates."""
    model = args[1] if len(args) > 1 else config.get("ollama_model", "qwen3.6:35b-a3b-q8_0")
    data_dir = resolve_data_dir(config)
    dataset = build_dataset(data_dir)
    if dataset.empty:
        logger.error("没有可解释的候选数据")
        return
    picks, _ = backtest_rules(dataset, BacktestConfig(top_n=int(config.get("top_n", 20))))
    latest = picks.sort_values("date").tail(int(config.get("top_n", 20)))
    fields = ["date", "code", "score", "change_pct", "turnover_rate", "volume_ratio_5", "return_5d", "ma5_dev"]
    candidates = latest[fields].assign(date=latest["date"].dt.strftime("%Y-%m-%d")).to_dict("records")
    try:
        from stock_screener.ollama_advisor import explain_candidates
        print(explain_candidates(candidates, model=model))
    except Exception as e:
        logger.error(f"Ollama 分析失败: {e}")
        print("请确认 Ollama 已启动，例如: ollama serve，并且本地已有对应模型。")


def run_ui(args):
    """Start the local web dashboard."""
    host = args[1] if len(args) > 1 else "127.0.0.1"
    port = int(args[2]) if len(args) > 2 else 3002
    from stock_screener.ui_server import run
    run(host=host, port=port)


# ==================== CLI --- 入口 ====================

def main(args=None):
    config = load_config()

    if not args or len(args) == 0:
        scan_stocks(config)
    elif args[0] in ("--help", "-h", "help"):
        print("""
A股尾盘涨停扫描器（改进版）
================
用法:
  python run.py                     立即执行一次扫描
  python run.py scan                立即执行一次扫描
  python run.py --auto              每日定时扫描

  日线代理回测（旧版，仅供对比）:
  python run.py backtest            使用缓存日线做代理回测
  python run.py train               训练日线涨停排序模型
  python run.py candidates          输出最新日线候选股

  分钟级回测（新版，时间对齐正确）:
  python run.py minute-backtest     分钟级回测（14:00选股 -> 当天涨停验证）
  python run.py minute-train        训练分钟级ML模型
  python run.py minute-candidates   输出分钟级候选股（带ML概率排序）

  其他:
  python run.py paper-report        查看虚拟盘命中率和收益
  python run.py explain             使用本地Ollama解释候选股
  python run.py ui                  打开本地Web工作台
  python run.py --help              显示帮助

配置: 编辑 stock-screener/config.json
  - scan_hour/minute: 扫描时间（默认14:00）
  - top_n: 返回候选股数量（默认10）
  - backtrack_days: 历史数据回溯天数
  - filters: 价格区间、排除ST等
        """)
    elif args[0] in ("scan", "run"):
        scan_stocks(config)
    elif args[0] in ("--auto", "auto"):
        auto_scan(config)
    elif args[0] in ("backtest", "bt"):
        run_backtest(config, args)
    elif args[0] in ("train", "fit"):
        run_train(config)
    elif args[0] in ("candidates", "pick"):
        run_candidates(config, args)
    elif args[0] in ("minute-backtest", "minute-bt", "mbt"):
        run_minute_backtest(config, args)
    elif args[0] in ("minute-train", "mtrain", "mfit"):
        run_minute_train(config, args)
    elif args[0] in ("minute-candidates", "mcandidates", "mc"):
        run_minute_candidates(config, args)
    elif args[0] in ("paper-report", "paper"):
        run_paper_report()
    elif args[0] in ("explain", "ollama"):
        run_explain(config, args)
    elif args[0] in ("ui", "web"):
        run_ui(args)
    else:
        print(f"未知命令: {args[0]}")
        print("运行 python run.py --help 查看用法")


if __name__ == "__main__":
    main(sys.argv[1:])
