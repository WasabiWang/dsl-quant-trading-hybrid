#!/usr/bin/env python3
"""L0 单元测试: scripts/rank_ic_monitor.py (v4.7.3 P0)

覆盖:
  - _spearman 边界 (常量序列 → nan, 完美正相关 → 1, 完美负相关 → -1)
  - compute_drift 分级 (healthy / degraded / critical / drifted / data_issue)
  - fill_realized 幂等性 (已填不覆盖)
"""
import os, sys, json, math
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import numpy as np

import rank_ic_monitor as rim


def test_spearman_perfect_positive():
    x = list(range(1, 21))
    y = list(range(2, 22))
    assert abs(rim._spearman(x, y) - 1.0) < 1e-9


def test_spearman_perfect_negative():
    x = list(range(1, 21))
    y = list(range(20, 0, -1))
    assert abs(rim._spearman(x, y) + 1.0) < 1e-9


def test_spearman_constant_returns_nan():
    x = [1.0] * 15
    y = list(range(15))
    assert math.isnan(rim._spearman(x, y))


def test_spearman_small_n():
    x = [1.0, 2.0]
    y = [1.0, 2.0]
    # 2个样本 std 不为0 可计算
    assert abs(rim._spearman(x, y) - 1.0) < 1e-9


def test_compute_drift_critical():
    rics = [0.05] * 40 + [-0.30] * 20
    series = [{"date": f"2026-01-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
              for i, v in enumerate(rics)]
    th = dict(rim.DEFAULT_THRESHOLDS)
    out = rim.compute_drift(series, th)
    assert out["drift_status"] == "critical"
    assert out["recent20_mean"] < -0.10


def test_compute_drift_degraded():
    rics = [0.05] * 40 + [-0.02] * 20
    series = [{"date": f"2026-01-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
              for i, v in enumerate(rics)]
    th = dict(rim.DEFAULT_THRESHOLDS)
    out = rim.compute_drift(series, th)
    assert out["drift_status"] == "degraded"


def test_compute_drift_healthy():
    rics = [0.10] * 60
    series = [{"date": f"2026-01-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
              for i, v in enumerate(rics)]
    th = dict(rim.DEFAULT_THRESHOLDS)
    out = rim.compute_drift(series, th)
    assert out["drift_status"] == "healthy"
    assert out["rank_icir_30d"] is None  # 零方差 → ICIR 无定义
    # 有方差但健康的序列 → ICIR 为正
    rics2 = [0.10 + (0.01 if i % 2 else -0.01) for i in range(60)]
    series2 = [{"date": f"2026-02-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
               for i, v in enumerate(rics2)]
    out2 = rim.compute_drift(series2, th)
    assert out2["drift_status"] == "healthy"
    assert out2["rank_icir_30d"] > 1.0


def test_compute_drift_decay():
    # 历史均值0.10, 近20日0.03 → 衰减70% > 50% → drifted
    rics = [0.10] * 40 + [0.03] * 20
    series = [{"date": f"2026-01-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
              for i, v in enumerate(rics)]
    th = dict(rim.DEFAULT_THRESHOLDS)
    out = rim.compute_drift(series, th)
    assert out["drift_status"] == "drifted"


def test_compute_drift_data_issue():
    rics = [0.10] * 30
    series = [{"date": f"2026-01-{i+1:02d}", "n": 30, "rank_ic": v, "coverage": 0.9}
              for i, v in enumerate(rics)]
    for r in series[-10:]:
        r["coverage"] = 0.4  # 近10日覆盖<0.6
    th = dict(rim.DEFAULT_THRESHOLDS)
    out = rim.compute_drift(series, th)
    assert out["drift_status"] == "data_issue"


def test_fill_realized_idempotent():
    cal = {"daily_records": [{
        "date": "2026-01-10",
        "stocks": [
            {"symbol": "000001", "predicted_return": 0.01, "horizon": "5d", "realized_return": 0.02},
            {"symbol": "000002", "predicted_return": 0.01, "horizon": "5d"},
        ],
    }]}
    kline_maps = {"000002": {"2026-01-10": 10.0, "2026-01-12": 11.0, "2026-01-13": 12.0,
                             "2026-01-14": 13.0, "2026-01-15": 14.0, "2026-01-16": 15.0}}
    trading_days = set(kline_maps["000002"].keys())
    # 已有 realized 不覆盖
    filled = rim.fill_realized(cal, kline_maps, trading_days, dry_run=True)
    assert cal["daily_records"][0]["stocks"][0]["realized_return"] == 0.02
    # 缺失的被填 (第5个交易日 = 2026-01-16, 15.0/10.0-1 = 0.5)
    assert abs(cal["daily_records"][0]["stocks"][1]["realized_return"] - 0.5) < 1e-6
    assert filled == 1


def test_compute_ic_series_retains_low_sample_day_with_null_ic():
    """P1: 样本不足的截面必须保留(仅 IC 置空), 否则数据缺失会被整日隐藏。"""
    cal = {"daily_records": [{
        "date": "2026-01-10",
        "stocks": [{"symbol": s, "predicted_return": float(i), "horizon": "5d",
                    "realized_return": float(i) * 0.5}
                   for i, s in enumerate(["000001", "000002", "000003", "000004", "000005",
                                          "000006", "000007", "000008", "000009"])],
    }]}
    th = dict(rim.DEFAULT_THRESHOLDS)
    series = rim.compute_ic_series(cal, th, "2026-01-10")
    assert len(series) == 1                 # 不再整日丢弃
    assert series[0]["rank_ic"] is None     # 9 < min_n=10 → IC 置空
    assert series[0]["coverage"] == 1.0
    assert series[0]["mature"] is True


def test_compute_ic_series_exposes_low_coverage_anomaly():
    """P1: 25 预测仅 9 条兑现 → coverage 0.36 必须可见并触发 data_issue。"""
    stocks = []
    for i in range(25):
        s = {"symbol": f"{i:06d}", "predicted_return": float(i), "horizon": "5d"}
        if i < 9:
            s["realized_return"] = float(i) * 0.5
        stocks.append(s)
    cal = {"daily_records": [{"date": "2026-01-10", "stocks": stocks}]}
    th = dict(rim.DEFAULT_THRESHOLDS)
    series = rim.compute_ic_series(cal, th, "2026-01-10")
    assert series[0]["coverage"] == round(9 / 25, 4)
    assert rim.compute_quality_summary(series, th)["quality_status"] == "data_issue"


def test_fill_realized_skips_missing_target_day():
    """P1: 兑现日不在该标的K线中时跳过, 不得 KeyError 中断整批回填。"""
    cal = {"daily_records": [{
        "date": "2026-01-10",
        "stocks": [{"symbol": "000002", "predicted_return": 0.01, "horizon": "5d"}],
    }]}
    kline_maps = {"000002": {"2026-01-10": 10.0, "2026-01-12": 11.0, "2026-01-13": 12.0,
                             "2026-01-14": 13.0, "2026-01-15": 14.0}}
    trading_days = {"2026-01-10", "2026-01-12", "2026-01-13",
                    "2026-01-14", "2026-01-15", "2026-01-16"}
    filled = rim.fill_realized(cal, kline_maps, trading_days, dry_run=True)
    assert filled == 0
    assert cal["daily_records"][0]["stocks"][0].get("realized_return") is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
