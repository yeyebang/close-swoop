#!/usr/bin/env python3
# 测试历史数据获取
import sys
import akshare as ak
import pandas as pd

# 测试取5只候选股的历史数据
test_codes = ['301027', '000782', '301222', '002862', '002480']

for code in test_codes:
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                               start_date="20260301", end_date="20260427",
                               adjust="qfq")
        print(f'{code} - {df.iloc[-1]["名称"] if "名称" in df.columns else "OK"}: {len(df)} 条日线')
        print(df.tail(3)[['日期', '开盘', '收盘', '最高', '最低', '成交量', '成交额', '振幅', '涨跌幅', '换手率']].to_string())
        print()
    except Exception as e:
        print(f'{code}: ERROR - {e}')
        print()
