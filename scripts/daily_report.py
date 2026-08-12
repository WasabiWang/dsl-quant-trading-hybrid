#!/usr/bin/env python3
"""Daily report wrapper - delegates to daily_sim_report_v3.py"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 解析 --market 参数
market = "a"
for i, arg in enumerate(sys.argv):
    if arg == "--market" and i + 1 < len(sys.argv):
        market = sys.argv[i + 1].lower()

# 直接调用daily_sim_report_v3
from scripts.daily_sim_report_v3 import main
main()
