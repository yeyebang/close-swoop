#!/usr/bin/env python3
# 快速测试：获取数据并简单过滤
import sys
import akshare as ak
import pandas as pd

print('=== 获取全市场数据 ===')
df = ak.stock_zh_a_spot_em()
print(f'总股票数: {len(df)}')

print('\n=== 基本过滤 ===')

# 排除ST
df_filtered = df[~df['名称'].str.contains('ST', na=False)]
print(f'排除ST后: {len(df_filtered)}')

# 排除科创板/创业板(300开头/688开头)
df_filtered = df_filtered[~df_filtered['代码'].str.startswith(('688', '300'))].copy()
print(f'排除科创/创业板后: {len(df_filtered)}')

# 价格区间3-50元
df_filtered = df_filtered[(df_filtered['最新价'] >= 3) & (df_filtered['最新价'] <= 50)]
print(f'价格区间3-50元后: {len(df_filtered)}')

# 排除涨停股（>9.8%）和跌停股（<-9.8%）
df_filtered = df_filtered[(df_filtered['涨跌幅'] < 9.8) & (df_filtered['涨跌幅'] > -2)]
print(f'排除涨跌停后: {len(df_filtered)}')

print('\n=== 候选股分布 ===')
print(df_filtered['涨跌幅'].describe())
print('\n换手率分布:')
print(df_filtered['换手率'].describe())
print('\n量比分布:')
print(df_filtered['量比'].describe())

print('\n=== 前10只候选（按涨幅排序） ===')
top = df_filtered.sort_values('涨跌幅', ascending=False)[['代码', '名称', '最新价', '涨跌幅', '换手率', '量比']].head(10)
print(top.to_string())
