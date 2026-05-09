#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_fetcher 单元测试（mock 网络请求）
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from stock_screener.data_fetcher import DataFetcher, CacheManager


# ====================  fixtures ====================

def _mock_realtime_df():
    return pd.DataFrame({
        "代码": ["000001", "000002", "600000"],
        "名称": ["平安银行", "浦发银行", "浦发银行"],
        "最新价": [10.5, 8.3, 12.0],
        "涨跌幅": [3.5, -1.2, 5.1],
        "涨跌额": [0.35, -0.1, 0.56],
        "成交量": [500000, 300000, 800000],
        "成交额": [525000000, 249000000, 960000000],
        "振幅": [2.1, 1.5, 3.0],
        "最高": [10.8, 8.4, 12.3],
        "最低": [10.3, 8.2, 11.8],
        "今开": [10.2, 8.4, 11.9],
        "昨收": [10.15, 8.4, 11.44],
        "量比": [1.8, 0.9, 2.5],
        "换手率": [3.2, 2.1, 4.5],
    })


def _mock_minute_df():
    rows = []
    for h in [9, 10, 11, 13, 14, 15]:
        for m in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]:
            if h == 9 and m < 35:
                continue
            if h == 11 and m > 30:
                break
            rows.append({
                "时间": f"2026-05-08 {h:02d}:{m:02d}:00",
                "开盘": 10.0 + (h - 9) * 0.1 + m * 0.001,
                "收盘": 10.0 + (h - 9) * 0.1 + m * 0.002,
                "最高": 10.5 + (h - 9) * 0.1,
                "最低": 9.8 + (h - 9) * 0.1,
                "成交量": 20000 + h * 1000,
                "成交额": 20000000 + h * 10000000,
                "涨跌幅": 1.0 + h * 0.3 + m * 0.01,
                "涨跌额": 0.1 + h * 0.05,
                "换手率": 2.0 + h * 0.2,
                "振幅": 1.0 + h * 0.1,
            })
    return pd.DataFrame(rows)


def _mock_daily_df():
    rows = []
    for d in range(1, 31):
        rows.append({
            "日期": f"2026-04-{d:02d}",
            "开盘": 10.0 + d * 0.05,
            "收盘": 10.0 + d * 0.08,
            "最高": 10.2 + d * 0.06,
            "最低": 9.8 + d * 0.04,
            "成交量": 500000 + d * 10000,
            "成交额": 500000000 + d * 50000000,
            "涨跌幅": 0.5 + d * 0.1,
            "涨跌额": 0.05 + d * 0.01,
            "换手率": 2.0 + d * 0.1,
        })
    return pd.DataFrame(rows)


# ==================== CacheManager 测试 ====================

def test_cache_write_read():
    tmpdir = Path("/tmp/test_cache_manager")
    tmpdir.mkdir(exist_ok=True)
    cm = CacheManager(tmpdir)
    df = _mock_realtime_df()
    cm.write("test_key", df)
    result, cached_at, age = cm.read("test_key", today_only=True)
    assert len(result) == len(df), f"Expected {len(df)} rows, got {len(result)}"
    assert cached_at is not None
    assert age is not None and age >= 0
    print("test_cache_write_read: PASS")
    # cleanup
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


def test_cache_expired():
    tmpdir = Path("/tmp/test_cache_expired")
    tmpdir.mkdir(exist_ok=True)
    cm = CacheManager(tmpdir)
    df = _mock_realtime_df()
    cm.write("test_key", df)
    # 强制过期：max_age_seconds=0
    result, _, _ = cm.read("test_key", today_only=True, max_age_seconds=0)
    assert result.empty, "Cache should be expired"
    print("test_cache_expired: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


def test_cache_not_today():
    tmpdir = Path("/tmp/test_cache_not_today")
    tmpdir.mkdir(exist_ok=True)
    cm = CacheManager(tmpdir)
    df = _mock_realtime_df()
     # 手动写入昨天时间的缓存
    df = df.copy()
    df["__cached_at"] = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    cache_file = tmpdir / "cache_test_key.csv"
    df.to_csv(cache_file, index=False, encoding="utf-8-sig")
     # today_only=True 但缓存是昨天的 → 应返回空
    result, _, _ = cm.read("test_key", today_only=True, max_age_seconds=999999)
    assert result.empty, "Cache from yesterday should be expired with today_only=True"
    print("test_cache_not_today: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()

def test_history_cache():
    tmpdir = Path("/tmp/test_history_cache")
    tmpdir.mkdir(exist_ok=True)
    cm = CacheManager(tmpdir)
    df = _mock_daily_df()
    cm.write_history("hist_key", df)
    result = cm.read_history("hist_key")
    assert len(result) == len(df)
    print("test_history_cache: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


# ==================== DataFetcher 测试 ====================

def test_normalize_code():
    f = DataFetcher({})
    assert f.normalize_code("000001") == "000001"
    assert f.normalize_code("1") == "000001"
    assert f.normalize_code("600000") == "600000"
    assert f.normalize_code("abc") == ""
    print("test_normalize_code: PASS")


def test_normalize_history():
    raw = pd.DataFrame({
        "日期": ["2026-04-01", "2026-04-02"],
        "开盘": [10.0, 10.1],
        "收盘": [10.5, 10.6],
        "最高": [10.8, 10.9],
        "最低": [9.9, 10.0],
        "成交量": [500000, 600000],
    })
    result = DataFetcher.normalize_history(raw)
    assert "date" in result.columns
    assert "open" in result.columns
    assert "close" in result.columns
    assert len(result) == 2
    print("test_normalize_history: PASS")


def test_normalize_minute():
    raw = pd.DataFrame({
        "时间": ["2026-05-08 09:35:00", "2026-05-08 09:40:00"],
        "开盘": [10.0, 10.1],
        "收盘": [10.5, 10.6],
        "最高": [10.8, 10.9],
        "最低": [9.9, 10.0],
        "成交量": [50000, 60000],
    })
    result = DataFetcher.normalize_minute(raw)
    assert "datetime" in result.columns
    assert "open" in result.columns
    assert "close" in result.columns
    assert len(result) == 2
    print("test_normalize_minute: PASS")


def test_normalize_realtime():
    raw = pd.DataFrame({
        "代码": ["000001", "600000"],
        "名称": ["平安银行", "浦发银行"],
        "最新价": [10.5, 12.0],
        "涨跌幅": [3.5, -1.2],
        "成交量": [500000, 800000],
        "成交额": [525000000, 960000000],
    })
    result = DataFetcher._normalize_realtime(raw, "test")
    assert "code" in result.columns
    assert "last_price" in result.columns
    assert "数据源" in result.columns
    assert result["数据源"].iloc[0] == "test"
    assert result["code"].iloc[0] == "000001"
    print("test_normalize_realtime: PASS")


@patch("stock_screener.data_fetcher.ak")
def test_get_minute_history(mock_ak):
    mock_ak.stock_zh_a_hist_min_em.return_value = _mock_minute_df()
    tmpdir = Path("/tmp/test_fetcher_minute")
    tmpdir.mkdir(exist_ok=True)
    fetcher = DataFetcher({"resolved_data_dir": str(tmpdir)})
    df = fetcher.get_minute_history("000001", period="5", days=5)
    assert len(df) > 0, "Should return minute data"
    assert "时间" in df.columns or "datetime" in df.columns
    print("test_get_minute_history: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


@patch("stock_screener.data_fetcher.ak")
def test_get_history(mock_ak):
    mock_ak.stock_zh_a_hist.return_value = _mock_daily_df()
    tmpdir = Path("/tmp/test_fetcher_daily")
    tmpdir.mkdir(exist_ok=True)
    fetcher = DataFetcher({"resolved_data_dir": str(tmpdir)})
    df = fetcher.get_history("000001", days=30)
    assert len(df) > 0, "Should return daily data"
    print("test_get_history: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


@patch("stock_screener.data_fetcher.ak")
def test_get_all_realtime(mock_ak):
    mock_ak.stock_zh_a_spot_em.return_value = _mock_realtime_df()
    tmpdir = Path("/tmp/test_fetcher_realtime")
    tmpdir.mkdir(exist_ok=True)
    fetcher = DataFetcher({"resolved_data_dir": str(tmpdir), "realtime_cache_ttl_seconds": 120})
    df = fetcher.get_all_realtime()
    assert len(df) == 3, f"Expected 3 stocks, got {len(df)}"
    assert "code" in df.columns
    assert "数据源" in df.columns
    print("test_get_all_realtime: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


@patch("stock_screener.data_fetcher.ak")
def test_realtime_fallback_to_sina(mock_ak):
    """东方财富失败时应回退到新浪"""
    mock_ak.stock_zh_a_spot_em.side_effect = Exception("connection refused")
    mock_ak.stock_zh_a_spot.return_value = _mock_realtime_df()
    tmpdir = Path("/tmp/test_fetcher_fallback")
    tmpdir.mkdir(exist_ok=True)
    fetcher = DataFetcher({"resolved_data_dir": str(tmpdir), "realtime_cache_ttl_seconds": 120})
    df = fetcher.get_all_realtime()
    assert len(df) > 0, "Should fallback to Sina"
    assert df["数据源"].iloc[0] == "新浪"
    print("test_realtime_fallback_to_sina: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


@patch("stock_screener.data_fetcher.ak")
def test_realtime_fallback_to_stale_cache(mock_ak):
    """两个实时接口都失败时应回退到旧缓存"""
    mock_ak.stock_zh_a_spot_em.side_effect = Exception("connection refused")
    mock_ak.stock_zh_a_spot.side_effect = Exception("connection refused")
    tmpdir = Path("/tmp/test_fetcher_stale")
    tmpdir.mkdir(exist_ok=True)
    fetcher = DataFetcher({"resolved_data_dir": str(tmpdir), "realtime_cache_ttl_seconds": 1})
    # 先写一个旧缓存
    df = _mock_realtime_df()
    df["数据源"] = "东方财富"
    cache_file = tmpdir / "cache_realtime.csv"
    df["__cached_at"] = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    df.to_csv(cache_file, index=False, encoding="utf-8-sig")
    result = fetcher.get_all_realtime()
    assert len(result) > 0, "Should fallback to stale cache"
    assert result["数据源"].iloc[0] == "东方财富"
    print("test_realtime_fallback_to_stale_cache: PASS")
    for f in tmpdir.glob("cache_*"):
        f.unlink()
    tmpdir.rmdir()


# ==================== 运行 ====================

if __name__ == "__main__":
    tests = [
        test_cache_write_read,
        test_cache_expired,
        test_cache_not_today,
        test_history_cache,
        test_normalize_code,
        test_normalize_history,
        test_normalize_minute,
        test_normalize_realtime,
        test_get_minute_history,
        test_get_history,
        test_get_all_realtime,
        test_realtime_fallback_to_sina,
        test_realtime_fallback_to_stale_cache,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n{'=' * 40}")
    print(f"结果: {passed} 通过, {failed} 失败, 共 {passed + failed} 个")
