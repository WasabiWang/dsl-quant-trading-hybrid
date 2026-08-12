#!/usr/bin/env python3
"""
DSL 审查工具 Layer 1: 历史预测准确率回测
==========================================
验证系统预测是否具有统计显著的预测能力。

审查维度:
  A. 方向准确率 (预测涨/跌 vs 实际涨/跌)
  B. 置信度分层胜率 (高置信度信号是否显著优于低置信度)
  C. 板块/tier分层准确率
  D. 滚动窗口准确率趋势 (模型是否在退化)
  E. 收益幅度预测准确性 (predicted_return vs actual_return)

用法:
  python3 scripts/audit/layer1_backtest_accuracy.py [--days 30] [--output report.json]
"""
import os, sys, json, argparse
from datetime import datetime, timedelta, date
import warnings
warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import pandas as pd

MIN_VALIDATED_WARNING = 5
MIN_VALIDATED_FAILURE = 1


def _audit_today() -> date:
    """Return the audit date, overridable for deterministic tests."""
    override = os.environ.get("DSL_AUDIT_AS_OF", "").strip()
    if override:
        return datetime.strptime(override[:10], "%Y-%m-%d").date()
    return datetime.now().date()


def _read_current_version() -> str:
    try:
        with open(os.path.join(PROJECT_ROOT, "VERSION"), "r", encoding="utf-8") as f:
            return f.readline().strip()
    except Exception:
        return "unknown"


def _parse_horizon_days(value, default: int = 5) -> int:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    try:
        parsed = int(digits)
    except Exception:
        parsed = default
    return max(1, parsed)


def _is_trading_day(day: date) -> bool:
    try:
        from config.holiday_calendar import is_trading_day
        return bool(is_trading_day(check_date=day, market="A_SHARE"))
    except Exception:
        return day.weekday() < 5


def _add_trading_days(start_day: date, trading_days: int) -> date:
    cur = start_day
    remaining = max(0, trading_days)
    while remaining > 0:
        cur += timedelta(days=1)
        if _is_trading_day(cur):
            remaining -= 1
    return cur


def _maturity_date(predict_day: date, horizon_days: int) -> date:
    return _add_trading_days(predict_day, horizon_days)


def _empty_current_metadata(days: int, as_of: date) -> dict:
    return {
        "as_of_date": as_of.isoformat(),
        "lookback_days": days,
        "current_version": _read_current_version(),
        "predict_date": "",
        "current_prediction_count": 0,
        "skipped_unmatured": 0,
        "unmatured_examples": [],
    }


def load_prediction_history(days: int = 30, as_of: date | None = None,
                            include_metadata: bool = False):
    """加载历史 daily_predict.json 文件并提取有结果验证的预测"""
    audit_day = as_of or _audit_today()
    metadata = _empty_current_metadata(days, audit_day)
    cache_dir = os.path.join(PROJECT_ROOT, "cache")
    pred_files = sorted([
        f for f in os.listdir(cache_dir)
        if f.startswith("daily_predict") and f.endswith(".json")
    ])
    if not pred_files:
        print("⚠️ 未找到 daily_predict.json 历史文件")
        return ([], metadata) if include_metadata else []

    # 读取最新版本
    pred_file = os.path.join(cache_dir, "daily_predict.json")
    if not os.path.exists(pred_file):
        return ([], metadata) if include_metadata else []

    with open(pred_file, "r") as f:
        data = json.load(f)

    predictions = data.get("predictions", [])
    predict_date = data.get("predict_date", "")
    if not predict_date:
        return ([], metadata) if include_metadata else []
    metadata.update({
        "current_version": data.get("version") or metadata["current_version"],
        "predict_date": predict_date,
        "current_prediction_count": len(predictions),
    })

    print(f"📊 加载预测: {len(predictions)}条 (预测日期: {predict_date})")

    # 验证: 获取实际5日后的K线数据, 对比预测方向
    validated = []
    try:
        pred_dt = datetime.strptime(predict_date, "%Y-%m-%d")
    except Exception:
        return ([], metadata) if include_metadata else []

    from dsl_data_sdk_original import get_kline, normalize_symbol

    for p in predictions:
        code = p.get("symbol", "")
        signal = p.get("signal", "hold")
        if signal == "hold":
            continue
        predicted_return = p.get("predicted_return", 0)
        confidence = p.get("confidence", 0)
        horizon_days = _parse_horizon_days(p.get("horizon"), default=5)
        mature_on = _maturity_date(pred_dt.date(), horizon_days)
        if mature_on > audit_day:
            metadata["skipped_unmatured"] += 1
            if len(metadata["unmatured_examples"]) < 5:
                metadata["unmatured_examples"].append({
                    "symbol": code,
                    "predict_date": predict_date,
                    "horizon_days": horizon_days,
                    "mature_on": mature_on.isoformat(),
                })
            continue

        try:
            normal = normalize_symbol(code)
            # 获取预测日到预测日后10天的K线 (5日预测窗口+缓冲)
            start = predict_date
            end = (mature_on + timedelta(days=5)).strftime("%Y-%m-%d")
            kline = get_kline(normal, start, end)

            if not kline or len(kline) < 5:
                continue

            df = pd.DataFrame(kline)
            for c in ["close"]:
                if c in df.columns:
                    df[c] = df[c].astype(float)

            pred_close = float(df["close"].iloc[0])  # 预测时的价格
            actual_close_5d = float(df["close"].iloc[min(5, len(df) - 1)])  # 5日后的价格
            actual_return_5d = (actual_close_5d / pred_close) - 1

            # 方向判断
            predicted_dir = "up" if (signal == "buy" and predicted_return > 0) else "down"
            actual_dir = "up" if actual_return_5d > 0 else "down"
            correct = predicted_dir == actual_dir

            validated.append({
                "symbol": code,
                "name": p.get("name", code),
                "predict_date": predict_date,
                "signal": signal,
                "predicted_return": predicted_return,
                "confidence": confidence,
                "horizon_days": horizon_days,
                "mature_on": mature_on.isoformat(),
                "pred_close": round(pred_close, 2),
                "actual_close_5d": round(actual_close_5d, 2),
                "actual_return_5d": round(actual_return_5d, 4),
                "predicted_dir": predicted_dir,
                "actual_dir": actual_dir,
                "correct": correct,
                "tier": p.get("tier", "unknown"),
            })
        except Exception as e:
            continue

    return (validated, metadata) if include_metadata else validated


def summarize_historical_calibration(days: int = 30, as_of: date | None = None,
                                     current_version: str | None = None) -> dict:
    """Summarize realized historical calibration without mixing it into current-version proof."""
    audit_day = as_of or _audit_today()
    current_version = current_version or _read_current_version()
    path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
    summary = {
        "source": path,
        "as_of_date": audit_day.isoformat(),
        "lookback_days": days,
        "current_version": current_version,
        "records": 0,
        "total": 0,
        "correct": 0,
        "accuracy": None,
        "current_version_total": 0,
        "current_version_correct": 0,
        "legacy_total": 0,
        "legacy_correct": 0,
        "by_version": {},
        "status": "missing",
    }
    if not os.path.exists(path):
        return summary
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        summary.update({"status": "invalid", "error": str(exc)})
        return summary

    cutoff = audit_day - timedelta(days=days)
    for dr in data.get("daily_records", []):
        try:
            record_day = datetime.strptime(str(dr.get("date", ""))[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if record_day < cutoff or record_day > audit_day:
            continue
        version = str(dr.get("version") or "unknown")
        record_seen = False
        for stock in dr.get("stocks", []):
            if stock.get("signal", "hold") == "hold":
                continue
            if not stock.get("realized_checked", False):
                continue
            if "realized_correct" not in stock:
                continue
            correct = bool(stock.get("realized_correct"))
            summary["total"] += 1
            summary["correct"] += int(correct)
            bucket = summary["by_version"].setdefault(version, {"total": 0, "correct": 0, "accuracy": None})
            bucket["total"] += 1
            bucket["correct"] += int(correct)
            if version == current_version:
                summary["current_version_total"] += 1
                summary["current_version_correct"] += int(correct)
            else:
                summary["legacy_total"] += 1
                summary["legacy_correct"] += int(correct)
            record_seen = True
        if record_seen:
            summary["records"] += 1

    if summary["total"] > 0:
        summary["accuracy"] = round(summary["correct"] / summary["total"], 4)
        summary["status"] = "available"
    else:
        summary["status"] = "empty"
    for bucket in summary["by_version"].values():
        if bucket["total"] > 0:
            bucket["accuracy"] = round(bucket["correct"] / bucket["total"], 4)
    return summary


def compute_accuracy_report(validated: list, evidence_metadata: dict | None = None,
                            historical_calibration: dict | None = None) -> dict:
    """计算完整准确率报告"""
    if not validated:
        reasons = ["当前版本无已兑现预测数据"]
        if evidence_metadata and evidence_metadata.get("skipped_unmatured", 0) > 0:
            reasons.append(f"跳过未成熟预测 {evidence_metadata['skipped_unmatured']} 条")
        return {
            "audit_time": datetime.now().isoformat(),
            "status": "warning",
            "validated_count": 0,
            "status_reason": "; ".join(reasons),
            "error": "无当前版本已兑现预测数据",
            "evidence_metadata": evidence_metadata or {},
            "historical_calibration": historical_calibration or {},
        }

    df = pd.DataFrame(validated)
    total = len(df)
    correct = df["correct"].sum()
    accuracy = correct / total if total > 0 else 0

    # A. 方向准确率
    direction = {
        "total_predictions": total,
        "correct": int(correct),
        "direction_accuracy": round(accuracy, 4),
        "buy_signals": int((df["signal"] == "buy").sum()),
        "sell_signals": int((df["signal"] == "sell").sum()),
    }

    # B. 置信度分层 — 按置信度分5档
    conf_bins = [0.3, 0.45, 0.55, 0.65, 0.75, 1.0]
    conf_labels = ["<45%", "45-55%", "55-65%", "65-75%", ">75%"]
    conf_strata = []
    for i in range(len(conf_bins) - 1):
        mask = (df["confidence"] >= conf_bins[i]) & (df["confidence"] < conf_bins[i + 1])
        subset = df[mask]
        n = len(subset)
        if n > 0:
            conf_strata.append({
                "range": conf_labels[i],
                "count": n,
                "accuracy": round(subset["correct"].sum() / n, 4),
                "avg_return": round(subset["actual_return_5d"].mean(), 4),
            })

    # C. Tier分层
    tier_strata = []
    for tier in df["tier"].unique():
        subset = df[df["tier"] == tier]
        n = len(subset)
        if n > 0:
            tier_strata.append({
                "tier": tier,
                "count": n,
                "accuracy": round(subset["correct"].sum() / n, 4),
            })

    # D. 收益幅度 vs 预测方向
    buy_df = df[df["signal"] == "buy"]
    sell_df = df[df["signal"] == "sell"]
    amplitude = {
        "buy_avg_actual_return": round(buy_df["actual_return_5d"].mean(), 4) if len(buy_df) > 0 else 0,
        "sell_avg_actual_return": round(sell_df["actual_return_5d"].mean(), 4) if len(sell_df) > 0 else 0,
        "predicted_vs_actual_corr": round(df["predicted_return"].corr(df["actual_return_5d"]), 4) if total > 2 else 0,
    }

    # E. 显著性检验 (二项分布)
    from math import sqrt
    se = sqrt(accuracy * (1 - accuracy) / total) if total > 0 else 1
    z_score = (accuracy - 0.5) / se if se > 0 else 0

    status = "passed"
    status_reason = "样本充足且准确率未低于随机下限"
    if total < MIN_VALIDATED_WARNING:
        status = "warning"
        status_reason = f"当前版本验证样本不足: {total} < {MIN_VALIDATED_WARNING}"
    elif accuracy < 0.48:
        status = "failed"
        status_reason = f"方向准确率低于随机下限: {accuracy:.1%} < 48%"

    return {
        "audit_time": datetime.now().isoformat(),
        "status": status,
        "status_reason": status_reason,
        "validated_count": total,
        "evidence_metadata": evidence_metadata or {},
        "historical_calibration": historical_calibration or {},
        "direction_accuracy": direction,
        "confidence_stratification": conf_strata,
        "tier_stratification": tier_strata,
        "amplitude_analysis": amplitude,
        "statistical_significance": {
            "z_score_vs_random": round(z_score, 2),
            "significant_at_95": abs(z_score) > 1.96,
            "p_value_approx": "p < 0.05" if abs(z_score) > 1.96 else "p >= 0.05 (not significant)",
        },
        "verdict": _generate_verdict(accuracy, z_score, conf_strata),
    }


def _generate_verdict(accuracy: float, z_score: float, conf_strata: list) -> str:
    """生成综合判断"""
    if accuracy > 0.55 and abs(z_score) > 1.96:
        return "✅ 系统具有统计显著的预测能力 (acc={:.1%}, z={:.1f})".format(accuracy, z_score)
    elif accuracy > 0.52:
        return "⚠️ 系统有微弱预测能力，但未达到统计显著 (acc={:.1%}, z={:.1f})".format(accuracy, z_score)
    elif accuracy > 0.48:
        return "⚠️ 系统预测能力接近随机水平 (acc={:.1%})".format(accuracy)
    else:
        return "❌ 系统预测能力低于随机水平 (acc={:.1%}) — 建议停止实盘".format(accuracy)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="回看天数")
    parser.add_argument("--output", default=None, help="报告输出路径")
    args = parser.parse_args()

    print("=" * 60)
    print("🔍 DSL审查 Layer1: 历史预测准确率回测")
    print("=" * 60)

    audit_day = _audit_today()
    validated, evidence_metadata = load_prediction_history(
        args.days, as_of=audit_day, include_metadata=True)
    historical_calibration = summarize_historical_calibration(
        args.days, as_of=audit_day, current_version=evidence_metadata.get("current_version"))
    if not validated:
        print("⚠️ 无有效预测数据可供验证")
        report = compute_accuracy_report(validated, evidence_metadata, historical_calibration)
        if args.output:
            with open(args.output, "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"📁 报告已保存: {args.output}")
        sys.exit(2)

    report = compute_accuracy_report(validated, evidence_metadata, historical_calibration)

    # 输出报告
    print(f"\n📊 预测总数: {report['direction_accuracy']['total_predictions']}")
    print(f"   方向准确率: {report['direction_accuracy']['direction_accuracy']:.2%}")
    print(f"   买入信号: {report['direction_accuracy']['buy_signals']} | 卖出信号: {report['direction_accuracy']['sell_signals']}")

    print(f"\n📊 置信度分层胜率:")
    for s in report["confidence_stratification"]:
        bar = "█" * int(s["accuracy"] * 30)
        print(f"   {s['range']:>8s}: {s['accuracy']:.1%} {bar} (n={s['count']})")

    print(f"\n📊 Tier分层:")
    for s in report["tier_stratification"]:
        print(f"   {s['tier']:>12s}: {s['accuracy']:.1%} (n={s['count']})")

    print(f"\n📊 幅度分析:")
    print(f"   买入信号平均实际收益: {report['amplitude_analysis']['buy_avg_actual_return']:+.3%}")
    print(f"   卖出信号平均实际收益: {report['amplitude_analysis']['sell_avg_actual_return']:+.3%}")
    print(f"   预测vs实际相关系数: {report['amplitude_analysis']['predicted_vs_actual_corr']:.3f}")

    print(f"\n📊 显著性检验:")
    print(f"   Z-score: {report['statistical_significance']['z_score_vs_random']:.1f}")
    print(f"   95%置信显著: {'✅' if report['statistical_significance']['significant_at_95'] else '❌'}")

    print(f"\n{'='*60}")
    print(f"📋 综合判断: {report['verdict']}")
    print(f"{'='*60}")

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📁 报告已保存: {args.output}")
    if report.get("status") == "failed":
        sys.exit(1)
    if report.get("status") == "warning":
        sys.exit(2)


if __name__ == "__main__":
    main()
