#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""features.py 单元测试"""

import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from stock_screener.features import (
     calc_minute_features,
     calc_daily_features,
     calc_sentiment_features,
     build_features,
     features_to_dataframe,
)


def _mock_minute_df():
     rows = []
     for h in [9, 10, 11, 13, 14]:
         for m in [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]:
             if h == 9 and m < 35:
                 continue
             if h == 11 and m > 30:
                 break
             rows.append({
                  "datetime": f"2026-05-08 {h:02d}:{m:02d}:00",
                  "open": 10.0 + (h - 9) * 0.1 + m * 0.001,
                  "close": 10.0 + (h - 9) * 0.1 + m * 0.002 + np.random.randn() * 0.05,
                  "high": 10.5 + (h - 9) * 0.1,
                  "low": 9.8 + (h - 9) * 0.1,
                  "volume": 20000 + h * 1000 + abs(np.random.randn()) * 5000,
                  "amount": 20000000 + h * 10000000,
             })
     return pd.DataFrame(rows)


def _mock_daily_df():
     rows = []
     for d in range(1, 31):
         rows.append({
              "date": f"2026-04-{d:02d}",
              "open": 10.0 + d * 0.05,
              "close": 10.0 + d * 0.08,
              "high": 10.2 + d * 0.06,
              "low": 9.8 + d * 0.04,
              "volume": 500000 + d * 10000,
              "turnover_rate": 2.0 + d * 0.1,
              "change_pct": 0.5 + d * 0.1,
         })
     return pd.DataFrame(rows)


def _mock_current_row():
     return {
          "code": "000001",
          "name": "平安银行",
          "last_price": 12.45,
          "change_pct": 4.2,
          "volume_ratio": 2.1,
          "turnover_rate": 5.8,
          "amplitude": 3.5,
          "high": 12.50,
          "low": 11.90,
          "open_price": 11.95,
          "pre_close": 11.95,
          "volume": 600000,
          "amount": 740000000,
     }


def _mock_sentiment():
     return {
          "rise_count": 2800,
          "fall_count": 1800,
          "total_stocks": 4700,
          "limit_up_count": 45,
          "limit_down_count": 8,
          "total_amount": 1200000000000,
          "avg_change_pct": 1.2,
     }


# ---- 测试 ----

def test_calc_minute_features():
     mf = _mock_minute_df()
     cr = _mock_current_row()
     feats = calc_minute_features(mf, cr)
     assert len(feats) > 0
     assert "late_30m_return" in feats
     assert "late_ratio" in feats
     assert "late_vol_ratio" in feats
     assert "vol_stability" in feats
     assert "price_position_intraday" in feats
     assert "return_10bar" in feats
     assert "volume_ratio" in feats
     assert "change_pct" in feats
     print("test_calc_minute_features: PASS")


def test_calc_minute_features_empty():
     feats = calc_minute_features(pd.DataFrame(), _mock_current_row())
     assert len(feats) > 0
     assert feats["late_30m_return"] == 0
     print("test_calc_minute_features_empty: PASS")


def test_calc_daily_features():
     df = _mock_daily_df()
     feats = calc_daily_features(df)
     assert len(feats) > 0
     assert "ma5_dev" in feats
     assert "ma_bull_aligned" in feats
     assert "return_5d" in feats
     assert "vol_vs_ma5" in feats
     assert "volatility_20d" in feats
     print("test_calc_daily_features: PASS")


def test_calc_daily_features_short():
     df = _mock_daily_df().head(5)
     feats = calc_daily_features(df)
     assert len(feats) > 0
     assert feats["ma20_dev"] == 0
     print("test_calc_daily_features_short: PASS")


def test_calc_sentiment_features():
     sent = _mock_sentiment()
     feats = calc_sentiment_features(sent)
     assert feats["rise_fall_ratio"] > 1.0
     assert feats["limit_up_count"] == 45
     assert feats["limit_down_count"] == 8
     print("test_calc_sentiment_features: PASS")


def test_calc_sentiment_features_empty():
     feats = calc_sentiment_features({})
     assert feats["rise_fall_ratio"] == 1.0
     print("test_calc_sentiment_features_empty: PASS")


def test_build_features():
     mf = _mock_minute_df()
     df = _mock_daily_df()
     cr = _mock_current_row()
     sent = _mock_sentiment()
     feats = build_features(mf, df, cr, sent)
     assert len(feats) > 20
     assert "late_surge_score" in feats
     assert "ma5_dev" in feats
     assert "rise_fall_ratio" in feats
     print("test_build_features: PASS")


def test_build_features_empty():
     feats = build_features(pd.DataFrame(), pd.DataFrame(), {}, {})
     assert len(feats) > 0
     print("test_build_features_empty: PASS")


def test_features_to_dataframe():
     feats_list = [
          {"a": 1, "b": 2.0, "c": True},
          {"a": 2, "b": 3.0, "c": False},
          {"a": 3, "b": 4.0, "c": True},
     ]
     df = features_to_dataframe(feats_list)
     assert len(df) == 3
     assert "a" in df.columns
     assert "b" in df.columns
     assert "c" in df.columns
     assert df["c"].dtype in [np.int64, int]
     print("test_features_to_dataframe: PASS")


def test_features_to_dataframe_empty():
     df = features_to_dataframe([])
     assert df.empty
     print("test_features_to_dataframe_empty: PASS")


# ---- 运行 ----

if __name__ == "__main__":
     tests = [
          test_calc_minute_features,
          test_calc_minute_features_empty,
          test_calc_daily_features,
          test_calc_daily_features_short,
          test_calc_sentiment_features,
          test_calc_sentiment_features_empty,
          test_build_features,
          test_build_features_empty,
          test_features_to_dataframe,
          test_features_to_dataframe_empty,
     ]
     passed = failed = 0
     for t in tests:
         try:
              t()
              passed += 1
         except Exception as e:
              print(f"FAIL {t.__name__}: {e}")
              failed += 1
     print(f"\n{'=' * 40}")
     print(f"结果: {passed} 通过, {failed} 失败, 共 {passed + failed} 个")
