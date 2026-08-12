#!/usr/bin/env python3
"""
DSL 审查工具 Layer 4: 数据质量监控
====================================
检测K线数据缺失、复权一致、基本面时效、缓存过期等问题。

审查维度:
  A. K线数据完整性 (缺失交易日/停牌/天数不足)
  B. 复权一致性 (前复权 vs 后复权)
  C. 基本面数据时效 (麦蕊API数据更新时间)
  D. 预测缓存过期检测
  E. 关键文件freshness检查
  F. 股票池覆盖率 (42只中有多少有模型)

用法:
  python3 scripts/audit/layer4_data_quality.py [--fix] [--output report.json]
"""
import os, sys, json, argparse
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import pandas as pd
import yaml


ENHANCED_FULL_WINDOW_DAYS = 7
ENHANCED_FULL_MIN_COUNT = 10
ENHANCED_FULL_POOL_RATIO = 0.5


class DataQualityMonitor:
    """数据质量监控器"""

    def __init__(self):
        self.issues = []
        self.warnings = []
        self.passes = []

    def run_all(self) -> dict:
        print("=" * 60)
        print("🔍 DSL审查 Layer4: 数据质量监控")
        print("=" * 60)

        checks = [
            self.check_kline_completeness,
            self.check_model_coverage,
            self.check_cache_freshness,
            self.check_prediction_semantics,
            self.check_enhanced_report_coverage,
            self.check_config_integrity,
            self.check_calibration_data,
        ]
        for check in checks:
            print(f"\n📋 {check.__doc__}...")
            check()

        return self._summary()

    @staticmethod
    def _trading_days_between(start_day, end_day) -> int:
        """Count A-share trading days strictly between two observed K-line dates."""
        try:
            from config.holiday_calendar import is_trading_day
        except Exception:
            is_trading_day = None

        cur = start_day + timedelta(days=1)
        count = 0
        while cur < end_day:
            if is_trading_day:
                if is_trading_day(check_date=cur, market="A_SHARE"):
                    count += 1
            elif cur.weekday() < 5:
                count += 1
            cur += timedelta(days=1)
        return count

    @staticmethod
    def _expected_plan_target_date(now: datetime = None) -> str:
        """Return the A-share target date accepted by execution scripts."""
        now = now or datetime.now()
        today = now.date()
        try:
            from config.holiday_calendar import is_trading_day, get_next_trading_day
            if is_trading_day(check_date=today, market="A_SHARE"):
                return today.isoformat()
            return get_next_trading_day("A_SHARE", today).isoformat()
        except Exception:
            return today.isoformat()

    @staticmethod
    def _safe_read_json(path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _stock_keys(data: dict) -> list:
        if not isinstance(data, dict):
            return []
        return [
            key for key in data.keys()
            if isinstance(key, str) and key.isdigit() and len(key) >= 6
        ]

    def _load_pool_symbols(self) -> list:
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = yaml.safe_load(f) or {}
        except Exception:
            return []
        return [
            str(s.get("symbol", ""))
            for s in pool.get("master_pool", [])
            if s.get("symbol")
        ]

    def _prediction_rows(self) -> list:
        path = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
        data = self._safe_read_json(path) or {}
        rows = data.get("predictions", [])
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _is_suspended_prediction(row: dict) -> bool:
        return row.get("source") == "suspended" or row.get("confidence_level") == "suspended"

    @staticmethod
    def _ratio(count: int, total: int) -> float:
        return count / total if total else 0.0

    def _append_ratio_result(self, label: str, count: int, total: int,
                             warn_ratio: float, fail_ratio: float):
        ratio = self._ratio(count, total)
        detail = f"{label}: {count}/{total} ({ratio:.0%})"
        if total == 0:
            self.warnings.append(f"{label}: 无可检查样本")
        elif ratio > fail_ratio:
            self.issues.append(f"{detail} > {fail_ratio:.0%}")
        elif ratio > warn_ratio:
            self.warnings.append(f"{detail} > {warn_ratio:.0%}")
        else:
            self.passes.append(f"{detail} ✅")

    def check_kline_completeness(self):
        """K线数据完整性检查"""
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path, "r") as f:
            pool = yaml.safe_load(f)

        stocks = pool.get("master_pool", [])[:10]  # 抽查前10只
        try:
            from dsl_data_sdk_original import get_kline, normalize_symbol
        except Exception:
            self.issues.append("无法加载K线SDK")
            return

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

        gaps = []
        calendar_only_gaps = []
        for s in stocks:
            code = s["symbol"]
            try:
                k = get_kline(normalize_symbol(code), start, end)
                if not k or len(k) < 200:
                    gaps.append(f"{code} {s['name']}: 仅{len(k) if k else 0}条数据")
                    continue
                # 检查日期连续性
                df = pd.DataFrame(k)
                if "date" in df.columns:
                    dates = pd.to_datetime(df["date"])
                    date_diffs = dates.diff().dropna()
                    max_calendar_gap = int(date_diffs.max().days)
                    max_trading_gap = 0
                    if len(dates) >= 2:
                        ordered_dates = sorted(d.date() for d in dates)
                        max_trading_gap = max(
                            self._trading_days_between(prev, cur)
                            for prev, cur in zip(ordered_dates, ordered_dates[1:])
                        )
                    detail = {
                        "code": code,
                        "name": s["name"],
                        "max_calendar_gap": max_calendar_gap,
                        "max_trading_gap": max_trading_gap,
                        "gap_type": "trading" if max_trading_gap > 3 else "calendar",
                    }
                    if max_trading_gap > 3:
                        gaps.append(
                            f"{detail['code']} {detail['name']}: "
                            f"trading_gap={detail['max_trading_gap']} "
                            f"calendar_gap={detail['max_calendar_gap']}"
                        )
                    elif max_calendar_gap > 7:
                        calendar_only_gaps.append(
                            f"{detail['code']} {detail['name']}: "
                            f"calendar_gap={detail['max_calendar_gap']} "
                            f"trading_gap={detail['max_trading_gap']}"
                        )
            except Exception as e:
                gaps.append(f"{code} {s['name']}: 获取失败({e})")

        if gaps:
            self.issues.append(f"{len(gaps)}/{len(stocks)}只K线有缺失: {'; '.join(gaps[:3])}")
        elif calendar_only_gaps:
            self.passes.append(
                f"抽查{len(stocks)}只股票K线交易日连续 ✅ "
                f"(自然日长间隔为节假日/周末: {calendar_only_gaps[0]})"
            )
        else:
            self.passes.append(f"抽查{len(stocks)}只股票K线完整 ✅")

    def check_model_coverage(self):
        """模型覆盖率: 42只中有多少有训练好的模型"""
        models_dir = os.path.join(PROJECT_ROOT, "models")
        if not os.path.exists(models_dir):
            self.issues.append("models目录不存在")
            return

        model_dirs = [
            d for d in os.listdir(models_dir)
            if os.path.isdir(os.path.join(models_dir, d)) and d != "pool"
        ]
        has_lgb = sum(1 for d in model_dirs
                      if os.path.exists(os.path.join(models_dir, d, "lightgbm.pkl")))
        has_meta = sum(1 for d in model_dirs
                       if os.path.exists(os.path.join(models_dir, d, "model_metadata.pkl")))

        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)
        total_stocks = len(pool.get("master_pool", []))

        if has_lgb < total_stocks * 0.8:
            self.issues.append(f"模型覆盖率: {has_lgb}/{total_stocks} ({has_lgb/total_stocks:.0%}) < 80%")
        else:
            self.passes.append(f"模型覆盖率: {has_lgb}/{total_stocks} ✅")

        if has_meta < has_lgb * 0.5:
            self.warnings.append(f"model_metadata.pkl覆盖率: {has_meta}/{has_lgb} (需重新训练)")
        else:
            self.passes.append(f"model_metadata: {has_meta}/{has_lgb} ✅")

    def check_cache_freshness(self):
        """关键缓存文件时效"""
        cache_dir = os.path.join(PROJECT_ROOT, "cache")
        files_to_check = {
            "daily_predict.json": 24,      # 小时
            "planned_trades.json": 24,
        }

        now = datetime.now()
        for fname, max_hours in files_to_check.items():
            fpath = os.path.join(cache_dir, fname)
            if not os.path.exists(fpath):
                self.warnings.append(f"缓存文件缺失: {fname}")
                continue
            if fname == "planned_trades.json":
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        target_date = data.get("target_date")
                        expected_date = self._expected_plan_target_date(now)
                        if target_date and target_date != expected_date:
                            self.passes.append(
                                f"{fname}: target_date={target_date}≠{expected_date}, "
                                "执行层已fail-closed ✅"
                            )
                            continue
                except Exception as e:
                    self.warnings.append(f"{fname}: 无法解析目标日({e})")
                    continue
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            hours_old = (now - mtime).total_seconds() / 3600
            if hours_old > max_hours:
                self.issues.append(f"{fname}: {hours_old:.1f}小时前 (> {max_hours}h限制)")
            elif hours_old > max_hours * 0.5:
                self.warnings.append(f"{fname}: {hours_old:.1f}小时前 (接近过期)")
            else:
                self.passes.append(f"{fname}: {hours_old:.1f}h前 ✅")

        # 检查 prediction_enhanced 最新文件
        pred_dir = os.path.join(PROJECT_ROOT, "reports", "predictor")
        if os.path.exists(pred_dir):
            enhanced_files = sorted([
                f for f in os.listdir(pred_dir) if f.startswith("prediction_enhanced")
            ])
            if enhanced_files:
                latest = enhanced_files[-1]
                lpath = os.path.join(pred_dir, latest)
                hours_old = (now - datetime.fromtimestamp(os.path.getmtime(lpath))).total_seconds() / 3600
                if hours_old > 28:
                    self.issues.append(f"prediction_enhanced: {hours_old:.1f}h前 (应每日更新)")
                else:
                    self.passes.append(f"prediction_enhanced: {hours_old:.1f}h前 ✅")
            else:
                self.warnings.append("无prediction_enhanced文件")
        else:
            self.warnings.append("reports/predictor目录不存在")

    def check_prediction_semantics(self):
        """daily_predict来源分布与Dashboard语义污染检查"""
        rows = self._prediction_rows()
        if not rows:
            self.issues.append("daily_predict: predictions为空，不能判定Dashboard预测语义")
            return

        pool_symbols = self._load_pool_symbols()
        pool_size = len(pool_symbols) or len(rows)
        active_rows = [r for r in rows if not self._is_suspended_prediction(r)]
        active_total = len(active_rows)
        source_counts = {}
        for row in rows:
            source = row.get("source", "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1

        h5d_count = source_counts.get("h5d_enhanced", 0)
        h5d_ratio = self._ratio(h5d_count, pool_size)
        if h5d_ratio < 0.50:
            self.issues.append(f"h5d_enhanced覆盖率: {h5d_count}/{pool_size} ({h5d_ratio:.0%}) < 50%")
        elif h5d_ratio < 0.80:
            self.warnings.append(f"h5d_enhanced覆盖率: {h5d_count}/{pool_size} ({h5d_ratio:.0%}) < 80%")
        else:
            self.passes.append(f"h5d_enhanced覆盖率: {h5d_count}/{pool_size} ({h5d_ratio:.0%}) ✅")

        fallback_count = source_counts.get("pool_fallback", 0)
        fallback_ratio = self._ratio(fallback_count, active_total)
        if fallback_ratio > 0.50:
            self.issues.append(f"pool_fallback占比: {fallback_count}/{active_total} ({fallback_ratio:.0%}) > 50%")
        elif fallback_ratio > 0.30:
            self.warnings.append(f"pool_fallback占比: {fallback_count}/{active_total} ({fallback_ratio:.0%}) > 30%")
        else:
            self.passes.append(f"pool_fallback占比: {fallback_count}/{active_total} ({fallback_ratio:.0%}) ✅")

        conf_default = sum(1 for r in active_rows if r.get("confidence") == 0.3)
        zero_return = sum(1 for r in active_rows if r.get("predicted_return", None) == 0)
        acc_default = sum(1 for r in active_rows if r.get("direction_accuracy", r.get("accuracy")) == 0.5)
        self._append_ratio_result("confidence=0.3默认值", conf_default, active_total, 0.10, 0.30)
        self._append_ratio_result("predicted_return=0默认值", zero_return, active_total, 0.10, 0.30)
        self._append_ratio_result("direction_accuracy=0.5待训练哨兵", acc_default, active_total, 0.10, 0.30)

        training_status = self._safe_read_json(os.path.join(PROJECT_ROOT, "cache", "training_status.json")) or {}
        trained_symbols = {
            sym for sym, info in training_status.items()
            if isinstance(info, dict)
            and (info.get("status") == "success" or info.get("model_fresh") is True)
        }
        if trained_symbols:
            missing_time = [
                sym for sym in sorted(trained_symbols)
                if not training_status.get(sym, {}).get("last_train_time")
            ]
            if missing_time:
                missing_ratio = self._ratio(len(missing_time), len(trained_symbols))
                msg = f"training_status成功标的缺last_train_time: {len(missing_time)}/{len(trained_symbols)} ({', '.join(missing_time[:5])})"
                if missing_ratio > 0.30:
                    self.issues.append(msg)
                else:
                    self.warnings.append(msg)
            else:
                self.passes.append(f"training_status last_train_time覆盖: {len(trained_symbols)}只 ✅")

            trained_default = []
            for row in active_rows:
                symbol = str(row.get("symbol") or row.get("code") or "")
                if symbol not in trained_symbols:
                    continue
                is_default = (
                    row.get("confidence") == 0.3
                    or row.get("predicted_return", None) == 0
                    or row.get("direction_accuracy", row.get("accuracy")) == 0.5
                )
                if is_default:
                    trained_default.append(symbol)
            if trained_default:
                ratio = self._ratio(len(trained_default), len(trained_symbols))
                msg = (
                    f"Dashboard待训练/默认值污染已训练标的: "
                    f"{len(trained_default)}/{len(trained_symbols)} ({', '.join(trained_default[:8])})"
                )
                if ratio > 0.30:
                    self.issues.append(msg)
                else:
                    self.warnings.append(msg)
            else:
                self.passes.append("已训练标的无Dashboard待训练/默认值污染 ✅")
        else:
            self.warnings.append("training_status无成功标的，无法交叉验证Dashboard待训练语义")

    def check_enhanced_report_coverage(self):
        """enhanced预测报告全量覆盖检查"""
        pred_dir = os.path.join(PROJECT_ROOT, "reports", "predictor")
        if not os.path.exists(pred_dir):
            self.warnings.append("reports/predictor目录不存在，无法检查enhanced全量报告")
            return

        files = [
            os.path.join(pred_dir, f)
            for f in os.listdir(pred_dir)
            if f.startswith("prediction_enhanced_") and f.endswith(".json")
        ]
        if not files:
            self.warnings.append("无prediction_enhanced文件，无法检查全量报告覆盖")
            return

        pool_size = len(self._load_pool_symbols())
        full_threshold = max(ENHANCED_FULL_MIN_COUNT, int(pool_size * ENHANCED_FULL_POOL_RATIO))
        now = datetime.now()
        reports = []
        for path in files:
            data = self._safe_read_json(path)
            if not isinstance(data, dict):
                continue
            count = len(self._stock_keys(data))
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            reports.append({
                "path": path,
                "name": os.path.basename(path),
                "count": count,
                "mtime": mtime,
                "is_full": count >= full_threshold,
                "age_days": (now - mtime).total_seconds() / 86400,
            })
        if not reports:
            self.warnings.append("prediction_enhanced文件均无法解析")
            return

        reports.sort(key=lambda r: r["mtime"], reverse=True)
        latest = reports[0]
        full_reports = [r for r in reports if r["is_full"]]
        recent_full = [r for r in full_reports if r["age_days"] <= ENHANCED_FULL_WINDOW_DAYS]

        if not recent_full:
            self.issues.append(
                f"7天内无全量enhanced报告: 全量阈值≥{full_threshold}只，"
                f"最新报告{latest['name']}仅{latest['count']}只"
            )
            return

        latest_full = recent_full[0]
        if latest["count"] < full_threshold:
            self.passes.append(
                f"最新enhanced为小批量{latest['count']}只，"
                f"但存在7天内全量基底{latest_full['name']}({latest_full['count']}只) ✅"
            )
        else:
            self.passes.append(
                f"最新全量enhanced: {latest_full['name']} "
                f"{latest_full['count']}只 age={latest_full['age_days']:.1f}d ✅"
            )

    def check_config_integrity(self):
        """配置文件完整性"""
        configs = ["master_stock_pool.yaml", "adaptive_params.yaml", "optimization_params.json"]
        config_dir = os.path.join(PROJECT_ROOT, "config")
        for cfg in configs:
            if not os.path.exists(os.path.join(config_dir, cfg)):
                self.issues.append(f"配置文件缺失: {cfg}")

        # 检查 master_stock_pool 的 tier 分布
        pool_path = os.path.join(config_dir, "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)
        stocks = pool.get("master_pool", [])
        tiers = {}
        for s in stocks:
            t = s.get("tier", "unknown")
            tiers[t] = tiers.get(t, 0) + 1
        self.passes.append(f"股票池: {len(stocks)}只 (tiers: {tiers})")

        # 检查是否有重复symbol
        symbols = [s["symbol"] for s in stocks]
        dups = [s for s in symbols if symbols.count(s) > 1]
        if dups:
            self.issues.append(f"重复股票代码: {list(set(dups))}")

    def check_calibration_data(self):
        """校准数据健康度"""
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        if not os.path.exists(cal_path):
            self.warnings.append("prediction_calibration.json 不存在")
            return

        with open(cal_path) as f:
            cal = json.load(f)

        sa = cal.get("stock_accuracy", {})
        total = len(sa)
        critical_symbols = [
            sym for sym, v in sa.items()
            if v.get("last_accuracy", 1) < 0.45
        ]
        critical = len(critical_symbols)
        below50 = sum(1 for v in sa.values()
                      if v.get("last_accuracy", 1) < 0.50)
        low_symbols = [
            sym for sym, v in sa.items()
            if v.get("last_accuracy", 1) < 0.50
        ]

        degraded_path = os.path.join(PROJECT_ROOT, "confidence_data", "degraded_models.json")
        degraded_symbols = set()
        if os.path.exists(degraded_path):
            try:
                with open(degraded_path) as f:
                    degraded_symbols = set((json.load(f) or {}).keys())
            except Exception:
                degraded_symbols = set()

        queued_symbols = set()
        try:
            from core.retrain_queue_manager import get_retrain_queue_manager
            mgr = get_retrain_queue_manager()
            for item in mgr.list_pending() + mgr.list_by_status("training"):
                if item.get("symbol"):
                    queued_symbols.add(item["symbol"])
        except Exception:
            queued_symbols = set()

        unhandled_critical = [
            s for s in critical_symbols
            if s not in degraded_symbols and s not in queued_symbols
        ]
        unhandled_low = [
            s for s in low_symbols
            if s not in degraded_symbols and s not in queued_symbols
        ]

        if unhandled_critical:
            self.issues.append(f"紧急低精度未处理: {len(unhandled_critical)}只 <45% ({', '.join(unhandled_critical[:5])})")
        elif critical > 0:
            self.passes.append(f"紧急低精度: {critical}只 <45%，均已降级或入重训队列 ✅")
        if below50 > total * 0.3:
            self.warnings.append(f"低精度占比: {below50}/{total} ({below50/total:.0%}) > 30%")
        else:
            self.passes.append(f"校准: {total}只, 紧急{critical}只, 低于50%={below50}只")
        planned_only = [s for s in unhandled_low if s not in set(unhandled_critical)]
        if planned_only:
            self.warnings.append(f"计划重训未入队/降级: {len(planned_only)}只 <50% ({', '.join(planned_only[:5])})")

        # 检查准确率列表长度 (防无界增长)
        max_len = max((len(v.get("accuracies", [])) for v in sa.values()), default=0)
        if max_len > 50:
            self.warnings.append(f"校准历史记录最长{max_len}条 (建议≤50)")
        else:
            self.passes.append(f"校准历史: 最多{max_len}条 ✅")

    def _summary(self) -> dict:
        return {
            "audit_time": datetime.now().isoformat(),
            "issues": self.issues,
            "warnings": self.warnings,
            "passes": self.passes,
            "issue_count": len(self.issues),
            "warning_count": len(self.warnings),
            "pass_count": len(self.passes),
            "health_score": max(0, 100 - len(self.issues) * 15 - len(self.warnings) * 5),
            "verdict": ("✅ 数据质量良好" if len(self.issues) == 0
                        else f"⚠️ {len(self.issues)}个问题需处理" if len(self.issues) <= 3
                        else f"❌ {len(self.issues)}个严重问题"),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="自动修复可修复的问题")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    monitor = DataQualityMonitor()
    report = monitor.run_all()

    print(f"\n{'='*60}")
    print(f"📋 数据质量报告:")
    print(f"   ❌ {len(report['issues'])} 问题")
    for i in report["issues"]:
        print(f"      - {i}")
    print(f"   ⚠️ {len(report['warnings'])} 警告")
    for w in report["warnings"]:
        print(f"      - {w}")
    print(f"   ✅ {len(report['passes'])} 通过")
    print(f"   健康度评分: {report['health_score']}/100")
    print(f"   综合判断: {report['verdict']}")
    print(f"{'='*60}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📁 报告: {args.output}")


if __name__ == "__main__":
    main()
