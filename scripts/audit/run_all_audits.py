#!/usr/bin/env python3
"""
DSL 量化交易系统 全量审查执行器
================================
依次运行5层审查工具，生成综合报告。

用法:
  python3 scripts/audit/run_all_audits.py [--skip layer2] [--output audit_report.json]
"""
import os, sys, json, subprocess, argparse
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python3")

LAYERS = {
    "layer1": {
        "script": "layer1_backtest_accuracy.py",
        "desc": "历史预测准确率回测",
        "args": ["--days", "30"],
        "json_output": True,
    },
    "layer2": {
        "script": "layer2_paper_simulation.py",
        "desc": "模拟交易回放",
        "args": [],
    },
    "layer3": {
        "script": "layer3_stress_test.py",
        "desc": "压力测试套件",
        "args": ["--scenario", "all", "--dry-run"],
        "json_output": True,
    },
    "layer4": {
        "script": "layer4_data_quality.py",
        "desc": "数据质量监控",
        "args": [],
        "json_output": True,
    },
    "layer5": {
        "script": "layer5_attribution.py",
        "desc": "归因分析",
        "args": [],
        "json_output": True,
    },
    "layer6": {
        "script": "validate_api_contract.py",
        "desc": "API契约校验 — 类方法完整性与缩进漂移检测",
        "args": [],
    },
    "layer7": {
        "script": "validate_config_keys.py",
        "desc": "配置Key一致性 — YAML生产者/消费者签名对齐",
        "args": [],
    },
    "layer8": {
        "script": "validate_indentation.py",
        "desc": "缩进层级校验 — 防止类方法被嵌套吞掉",
        "args": [],
    },
    "layer9": {
        "script": "dashboard_contract_audit.py",
        "desc": "Dashboard前后端合约矩阵 — 全tab/模块字段一致性",
        "args": [],
        "json_output": True,
    },
}


def classify_layer_result(name: str, returncode: int, report: dict | None = None) -> tuple[str, str]:
    """Classify a layer without allowing empty or internally-warning output to pass."""
    if report:
        explicit = report.get("status")
        if explicit in {"passed", "warning", "failed", "skipped"}:
            return explicit, report.get("status_reason", "")

        if name == "layer1":
            da = report.get("direction_accuracy", {}) or {}
            validated = int(report.get("validated_count", da.get("total_predictions", 0)) or 0)
            accuracy = float(da.get("direction_accuracy", 0) or 0)
            if validated <= 1:
                return "failed", f"Layer1验证样本过少: {validated}"
            if accuracy < 0.48:
                return "failed", f"Layer1方向准确率过低: {accuracy:.1%}"
            if validated < 5:
                return "warning", f"Layer1验证样本不足: {validated}"

        issue_count = int(report.get("issue_count", 0) or 0)
        warning_count = int(report.get("warning_count", 0) or 0)
        if issue_count > 0:
            return "failed", f"内部问题数={issue_count}"
        if warning_count > 0:
            return "warning", f"内部警告数={warning_count}"

        if name == "layer3":
            passed = int(report.get("passed", 0) or 0)
            total = int(report.get("total_scenarios", 0) or 0)
            if total <= 0:
                return "warning", "压力测试场景数为0"
            if passed < total:
                return "failed", f"压力测试通过 {passed}/{total}"

        if name == "layer5":
            issues = report.get("summary_issues", []) or []
            verdict = str(report.get("verdict", ""))
            if issues:
                if verdict.startswith("❌"):
                    return "failed", "; ".join(str(i) for i in issues[:3])
                return "warning", "; ".join(str(i) for i in issues[:3])

    if returncode == 0:
        return "passed", ""
    if returncode == 2:
        return "warning", "脚本以warning退出"
    return "failed", f"退出码={returncode}"


def run_layer(name: str, info: dict, skip: list, audit_id: str) -> dict:
    if name in skip:
        return {"status": "skipped", "reason": "用户跳过"}

    script_path = os.path.join(AUDIT_DIR, info["script"])
    if not os.path.exists(script_path):
        return {"status": "error", "reason": f"脚本不存在: {script_path}"}

    print(f"\n{'#'*60}")
    print(f"# Layer {name[-1]}: {info['desc']}")
    print(f"{'#'*60}")

    report_path = None
    cmd = [VENV_PYTHON if os.path.exists(VENV_PYTHON) else "python3",
           script_path] + info["args"]
    if info.get("json_output"):
        output_dir = os.path.join(PROJECT_ROOT, "cache", "audit_layers")
        os.makedirs(output_dir, exist_ok=True)
        report_path = os.path.join(output_dir, f"{audit_id}_{name}.json")
        cmd += ["--output", report_path]

    try:
        result = subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        print(result.stdout)
        if result.stderr:
            print(f"[stderr]: {result.stderr[:200]}")
        parsed_report = None
        if report_path and os.path.exists(report_path):
            try:
                with open(report_path, "r", encoding="utf-8") as f:
                    parsed_report = json.load(f)
            except Exception:
                parsed_report = None
        status, reason = classify_layer_result(name, result.returncode, parsed_report)
        return {
            "status": status,
            "exit_code": result.returncode,
            "stdout_lines": len(result.stdout.split("\n")),
            "reason": reason,
            "report_path": report_path,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "reason": "超过300s"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", nargs="*", default=[], help="跳过的layer (如: layer2 layer3)")
    parser.add_argument("--output", default=None, help="JSON报告路径")
    args = parser.parse_args()

    print("=" * 70)
    print("🔍 DSL 量化交易系统 全量审查")
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   项目: {PROJECT_ROOT}")
    print("=" * 70)

    audit_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = {
        "audit_id": audit_id,
        "audit_time": datetime.now().isoformat(),
        "project": PROJECT_ROOT,
        "layers": {},
    }

    for name, info in LAYERS.items():
        if name in args.skip:
            print(f"\n⏭️  跳过 Layer {name[-1]}: {info['desc']}")
            report["layers"][name] = {"status": "skipped"}
            continue
        report["layers"][name] = run_layer(name, info, args.skip, audit_id)

    # Summary
    passed = sum(1 for l in report["layers"].values() if l["status"] == "passed")
    warnings = sum(1 for l in report["layers"].values() if l["status"] == "warning")
    failed = sum(1 for l in report["layers"].values() if l["status"] == "failed")
    skipped = sum(1 for l in report["layers"].values() if l["status"] == "skipped")
    total = len(report["layers"])
    report["summary"] = {
        "passed": passed,
        "warnings": warnings,
        "failed": failed,
        "skipped": skipped,
        "total": total,
    }

    print(f"\n{'='*70}")
    print(f"📋 审查完成: {passed}✅ {warnings}⚠️ {failed}❌ {skipped}⏭️ / {total}")
    print(f"{'='*70}")

    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📁 综合报告: {args.output}")

    if failed > 0:
        sys.exit(1)
    if warnings > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
