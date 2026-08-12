#!/usr/bin/env python3
"""
DSL 审查工具 Layer 5: 归因分析
================================
分解收益来源，识别哪些因素真正贡献alpha。

审查维度:
  A. 收益分解: ML预测贡献 vs 技术信号贡献 vs 市场beta
  B. 板块归因: 哪个行业/概念贡献最多收益
  C. Tier归因: bluechip vs core vs growth vs flex
  D. 亏损模式识别: 亏损交易是否有共同特征
  E. 信号质量衰减: 预测时间越久，准确性是否下降

用法:
  python3 scripts/audit/layer5_attribution.py [--output report.json]
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


class AttributionAnalyzer:
    """归因分析引擎"""

    def __init__(self):
        self.results = {}

    def run_all(self) -> dict:
        print("=" * 60)
        print("🔍 DSL审查 Layer5: 归因分析")
        print("=" * 60)

        analyses = [
            self.analyze_return_decomposition,
            self.analyze_tier_attribution,
            self.analyze_sector_attribution,
            self.analyze_loss_patterns,
            self.analyze_signal_decay,
        ]
        for analysis in analyses:
            print(f"\n📋 {analysis.__doc__}...")
            analysis()

        return self._summary()

    def analyze_return_decomposition(self):
        """收益分解: ML vs 技术信号 vs Beta"""
        db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
        if not os.path.exists(db_path):
            self.results["return_decomposition"] = {"error": "无交易数据"}
            return

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            trades = conn.execute(
                "SELECT * FROM trade_history WHERE COALESCE(is_valid_for_metrics, 1)=1 "
                "ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        except Exception:
            trades = conn.execute(
                "SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT 50"
            ).fetchall()
        conn.close()

        if not trades:
            self.results["return_decomposition"] = {"error": "无交易记录"}
            return

        # 计算每笔交易的收益
        amounts = []
        for t in trades:
            if t["action"] == "SELL":
                amounts.append(float(t["amount"]))

        # 读取ML预测的对应信号
        pred_file = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
        ml_contribution = 0.0
        tech_contribution = 0.0

        if os.path.exists(pred_file):
            with open(pred_file) as f:
                pred_data = json.load(f)
            predictions = {p["symbol"]: p for p in pred_data.get("predictions", [])}
            # 粗略归因: ML buy + 动量增持 = 双因子; 仅ML = ML因子
            for t in trades:
                if t["action"] == "SELL" and t["stock_code"] in predictions:
                    pred = predictions[t["stock_code"]]
                    pnl = float(t["amount"])
                    if pred.get("signal") == "buy" and pred.get("confidence", 0) >= 0.58:
                        ml_contribution += pnl * 0.6  # ML权重60%
                        tech_contribution += pnl * 0.4
                    else:
                        tech_contribution += pnl

        total_pnl = sum(amounts) if amounts else 0

        self.results["return_decomposition"] = {
            "total_trades_analyzed": len(trades),
            "total_pnl": round(total_pnl, 2),
            "avg_amount_per_trade": round(total_pnl / len(trades), 2) if trades else 0,
            "ml_contribution_est": round(ml_contribution, 2),
            "tech_contribution_est": round(tech_contribution, 2),
            "ml_vs_tech_ratio": round(ml_contribution / max(abs(tech_contribution), 1e-6), 2),
        }

        r = self.results["return_decomposition"]
        print(f"   总收益: ¥{r['total_pnl']:,.0f}")
        print(f"   ML估计贡献: ¥{r['ml_contribution_est']:,.0f}")
        print(f"   技术估计贡献: ¥{r['tech_contribution_est']:,.0f}")

    def analyze_tier_attribution(self):
        """Tier归因: bluechip vs core vs growth vs flex"""
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)
        tier_map = {s["symbol"]: s.get("tier", "unknown") for s in pool.get("master_pool", [])}

        # 从校准数据获取各tier的准确率
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        tier_acc = {}
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                cal = json.load(f)
            sa = cal.get("stock_accuracy", {})
            for code, info in sa.items():
                tier = tier_map.get(code, "unknown")
                if tier not in tier_acc:
                    tier_acc[tier] = {"accuracies": [], "count": 0}
                tier_acc[tier]["accuracies"].append(info.get("last_accuracy", 0))
                tier_acc[tier]["count"] += 1

        tiers = {}
        for t in ["bluechip", "core", "growth", "flex"]:
            data = tier_acc.get(t, {"accuracies": [], "count": 0})
            tiers[t] = {
                "count": data["count"],
                "avg_accuracy": round(np.mean(data["accuracies"]), 4) if data["accuracies"] else 0,
                "median_accuracy": round(np.median(data["accuracies"]), 4) if data["accuracies"] else 0,
                "min_accuracy": round(min(data["accuracies"]), 4) if data["accuracies"] else 0,
                "max_accuracy": round(max(data["accuracies"]), 4) if data["accuracies"] else 0,
            }

        self.results["tier_attribution"] = tiers

        print(f"   Tier准确率分布:")
        for t, info in tiers.items():
            bar = "█" * int(info["avg_accuracy"] * 30)
            print(f"   {t:>10s}: avg={info['avg_accuracy']:.1%} med={info['median_accuracy']:.1%} ({info['count']}只) {bar}")

    def analyze_sector_attribution(self):
        """板块归因: 行业/概念层面分析"""
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)

        # 提取申万行业分类
        sector_stats = {}
        for s in pool.get("master_pool", []):
            concept = s.get("concept", "")
            # 提取行业标签
            for tag in concept.split(","):
                tag = tag.strip()
                if "申万二级" in tag:
                    sector = tag.replace("A股-申万二级-", "")
                    if sector not in sector_stats:
                        sector_stats[sector] = []
                    sector_stats[sector].append(s["symbol"])

        # 计算各行业平均准确率
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        sector_acc = {}
        if os.path.exists(cal_path):
            with open(cal_path) as f:
                cal = json.load(f)
            sa = cal.get("stock_accuracy", {})

            for sector, symbols in sector_stats.items():
                accs = [sa.get(s, {}).get("last_accuracy", 0) for s in symbols if s in sa]
                if accs:
                    sector_acc[sector] = {
                        "count": len(accs),
                        "avg_accuracy": round(np.mean(accs), 4),
                    }

        # 排序展示
        sorted_sectors = sorted(sector_acc.items(), key=lambda x: x[1]["avg_accuracy"], reverse=True)
        self.results["sector_attribution"] = dict(sorted_sectors[:10])

        print(f"   行业准确率 (Top 5):")
        for sector, info in sorted_sectors[:5]:
            print(f"   {sector:>20s}: {info['avg_accuracy']:.1%} ({info['count']}只)")

    def analyze_loss_patterns(self):
        """亏损模式识别: 亏损交易是否共享特征"""
        db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
        if not os.path.exists(db_path):
            self.results["loss_patterns"] = {"error": "无交易数据"}
            return

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        try:
            trades = conn.execute(
                "SELECT * FROM trade_history WHERE action='SELL' "
                "AND COALESCE(is_valid_for_metrics, 1)=1 ORDER BY timestamp DESC LIMIT 100"
            ).fetchall()
        except Exception:
            trades = conn.execute(
                "SELECT * FROM trade_history WHERE action='SELL' ORDER BY timestamp DESC LIMIT 100"
            ).fetchall()
        conn.close()

        losers = [t for t in trades if float(t["amount"]) < 0]
        winners = [t for t in trades if float(t["amount"]) >= 0]

        if not losers:
            self.results["loss_patterns"] = {"detail": "无亏损交易"}
            return

        # 亏损交易特征
        avg_loss = np.mean([float(t.get("amount", 0)) for t in losers])
        avg_win = np.mean([float(t.get("amount", 0)) for t in winners]) if winners else 0

        # 检查亏损集中在哪些tier
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)
        tier_map = {s["symbol"]: s.get("tier", "unknown") for s in pool.get("master_pool", [])}

        loss_by_tier = {}
        for t in losers:
            tier = tier_map.get(t["stock_code"], "unknown")
            loss_by_tier[tier] = loss_by_tier.get(tier, 0) + 1

        self.results["loss_patterns"] = {
            "total_losing_trades": len(losers),
            "avg_loss": round(avg_loss, 2),
            "avg_win": round(avg_win, 2),
            "amount_factor": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0,
            "loss_by_tier": loss_by_tier,
            "loss_rate": round(len(losers) / len(trades), 4) if trades else 0,
        }

        r = self.results["loss_patterns"]
        print(f"   亏损交易: {r['total_losing_trades']}笔 (胜率{(1-r['loss_rate']):.0%})")
        print(f"   平均亏损: ¥{r['avg_loss']:,.0f} | 平均盈利: ¥{r['avg_win']:,.0f}")
        print(f"   盈亏比: {r['amount_factor']}")

    def analyze_signal_decay(self):
        """信号质量衰减: 预测越久，准确性是否下降"""
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        if not os.path.exists(cal_path):
            self.results["signal_decay"] = {"error": "无校准数据"}
            return

        with open(cal_path) as f:
            cal = json.load(f)
        sa = cal.get("stock_accuracy", {})

        # 分析准确率历史趋势
        decay_signals = []
        for code, info in sa.items():
            accs = info.get("accuracies", [])
            if len(accs) >= 5:
                # 最近5次训练的趋势
                recent = accs[-5:]
                slope = np.polyfit(range(5), recent, 1)[0]  # 线性趋势
                decay_signals.append({
                    "code": code,
                    "name": info.get("name", code),
                    "trend": round(slope, 4),
                    "current": recent[-1],
                    "initial": recent[0],
                })

        declining = [d for d in decay_signals if d["trend"] < -0.005]
        improving = [d for d in decay_signals if d["trend"] > 0.005]
        stable = [d for d in decay_signals if abs(d["trend"]) <= 0.005]

        self.results["signal_decay"] = {
            "total_stocks": len(decay_signals),
            "declining": len(declining),
            "improving": len(improving),
            "stable": len(stable),
            "worst_decliners": sorted(declining, key=lambda x: x["trend"])[:3],
        }

        r = self.results["signal_decay"]
        print(f"   信号衰减: 📉{r['declining']} 📈{r['improving']} ➡️{r['stable']}")
        if r["worst_decliners"]:
            decliners_str = ", ".join(
                "{} ({:.3f}/epoch)".format(d["name"], d["trend"])
                for d in r["worst_decliners"][:3]
            )
            print(f"   最快衰减: {decliners_str}")

    def _summary(self) -> dict:
        # 综合评分
        tier_data = self.results.get("tier_attribution", {})
        decay_data = self.results.get("signal_decay", {})
        loss_data = self.results.get("loss_patterns", {})

        overall_acc = max(
            tier_data.get("core", {}).get("avg_accuracy", 0),
            tier_data.get("bluechip", {}).get("avg_accuracy", 0),
        )
        declining_pct = decay_data.get("declining", 0) / max(decay_data.get("total_stocks", 1), 1)
        loss_rate = loss_data.get("loss_rate", 0.5)

        issues = []
        if overall_acc < 0.52:
            issues.append("整体准确率 < 52%")
        if declining_pct > 0.3:
            issues.append(f"{declining_pct:.0%} 股票在衰减")
        if loss_rate > 0.5:
            issues.append(f"亏损率 {loss_rate:.0%} > 50%")

        return {
            "audit_time": datetime.now().isoformat(),
            "results": self.results,
            "summary_issues": issues,
            "verdict": "✅ 系统归因健康" if not issues
            else f"⚠️ {'; '.join(issues)}" if len(issues) <= 2
            else f"❌ {'; '.join(issues)}",
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    analyzer = AttributionAnalyzer()
    report = analyzer.run_all()

    print(f"\n{'='*60}")
    print(f"📋 归因分析: {report['verdict']}")
    print(f"{'='*60}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📁 报告: {args.output}")


if __name__ == "__main__":
    main()
