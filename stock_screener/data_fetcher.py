#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据获取层

负责从 AKShare / 直连 HTTP 拉取所有需要的数据，带缓存和降级机制。
数据源分层：腾讯直连 > 新浪 > 旧缓存应急
"""

import os
import json
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Union

import pandas as pd
import numpy as np
from loguru import logger
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import akshare as ak
except ImportError:
    ak = None

import requests as _requests

# =====================================================================
# 全局注入 requests.Session，解决东方财富/新浪 API 频繁断连问题
# =====================================================================
# akshare 底层使用 requests，macOS 上东方财富会频繁关闭空闲连接
# 通过 monkey-patch requests.Session 来添加 retry adapter
_imported_requests = False


def _inject_resilient_session():
    """在模块加载时注入带有重试机制的 requests Session。"""
    global _imported_requests
    if _imported_requests:
        return
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry_strategy = Retry(
            total=5,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10,
        )
        session = requests.Session()
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Monkey-patch the default session used by requests
        requests.sessions.get_adapter = lambda self, url: adapter  # type: ignore
        # Also patch the module-level default session
        requests.adapters.DEFAULT_ADAPTER = adapter  # type: ignore

        _imported_requests = True
        logger.info("已注入弹性 requests Session（5次重试，指数退避）")
    except Exception as e:
        logger.warning(f"注入弹性 Session 失败: {e}（将使用默认 requests 配置）")


_inject_resilient_session()


# ==================== 配置 ====================

def resolve_data_dir(config_or_base: Union[dict, Path]) -> Path:
    """解析数据目录路径"""
    if isinstance(config_or_base, Path):
        return config_or_base
    data_dir = config_or_base.get("resolved_data_dir", str(Path(__file__).parent.parent / "data"))
    return Path(data_dir)


# ==================== 缓存管理器 ====================

class CacheManager:
    """统一管理数据缓存，带过期时间"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        return self.data_dir / f"cache_{key}.csv"

    def read(self, key: str, today_only: bool = False,
             max_age_seconds: Optional[int] = None) -> tuple:
        """
        读取缓存，返回 (df, cached_at, age_seconds)
        df 为空表示缓存失效或不存在
        """
        cache_file = self._cache_path(key)
        if not cache_file.exists():
            return pd.DataFrame(), None, None

        try:
            df = pd.read_csv(cache_file)
            cached_at_str = df["__cached_at"].iloc[0] if "__cached_at" in df.columns else None
            cached_at = pd.to_datetime(cached_at_str, errors="coerce") if cached_at_str else None
            if cached_at is not None and pd.isna(cached_at):
                cached_at = None

            age_seconds = None
            if cached_at is not None:
                age_seconds = (datetime.now() - cached_at.to_pydatetime()).total_seconds()

            if today_only and (cached_at is None or cached_at.strftime("%Y-%m-%d") != datetime.now().strftime("%Y-%m-%d")):
                return pd.DataFrame(), cached_at, age_seconds
            if max_age_seconds is not None and (age_seconds is None or age_seconds > max_age_seconds):
                return pd.DataFrame(), cached_at, age_seconds

            return df, cached_at, age_seconds
        except Exception as e:
            logger.warning(f"读取缓存失败 [{key}]: {e}")
            return pd.DataFrame(), None, None

    def write(self, key: str, df: pd.DataFrame) -> None:
        """写入缓存，附带时间戳"""
        cache_file = self._cache_path(key)
        df = df.copy()
        df["__cached_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df.to_csv(cache_file, index=False, encoding="utf-8-sig")

    def read_history(self, key: str) -> pd.DataFrame:
        """读取历史数据（不检查是否当天有效，用于日K/分钟K等历史数据）"""
        cache_file = self._cache_path(key)
        if not cache_file.exists():
            return pd.DataFrame()
        try:
            return pd.read_csv(cache_file)
        except Exception as e:
            logger.warning(f"读取历史缓存失败 [{key}]: {e}")
            return pd.DataFrame()

    def write_history(self, key: str, df: pd.DataFrame) -> None:
        """写入历史数据"""
        cache_file = self._cache_path(key)
        df.to_csv(cache_file, index=False, encoding="utf-8-sig")


# ==================== 数据获取器 ====================

class DataFetcher:
    """统一数据获取器"""

    def __init__(self, config: dict):
        self.config = config
        self.data_dir = resolve_data_dir(config)
        self.cache = CacheManager(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._sector_cache: dict[str, str] = {}
        self._name_cache: dict[str, str] = {}
        self._load_name_cache()

    def _load_name_cache(self) -> None:
        path = self.data_dir / "stock_names.json"
        if path.exists():
            try:
                self._name_cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self._name_cache = {}

    def _save_name_cache(self) -> None:
        path = self.data_dir / "stock_names.json"
        path.write_text(json.dumps(self._name_cache, ensure_ascii=False), encoding="utf-8")

    def get_stock_name(self, code: str) -> str:
        code = self.normalize_code(code)
        if code in self._name_cache:
            return self._name_cache[code]
        return ""

    def set_stock_name(self, code: str, name: str) -> None:
        code = self.normalize_code(code)
        if code and name:
            self._name_cache[code] = name

    def batch_resolve_names(self, codes: list[str]) -> dict[str, str]:
        unknown = [c for c in codes if self.normalize_code(c) not in self._name_cache]
        if unknown:
            try:
                if ak is not None:
                    df = ak.stock_info_a_code_name()
                    if df is not None and not df.empty:
                        for _, row in df.iterrows():
                            c = self.normalize_code(str(row.get("code", "")))
                            n = str(row.get("name", ""))
                            if c and n:
                                self._name_cache[c] = n
                    self._save_name_cache()
            except Exception as e:
                logger.warning(f"批量获取股票名称失败: {e}")

        resolved = {}
        for c in codes:
            nc = self.normalize_code(c)
            if nc in self._name_cache:
                resolved[nc] = self._name_cache[nc]
        return resolved

    # ---- 通用工具 ----

    @staticmethod
    def normalize_code(code: str) -> str:
        """标准化股票代码为6位"""
        text = "".join(ch for ch in str(code).strip() if ch.isdigit())
        return text.zfill(6)[-6:] if text else ""

    def _safe_fetch(self, fetch_func, source_name: str, max_retries: int = 3,
                    retry_delay: float = 2.0) -> Optional[pd.DataFrame]:
        """带重试的通用抓取"""
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                df = fetch_func()
                if df is not None and not df.empty:
                    logger.info(f"成功从{source_name}获取数据: {len(df)} 条")
                    return df
                last_error = f"{source_name} 返回空数据"
            except Exception as e:
                last_error = e
                logger.warning(f"{source_name} 获取失败({attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
        logger.warning(f"{source_name} 所有重试均失败: {last_error}")
        return None

    # ---- 分钟K线 ----

    def get_minute_history(self, code: str, period: str = "5",
                           days: int = 60) -> pd.DataFrame:
        """
        获取个股分钟级K线

        Args:
            code: 股票代码（6位）
            period: 周期 "1"/"5"/"15"/"30"/"60"
            days: 回溯天数
        Returns:
            DataFrame with columns: 时间, 开盘, 收盘, 最高, 最低, 成交量, 成交额, ...
        """
        if ak is None:
            logger.error("akshare 未安装")
            return pd.DataFrame()

        code = self.normalize_code(code)
        if not code:
            return pd.DataFrame()

        cache_key = f"minute_{code}_{period}_{days}"
        cached = self.cache.read_history(cache_key)
        if len(cached) >= 20:
            return cached

        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")

        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust="",
            )
            if df is not None and not df.empty:
                self.cache.write_history(cache_key, df)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"获取 {code} {period}min K 线失败: {e}")
            return cached if len(cached) > 0 else pd.DataFrame()

    def get_minute_history_by_date(self, code: str, date: str,
                                   period: str = "5") -> pd.DataFrame:
        """
        获取指定日期的分钟级K线

        Args:
            code: 股票代码（6位）
            date: 日期字符串 "YYYY-MM-DD" 或 "YYYYMMDD"
            period: 周期 "1"/"5"/"15"/"30"/"60"
        Returns:
            DataFrame
        """
        if ak is None:
            logger.error("akshare 未安装")
            return pd.DataFrame()

        code = self.normalize_code(code)
        if not code:
            return pd.DataFrame()

        date_fmt = date.replace("-", "")
        cache_key = f"minute_{code}_{period}_{date_fmt}"
        cached = self.cache.read_history(cache_key)
        if len(cached) >= 10:
            return cached

        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                start_date=date,
                end_date=date,
                adjust="",
            )
            if df is not None and not df.empty:
                self.cache.write_history(cache_key, df)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"获取 {code} {date} {period}min K 线失败: {e}")
            return cached if len(cached) > 0 else pd.DataFrame()

    # ---- 日K线 ----

    def get_history(self, code: str, days: int = 180) -> pd.DataFrame:
        """
        获取个股日K线（前复权）—— 腾讯主源，新浪备用
        """
        code = self.normalize_code(code)
        if not code:
            return pd.DataFrame()

        cache_key = f"history_{code}_{days}"
        cached = self.cache.read_history(cache_key)
        if len(cached) >= 10:
            return cached

        # 主源：腾讯
        df = self._fetch_history_tencent(code, days)
        if not df.empty:
            self.cache.write_history(cache_key, df)
            return df

        # 备用：新浪
        if ak is not None:
            try:
                end_date = datetime.now().strftime("%Y%m%d")
                start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
                df = ak.stock_zh_a_daily(
                    symbol=self._to_tx_prefix(code),
                    start_date=start_date, end_date=end_date,
                    adjust="qfq",
                )
                if df is not None and not df.empty:
                    self.cache.write_history(cache_key, df)
                    return df
            except Exception as e:
                logger.warning(f"获取 {code} 新浪日K失败: {e}")

        return cached if len(cached) > 0 else pd.DataFrame()

    def _fetch_history_tencent(self, code: str, days: int = 180) -> pd.DataFrame:
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
            return pd.DataFrame(rows)
        except Exception as e:
            logger.debug(f"{code} 腾讯日K失败: {e}")
            return pd.DataFrame()


    # ---- 全市场实时行情 ----

    # 腾讯行情 API 字段索引（qt.gtimg.cn，~分隔）
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

    def _get_all_codes_tx(self) -> list[str]:
        """获取全量A股代码列表，返回腾讯格式 (sh/sz/bj + 6位)"""
        cache_file = self.data_dir / "cache_all_codes.json"
        stale_codes: list[str] = []
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                cached_at = datetime.fromisoformat(payload["cached_at"])
                if (datetime.now() - cached_at).total_seconds() < 86400:
                    return payload["codes"]
                stale_codes = payload.get("codes", [])
            except Exception:
                pass

        if ak is None:
            return stale_codes
        try:
            df = ak.stock_info_a_code_name()
            codes = [
                self._to_tx_prefix(str(r).zfill(6))
                for r in df["code"].tolist()
            ]
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

    def _fetch_tencent_batch(self, batch: list[str]) -> list[dict]:
        """拉取一批腾讯行情，返回行列表"""
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
                raw = line.split('"')[1]
                parts = raw.split("~")
                if len(parts) < 50 or not parts[3]:
                    continue
                row = {}
                for col, idx in self._TX_FIELDS.items():
                    row[col] = parts[idx] if idx < len(parts) else ""
                row["code"] = DataFetcher.normalize_code(row["code"])
                # amount 原始单位是万元，转换为元与其他数据源一致
                try:
                    row["amount"] = float(row["amount"]) * 10000
                except (ValueError, TypeError):
                    row["amount"] = 0.0
                rows.append(row)
            except Exception:
                continue
        return rows

    def _fetch_tencent_realtime(self) -> Optional[pd.DataFrame]:
        """腾讯直连批量获取全市场行情（并发5线程，约 3-5 秒完成）"""
        codes = self._get_all_codes_tx()
        if not codes:
            logger.warning("腾讯行情: 股票代码列表为空，跳过")
            return None

        batch_size = 100
        batches = [codes[i: i + batch_size] for i in range(0, len(codes), batch_size)]
        all_rows: list[dict] = []
        failed = 0

        try:
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(self._fetch_tencent_batch, b): b for b in batches}
                for fut in as_completed(futures):
                    rows = fut.result()
                    if rows:
                        all_rows.extend(rows)
                    else:
                        failed += 1
        except Exception as e:
            logger.warning(f"腾讯行情并发拉取异常: {e}")
            return None

        logger.info(f"腾讯行情批次: 成功 {len(batches)-failed}/{len(batches)}, 得到 {len(all_rows)} 条")
        if not all_rows:
            logger.warning("腾讯行情: 所有批次均返回空数据")
            return None

        df = pd.DataFrame(all_rows)
        num_cols = [c for c in df.columns if c != "name" and c != "code"]
        for col in num_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df["name"] = df["name"].fillna("")
        df["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df["数据源"] = "腾讯"
        logger.info(f"腾讯行情获取成功: {len(df)} 条")
        return df

    def get_all_realtime(self) -> pd.DataFrame:
        """获取全市场实时行情（腾讯直连优先，新浪备用）"""
        logger.info("正在获取全市场实时数据...")
        ttl_seconds = int(self.config.get("realtime_cache_ttl_seconds", 120))

        cached_df, cached_at, cache_age = self.cache.read(
            "realtime", today_only=True, max_age_seconds=ttl_seconds
        )
        if not cached_df.empty:
            logger.info(f"使用缓存的实时数据: {cached_at}，缓存年龄 {self._format_cache_age(cache_age)}")
            return cached_df
        if cached_at is not None:
            logger.info(f"实时缓存已过期: {cached_at}，重新拉取")

        # 腾讯直连（主力）
        try:
            df = self._fetch_tencent_realtime()
        except Exception as e:
            logger.warning(f"腾讯行情异常: {e}")
            df = None
        if df is not None and not df.empty:
            self.cache.write("realtime", df)
            return df

        # 新浪（备用）—— 缺量比和换手率，填0托底
        logger.warning("腾讯行情失败，降级到新浪")
        if ak is not None:
            sina_df = self._safe_fetch(ak.stock_zh_a_spot, "新浪")
            if sina_df is not None and not sina_df.empty:
                sina_df = self._normalize_realtime(sina_df, "新浪")
                self.cache.write("realtime", sina_df)
                return sina_df

        # 全部失败，使用旧缓存
        stale_df, stale_at, stale_age = self.cache.read("realtime", today_only=False)
        if not stale_df.empty:
            logger.error(f"实时行情接口全部失败，使用最近缓存: {stale_at}，缓存年龄 {self._format_cache_age(stale_age)}")
            return stale_df

        logger.error("获取实时行情全部失败")
        return pd.DataFrame()

    @staticmethod
    def _normalize_realtime(df: pd.DataFrame, source: str) -> pd.DataFrame:
        """统一实时行情列名（用于新浪等 AKShare 数据源）"""
        col_map = {
            "代码": "code", "名称": "name", "最新价": "last_price",
            "涨跌幅": "change_pct", "涨跌额": "change_amt", "成交量": "volume",
            "成交额": "amount", "振幅": "amplitude", "最高": "high",
            "最低": "low", "今开": "open_price", "昨收": "pre_close",
            "量比": "volume_ratio", "换手率": "turnover_rate",
        }
        df = df.rename(columns=col_map)
        if "code" in df.columns:
            df["code"] = df["code"].apply(DataFetcher.normalize_code)
        df["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        df["数据源"] = source
        if "序号" in df.columns:
            df.drop(columns=["序号"], inplace=True)

        defaults = {
            "name": "", "last_price": 0, "change_pct": 0, "change_amt": 0,
            "volume": 0, "amount": 0, "amplitude": 0, "high": 0, "low": 0,
            "open_price": 0, "pre_close": 0, "volume_ratio": 0, "turnover_rate": 0,
        }
        for col, default in defaults.items():
            if col not in df.columns:
                df[col] = default
            else:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(default if col != "name" else "")
        return df

    @staticmethod
    def _format_cache_age(age_seconds) -> str:
        if age_seconds is None or pd.isna(age_seconds):
            return "未知"
        return f"{age_seconds:.0f}s"

    # ---- 涨停股池 ----

    def get_limit_up_pool(self, date: Optional[str] = None) -> pd.DataFrame:
        """
        获取涨停股池

        Args:
            date: 日期字符串 "YYYYMMDD"，默认今天
        Returns:
            DataFrame: 涨停股票列表
        """
        if ak is None:
            return pd.DataFrame()

        if date is None:
            date = datetime.now().strftime("%Y%m%d")

        cache_key = f"zt_pool_{date}"
        cached = self.cache.read_history(cache_key)
        if len(cached) > 0:
            return cached

        try:
            df = ak.stock_zt_pool_em(date=date)
            if df is not None and not df.empty:
                self.cache.write_history(cache_key, df)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"获取涨停股池失败 ({date}): {e}")
            return pd.DataFrame()

    def get_limit_up_previous(self, date: Optional[str] = None) -> pd.DataFrame:
        """获取前一日涨停股池（用于回测）"""
        if ak is None:
            return pd.DataFrame()
        if date is None:
            date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        else:
            date = date.replace("-", "")

        cache_key = f"zt_pool_prev_{date}"
        cached = self.cache.read_history(cache_key)
        if len(cached) > 0:
            return cached

        try:
            df = ak.stock_zt_pool_previous_em(date=date)
            if df is not None and not df.empty:
                self.cache.write_history(cache_key, df)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"获取前日涨停股池失败 ({date}): {e}")
            return pd.DataFrame()

    # ---- 板块数据 ----

    def get_all_concept_sectors(self) -> pd.DataFrame:
        """获取全部概念板块实时行情"""
        cache_key = "sectors_concept"
        cached, _, _ = self.cache.read(cache_key, today_only=True, max_age_seconds=300)
        if not cached.empty:
            return cached

        if ak is None:
            return pd.DataFrame()

        try:
            # 获取概念板块列表
            names_df = ak.stock_board_concept_name_em()
            if names_df is None or names_df.empty:
                return pd.DataFrame()

            all_rows = []
            for _, row in names_df.iterrows():
                try:
                    name = row.get("板块名称", "")
                    code = row.get("板块代码", "")
                    if not name or not code:
                        continue
                    self._sector_cache[code] = name
                    sector_df = ak.stock_board_concept_spot_em(symbol=code)
                    if sector_df is not None and not sector_df.empty:
                        sector_df["板块名称"] = name
                        sector_df["板块代码"] = code
                        all_rows.append(sector_df.iloc[0])  # 取第一条（汇总行）
                except Exception:
                    continue

            if all_rows:
                result = pd.DataFrame(all_rows)
                self.cache.write(cache_key, result)
                return result
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"获取概念板块数据失败: {e}")
            return pd.DataFrame()

    def get_sector_by_stock(self, code: str) -> list:
        """
        获取某只股票所属的概念板块
        注意：这个需要单独 API，简化版返回空列表
        后续可以接入东财个股-板块接口
        """
        return []

    # ---- 大盘情绪指标 ----

    def get_market_sentiment(self) -> dict:
        """
        获取大盘情绪指标
        返回字典: 涨跌家数、涨停家数、跌停家数、成交额等
        """
        cache_key = "market_sentiment"
        cached, _, _ = self.cache.read(cache_key, today_only=True, max_age_seconds=300)
        if not cached.empty:
            return dict(cached.iloc[0])

        if ak is None:
            return {}

        sentiment = {}
        today = datetime.now().strftime("%Y%m%d")

        try:
            # 涨停股池统计
            zt_df = self.get_limit_up_pool(today)
            sentiment["limit_up_count"] = len(zt_df) if not zt_df.empty else 0

            # 跌停股池统计
            try:
                dt_df = ak.stock_zh_a_stop_em()  # 涨跌停
                if not dt_df.empty:
                    sentiment["limit_down_count"] = len(dt_df)
                else:
                    sentiment["limit_down_count"] = 0
            except Exception:
                sentiment["limit_down_count"] = 0

            # 涨跌家数（从全市场实时数据计算）
            realtime = self.get_all_realtime()
            if not realtime.empty:
                sentiment["total_stocks"] = len(realtime)
                sentiment["rise_count"] = len(realtime[realtime["change_pct"] > 0])
                sentiment["fall_count"] = len(realtime[realtime["change_pct"] < 0])
                sentiment["flat_count"] = len(realtime[realtime["change_pct"] == 0])
                sentiment["total_amount"] = float(realtime["amount"].sum())
                sentiment["avg_change_pct"] = float(realtime["change_pct"].mean())
            else:
                sentiment["total_stocks"] = 0
                sentiment["rise_count"] = 0
                sentiment["fall_count"] = 0
                sentiment["flat_count"] = 0
                sentiment["total_amount"] = 0.0
                sentiment["avg_change_pct"] = 0.0

            # 保存缓存
            sentiment_df = pd.DataFrame([sentiment])
            self.cache.write(cache_key, sentiment_df)

            return sentiment
        except Exception as e:
            logger.warning(f"获取大盘情绪数据失败: {e}")
            # 尝试读旧缓存
            old_cached, _, _ = self.cache.read(cache_key, today_only=False)
            if not old_cached.empty:
                return dict(old_cached.iloc[0])
            return {}

    # ---- 标准化历史数据 ----

    @staticmethod
    def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化日K数据列名
        输入: AKShare 原始日K
        输出: 统一列名
        """
        if df.empty:
            return df
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "涨跌幅": "change_pct", "涨跌额": "change_amt",
            "换手率": "turnover_rate",
        }
        df = df.rename(columns=col_map)
        required = ["date", "open", "close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return df
        for c in required:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        for c in ["change_pct", "change_amt", "turnover_rate", "amount"]:
            if c not in df.columns:
                df[c] = 0
            else:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        return df

    @staticmethod
    def normalize_minute(df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化分钟K数据列名
        """
        if df.empty:
            return df
        col_map = {
            "时间": "datetime", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "涨跌幅": "change_pct", "涨跌额": "change_amt",
            "换手率": "turnover_rate", "振幅": "amplitude",
        }
        df = df.rename(columns=col_map)
        required = ["datetime", "open", "close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return df
        for c in required:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        for c in ["change_pct", "change_amt", "turnover_rate", "amplitude", "amount"]:
            if c not in df.columns:
                df[c] = 0
            else:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df
