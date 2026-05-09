#!/usr/bin/env python3
"""启动脚本 - 放在stock-screener目录外层"""
import sys
from pathlib import Path

# 把stock_screener加到path
sys.path.insert(0, str(Path(__file__).parent / "stock_screener"))

from stock_screener.main import main
main(sys.argv[1:])
