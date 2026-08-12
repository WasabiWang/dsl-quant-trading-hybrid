#!/usr/bin/env python3
"""
Alpha-Quant 自迭代引擎 v1.0
周末空闲时段自动运行，对历史分析进行回溯验证并改进框架

机制:
  1. 每周收集 alpha-quant-research 产出的个股分析
  2. 比对预测目标价与现价，计算准确率
  3. 分析系统误差方向(过度乐观/悲观)
  4. 自动调整挤水分系数/PE区间/信号阈值
  5. 输出迭代报告

独立于DSL系统，但复用数据源(麦蕊API)
"""
import os, sys, json, yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import requests

# ── 路径配置 ──
WORKSPACE = Path.home() / ".openclaw" / "workspace"
SKILL_DIR = Path.home() / ".agents" / "skills" / "alpha-quant-research"
REPORT_DIR = WORKSPACE / "reports" / "alpha-quant"
DATA_DIR = WORKSPACE / "data" / "alpha-quant-iteration"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 历史分析记录文件
ANALYSIS_HISTORY = DATA_DIR / "analysis_history.json"
# 迭代参数(挤水分系数/PE区间等)
ITERATION_PARAMS = DATA_DIR / "iteration_params.json"
# 精度追踪
ACCURACY_LOG = DATA_DIR / "accuracy_log.json"


def load_history() -> list:
    """加载所有历史分析记录"""
    if ANALYSIS_HISTORY.exists():
        with open(ANALYSIS_HISTORY) as f:
            return json.load(f)
    return []


def save_history(history: list):
    with open(ANALYSIS_HISTORY, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_params() -> dict:
    """加载迭代参数"""
    default = {
        "version": 2,
        "挤水分系数": {
            "一般行业": 0.85,
            "科技/医药/高波": 0.80,
        },
        "PE区间": {
            "悲观": 15,
            "中性": 20,
            "乐观": 25,
        },
        "信号阈值": {
            "买入": 0.05,   # 预测空间>5%才发买入
            "卖出": -0.05,  # 预测空间<-5%才发卖出
        },
        "政策风口权重系数": 1.0,
        "主力性质权重系数": 1.0,
        "迭代记录": [],
        "last_updated": None,
    }
    if ITERATION_PARAMS.exists():
        with open(ITERATION_PARAMS) as f:
            stored = json.load(f)
            # 合并保留自定义字段
            default.update(stored)
    return default


def save_params(params: dict):
    params["last_updated"] = datetime.now().isoformat()
    with open(ITERATION_PARAMS, "w") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)


def load_accuracy_log() -> dict:
    if ACCURACY_LOG.exists():
        with open(ACCURACY_LOG) as f:
            return json.load(f)
    return {"by_stock": {}, "by_week": [], "overall": {"total": 0, "correct_direction": 0}}


def save_accuracy_log(log: dict):
    with open(ACCURACY_LOG, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════
# 数据获取
# ════════════════════════════════════════

MAIRUI_LICENCE = os.getenv("MAIRUI_LICENCE", "")


def get_stock_price(code: str) -> dict:
    """获取实时股价(麦蕊API)"""
    if not MAIRUI_LICENCE:
        print("⚠️ MAIRUI_LICENCE未配置，跳过麦蕊实时股价")
        return {}
    try:
        r = requests.get(f"https://api.mairuiapi.com/hsrl/ssjy/{code}/{MAIRUI_LICENCE}", timeout=5)
        if r.status_code == 200:
            d = r.json()
            return {
                "price": float(d.get("p", 0)),
                "change": float(d.get("pc", 0)),
                "pe": float(d.get("pe", 0)),
            }
    except Exception:
        pass
    return {"price": 0, "change": 0, "pe": 0}


# ════════════════════════════════════════
# 比对与准确率计算
# ════════════════════════════════════════

def record_analysis(analysis_data: dict):
    """记录一次个股分析到历史库"""
    history = load_history()
    record = {
        "stock_code": analysis_data.get("code", ""),
        "stock_name": analysis_data.get("name", ""),
        "analysis_date": analysis_data.get("date", datetime.now().strftime("%Y-%m-%d")),
        "当时的股价": analysis_data.get("current_price", 0),
        "短期目标价(3M)": analysis_data.get("short_term_target", 0),
        "中期目标价(1Y)": analysis_data.get("mid_term_target", 0),
        "长期目标价(3Y)": analysis_data.get("long_term_target", 0),
        "建议": analysis_data.get("recommendation", ""),
        "评估EPS": analysis_data.get("eps_estimates", {}),
        "PE区间": analysis_data.get("pe_range", {}),
        "主力性质": analysis_data.get("main_force_type", ""),
        "政策评分": analysis_data.get("policy_score", 0),
    }
    history.append(record)
    save_history(history)
    print(f"  ✅ 已记录分析: {analysis_data.get('name','')}({analysis_data.get('code','')})")
    return record


def compare_predictions() -> dict:
    """比对所有历史分析的预测与现价"""
    history = load_history()
    params = load_params()
    accuracy_log = load_accuracy_log()
    now = datetime.now()

    results = []
    total = 0
    correct_direction = 0
    total_magnitude_error = 0
    magnitude_count = 0

    # 统计各维度的系统偏差
    biases = {
        "短期(3M)": [],
        "中期(1Y)": [],
        "长期(3Y)": [],
    }
    # 按股票统计
    stocks_seen = set()
    
    for rec in history:
        code = rec.get("stock_code", "")
        if code in stocks_seen:
            continue  # 每个股票只评估最新一次分析
        stocks_seen.add(code)

        # 跳过最近30天内的分析(还没到验证期)
        try:
            ana_date = datetime.strptime(rec["analysis_date"][:10], "%Y-%m-%d")
        except Exception:
            continue
        days_since = (now - ana_date).days
        if days_since < 14:
            continue  # 至少14天才有意义

        # 获取现价
        price_data = get_stock_price(code)
        current_price = price_data.get("price", 0)
        if current_price == 0:
            continue

        base_price = rec.get("当时的股价", 0)
        if base_price == 0:
            continue
        actual_change = (current_price - base_price) / base_price

        # 评估每个时间维度的预测
        eval_types = [
            ("短期(3M)", "短期目标价(3M)", 30),
            ("中期(1Y)", "中期目标价(1Y)", 90),
        ]
        for eval_name, target_key, min_days_needed in eval_types:
            target = rec.get(target_key, 0)
            if not target or target == 0:
                continue
            if days_since < min_days_needed:
                continue  # 时间不够长

            predicted_change = (target - base_price) / base_price

            direction_correct = (predicted_change > 0 and actual_change > 0) or \
                                (predicted_change < 0 and actual_change < 0)
            magnitude_error = abs(actual_change - predicted_change)
            # 偏差(正则: 预测值符号是否倾向于过乐观)
            bias = actual_change - predicted_change

            total += 1
            if direction_correct:
                correct_direction += 1
            total_magnitude_error += magnitude_error
            magnitude_count += 1
            biases[eval_name].append(bias)

            results.append({
                "stock": f"{rec.get('stock_name','')}({code})",
                "时间维度": eval_name,
                "分析日": rec["analysis_date"][:10],
                "天数": days_since,
                "当时价": base_price,
                "现价": current_price,
                "实际变动": f"{actual_change:+.1%}",
                "预测变动": f"{predicted_change:+.1%}",
                "方向正确": direction_correct,
                "幅度偏差": f"{magnitude_error:.1%}",
            })

    # 计算总体准确率
    direction_accuracy = correct_direction / total if total > 0 else 0
    avg_magnitude_error = total_magnitude_error / magnitude_count if magnitude_count > 0 else 0

    # 分析系统偏差
    bias_analysis = {}
    for period, bias_list in biases.items():
        if bias_list:
            avg_bias = sum(bias_list) / len(bias_list)
            bias_analysis[period] = {
                "平均偏差": f"{avg_bias:+.1%}",
                "方向": "过度乐观" if avg_bias < -0.05 else ("过度悲观" if avg_bias > 0.05 else "基本无偏"),
                "样本数": len(bias_list),
            }

    # 更新全局精度记录
    accuracy_log["overall"] = {
        "total": accuracy_log["overall"]["total"] + total if total > 0 else accuracy_log["overall"]["total"],
        "correct_direction": accuracy_log["overall"]["correct_direction"] + correct_direction,
        "last_updated": now.isoformat(),
    }

    # 更新按股票统计
    for r in results:
        code = r["stock"]
        if code not in accuracy_log["by_stock"]:
            accuracy_log["by_stock"][code] = {"total": 0, "correct": 0}
        accuracy_log["by_stock"][code]["total"] += 1
        if r["方向正确"]:
            accuracy_log["by_stock"][code]["correct"] += 1

    # 周记录
    week_key = now.strftime("%Y-W%W")
    week_entry = next((w for w in accuracy_log["by_week"] if w["week"] == week_key), None)
    if not week_entry:
        week_entry = {"week": week_key, "total": 0, "correct": 0}
        accuracy_log["by_week"].append(week_entry)
    week_entry["total"] += total
    if correct_direction:
        week_entry["correct"] += correct_direction

    save_accuracy_log(accuracy_log)

    return {
        "comparison_date": now.strftime("%Y-%m-%d"),
        "total_predictions_validated": total,
        "direction_accuracy": f"{direction_accuracy:.1%}",
        "correct": correct_direction,
        "total": total,
        "avg_magnitude_error": f"{avg_magnitude_error:.1%}",
        "bias_analysis": bias_analysis,
        "details": results[:20],  # 最多显示20条
        "overall_history": accuracy_log["overall"],
    }


def adjust_framework(comparison: dict, params: dict) -> dict:
    """根据比对结果自动调整框架参数"""
    accuracy_str = comparison.get("direction_accuracy", "0%")
    accuracy = float(accuracy_str.replace("%", "")) / 100
    total = comparison.get("total", 0)

    if total < 3:
        print("  ⏸️ 样本不足(<3), 跳过参数调整")
        return params

    # 记录本次迭代
    iter_record = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "samples": total,
        "direction_accuracy": accuracy,
        "adjusted_params": {},
    }

    # 1. 调整挤水分系数
    squeeze = params["挤水分系数"]
    bias = comparison.get("bias_analysis", {})
    for period, ba in bias.items():
        bias_str = ba.get("平均偏差", "0%")
        bias_val = float(bias_str.replace("%", "").replace("+", "")) / 100
        # 过度乐观(实际涨得少或跌得多) → 加大挤水分力度
        if bias_val < -0.1:  # 偏差 > 10%的过度乐观
            squeeze["一般行业"] = max(0.70, squeeze["一般行业"] - 0.02)
            squeeze["科技/医药/高波"] = max(0.65, squeeze["科技/医药/高波"] - 0.02)
            iter_record["adjusted_params"]["挤水分系数"] = "降低0.02(过度乐观)"
        elif bias_val > 0.1:  # 过度悲观
            squeeze["一般行业"] = min(0.95, squeeze["一般行业"] + 0.02)
            squeeze["科技/医药/高波"] = min(0.95, squeeze["科技/医药/高波"] + 0.02)
            iter_record["adjusted_params"]["挤水分系数"] = "增加0.02(过度悲观)"

    # 2. 调整PE区间
    pe = params["PE区间"]
    if accuracy < 0.5 and total >= 5:
        # 方向准确率<50% → PE区间过于激进
        pe["悲观"] = max(10, pe["悲观"] - 1)
        pe["中性"] = max(12, pe["中性"] - 1)
        pe["乐观"] = max(15, pe["乐观"] - 1)
        iter_record["adjusted_params"]["PE区间"] = "整体下调(准确率<50%)"
    elif accuracy > 0.8:
        # 准确率>80% → PE区间可以略扩张
        pe["乐观"] = min(35, pe["乐观"] + 1)
        iter_record["adjusted_params"]["PE区间"] = "上调乐观端(准确率>80%)"

    # 3. 调整政策风口权重
    if accuracy < 0.4:
        params["政策风口权重系数"] = max(0.5, params["政策风口权重系数"] - 0.1)
        iter_record["adjusted_params"]["政策风口权重"] = f"降至{params['政策风口权重系数']:.1f}"

    iter_record["coeffs"] = {
        "挤水分_一般": squeeze["一般行业"],
        "挤水分_科技": squeeze["科技/医药/高波"],
        "PE_悲观": pe["悲观"],
        "PE_中性": pe["中性"],
        "PE_乐观": pe["乐观"],
        "政策权重": params["政策风口权重系数"],
    }

    params["迭代记录"].append(iter_record)
    params["迭代记录"] = params["迭代记录"][-52:]  # 保留1年
    save_params(params)
    return params


def generate_iteration_report(comparison: dict, params: dict) -> str:
    """生成迭代报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total = comparison.get("total", 0)
    iter_count = len(params.get("迭代记录", []))

    report = f"""# 🔄 Alpha-Quant 自迭代周报

📅 {now} | 第{iter_count}次迭代

---

## 📊 预测准确度

| 指标 | 数值 |
|:--|:--:|
| 验证样本数 | {total} |
| **方向准确率** | **{comparison.get('direction_accuracy', 'N/A')}** |
| 平均幅度偏差 | {comparison.get('avg_magnitude_error', 'N/A')} |
| 正确预测 | {comparison.get('correct', 0)}/{total} |

## 📈 系统偏差分析

| 时间维度 | 平均偏差 | 倾向 | 样本数 |
|:--|:--:|:--:|:--:|
"""
    bias = comparison.get("bias_analysis", {})
    for period, ba in bias.items():
        report += f"| {period} | {ba.get('平均偏差','?')} | {ba.get('方向','?')} | {ba.get('样本数',0)} |\n"

    report += f"""
## 🔧 当前框架参数

| 参数 | 值 |
|:--|:--:|
| 挤水分(一般行业) | {params['挤水分系数']['一般行业']} |
| 挤水分(科技/医药) | {params['挤水分系数']['科技/医药/高波']} |
| PE区间(悲观/中性/乐观) | {params['PE区间']['悲观']}x/{params['PE区间']['中性']}x/{params['PE区间']['乐观']}x |
| 政策风口权重 | {params.get('政策风口权重系数',1.0)} |
| 主力性质权重 | {params.get('主力性质权重系数',1.0)} |

## 🔬 近期迭代历史

| 日期 | 样本 | 准确率 | 调整内容 |
|:--|:--:|:--:|:--|
"""
    for it in params["迭代记录"][-8:]:
        date = it.get("date", "?")
        samples = it.get("samples", "?")
        acc = f"{it.get('direction_accuracy',0):.0%}" if isinstance(it.get('direction_accuracy'), float) else it.get('direction_accuracy', '?')
        adj = str(it.get("adjusted_params", {}))
        report += f"| {date} | {samples} | {acc} | {adj} |\n"

    if comparison.get("details"):
        report += f"""
## 📋 明细比对 (Top {min(len(comparison['details']), 10)})

| 股票 | 维度 | 天数 | 预测变动 | 实际变动 | 方向 | 幅度偏差 |
|:--|:--:|:--:|:--:|:--:|:--:|:--:|
"""
        for d in comparison["details"][:10]:
            emoji = "✅" if d["方向正确"] else "❌"
            report += f"| {d['stock']} | {d['时间维度']} | {d['天数']}d | {d['预测变动']} | {d['实际变动']} | {emoji} | {d['幅度偏差']} |\n"

    overall = comparison.get("overall_history", {})
    if overall.get("total", 0) > 0:
        overall_acc = overall["correct_direction"] / overall["total"]
        report += f"""
## 📊 累计精度

| 指标 | 数值 |
|:--|:--|
| 累计验证 | {overall['total']}次 |
| 累计正确 | {overall['correct_direction']}次 |
| **累计准确率** | **{overall_acc:.1%}** |
"""
    report += f"""
---
*Alpha-Quant 自迭代引擎 v1.0 | 独立于DSL系统*
*每次周末自动运行: 比对→分析→调参→输出*
"""
    return report


def main():
    print(f"{'='*60}")
    print(f"🔄 Alpha-Quant 自迭代引擎 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}")

    # 1. 加载参数
    params = load_params()
    print(f"\n📐 当前框架参数:")
    print(f"   挤水分: 一般={params['挤水分系数']['一般行业']}, 科技={params['挤水分系数']['科技/医药/高波']}")
    print(f"   PE区间: {params['PE区间']['悲观']}x/{params['PE区间']['中性']}x/{params['PE区间']['乐观']}x")
    iter_count = len(params.get("迭代记录", []))
    print(f"   已迭代次数: {iter_count}")

    # 2. 获取所有已分析股票的最新价格并比对
    print(f"\n🔍 比对历史分析预测 vs 现价...")
    history = load_history()
    print(f"   历史记录: {len(history)}条")
    comparison = compare_predictions()

    total = comparison.get("total", 0)
    if total == 0:
        print(f"   暂无足够数据验证(需要≥14天前且有目标价的记录)")
        print(f"\n   💡 下次分析时使用 record_analysis() 记录即可")
        # 生成空报告
        report = generate_iteration_report(comparison, params)
        report_path = REPORT_DIR / f"iteration_report_{datetime.now().strftime('%Y%m%d')}.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"\n📄 空报告已生成: {report_path}")
        print(f"{'='*60}")
        return 0

    print(f"   验证样本: {total}")
    print(f"   方向准确率: {comparison.get('direction_accuracy','?')}")
    print(f"   幅度偏差: {comparison.get('avg_magnitude_error','?')}")

    # 偏差分析
    bias = comparison.get("bias_analysis", {})
    for period, ba in bias.items():
        print(f"   {period}: {ba.get('平均偏差','?')} ({ba.get('方向','?')})")

    # 3. 自动调整框架参数
    print(f"\n🔧 自适应调参...")
    params = adjust_framework(comparison, params)
    if params.get("迭代记录") and params["迭代记录"][-1].get("adjusted_params"):
        for k, v in params["迭代记录"][-1]["adjusted_params"].items():
            print(f"   {k}: {v}")

    # 4. 生成迭代报告
    report = generate_iteration_report(comparison, params)
    report_path = REPORT_DIR / f"iteration_report_{datetime.now().strftime('%Y%m%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n📄 迭代报告已保存: {report_path}")

    print(f"\n{'='*60}")
    print(f"✅ 第{iter_count+1}次迭代完成")
    print(f"📊 方向准确率: {comparison.get('direction_accuracy','?')}")
    print(f"{'='*60}")
    return 0


def record_analysis_cli():
    """CLI接口: 被alpha-quant-research skill调用记录分析"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--price", type=float, required=True)
    parser.add_argument("--short", type=float, help="短期目标价(3M)")
    parser.add_argument("--mid", type=float, help="中期目标价(1Y)")
    parser.add_argument("--long", type=float, help="长期目标价(3Y)")
    parser.add_argument("--recommendation")
    parser.add_argument("--eps-2025e", type=float)
    parser.add_argument("--eps-2026e", type=float)
    parser.add_argument("--eps-2027e", type=float)
    parser.add_argument("--pe-pessimistic", type=int)
    parser.add_argument("--pe-neutral", type=int)
    parser.add_argument("--pe-optimistic", type=int)
    parser.add_argument("--main-force")
    parser.add_argument("--policy-score", type=int)
    args = parser.parse_args()

    data = {
        "code": args.code,
        "name": args.name,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "current_price": args.price,
        "short_term_target": args.short or 0,
        "mid_term_target": args.mid or 0,
        "long_term_target": args.long or 0,
        "recommendation": args.recommendation or "",
        "eps_estimates": {
            "2025E": args.eps_2025e or 0,
            "2026E": args.eps_2026e or 0,
            "2027E": args.eps_2027e or 0,
        },
        "pe_range": {
            "悲观": args.pe_pessimistic or 15,
            "中性": args.pe_neutral or 20,
            "乐观": args.pe_optimistic or 25,
        },
        "main_force_type": args.main_force or "",
        "policy_score": args.policy_score or 0,
    }
    record_analysis(data)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--record":
        sys.argv.pop(1)
        record_analysis_cli()
    else:
        sys.exit(main())
