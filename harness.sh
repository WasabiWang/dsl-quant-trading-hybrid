#!/bin/bash
# DSL Test Pyramid — Master Runner
# 用法: ./harness.sh test [l0|l1|l2|l3|property|golden|golden-strict|all]

cd "$(dirname "$0")"

# Python 解释器: 优先本地 venv, 否则回退系统 python3
# (全新公开克隆/换机可能没有 .venv, 不能让 harness 在入口就报 no such file)
PYTHON_BIN=".venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(command -v python3 || echo python3)"
fi

echo "🐍 Python: $PYTHON_BIN"

run_test() {
    local name="$1"; shift
    local target="$1"
    echo ""
    # 2026-09-13: pytest 风格的测试文件(无 `if __name__ == "__main__"` 入口)直接执行
    # 等于 0 用例空跑 —— 仍 exit 0 并打印"全部测试通过"(假绿)。
    # 这类文件必须交给 pytest 收集执行; 脚本式(有 __main__/argparse, 如 golden_test.py --strict)
    # 与 audit 校验脚本保持直接执行不变。
    if [ -f "$target" ] && [ "${target##*.}" = "py" ] && ! grep -q '__main__' "$target"; then
        PYTHONPATH="$(dirname "$0"):${PYTHONPATH:-}" "$PYTHON_BIN" -m pytest "$@" -q 2>&1 | grep -v "error_handler\|feishu_alert\|INFO\|ERROR\|WARNING"
    else
        PYTHONPATH="$(dirname "$0"):${PYTHONPATH:-}" "$PYTHON_BIN" "$@" 2>&1 | grep -v "error_handler\|feishu_alert\|INFO\|ERROR\|WARNING"
    fi
    local rc=${PIPESTATUS[0]}
    if [ $rc -ne 0 ]; then
        echo "  ❌ $name FAILED (exit=$rc)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

FAIL_COUNT=0

case "${1:-all}" in
    l0)
        run_test "L0-Unit" tests/test_unit.py
        ;;
    l1)
        run_test "L1-Component" tests/test_component.py
        ;;
    l2)
        run_test "L2-Integration" tests/pipeline_test.py
        ;;
    l3)
        run_test "L3-E2E" tests/test_e2e.py
        ;;
    property)
        run_test "Property-Based" tests/test_property.py
        ;;
    golden)
        run_test "Golden-Test" tests/golden_test.py
        ;;
    golden-strict)
        # pre-deploy/CI: 基线必须齐备, 任何 SKIP(无私有配置/无基线)都算失败
        run_test "Golden-Strict" tests/golden_test.py --strict
        ;;
    quick)
        run_test "Harness-Quick" tests/harness.py -- --quick
        ;;
    all)
        echo "🔺 DSL Test Pyramid — Full Run 🔺"
        run_test "L0-Unit" tests/test_unit.py
        run_test "L1-Component" tests/test_component.py
        run_test "Property-Based" tests/test_property.py
        run_test "L2-Integration" tests/pipeline_test.py
        run_test "L3-E2E" tests/test_e2e.py
        run_test "Golden-Test" tests/golden_test.py
        echo "🔺 Audits — Structural Invariants 🔺"
        run_test "Layer6-API-Contract" scripts/audit/validate_api_contract.py
        run_test "Layer7-Config-Keys" scripts/audit/validate_config_keys.py
        run_test "Layer8-Indentation" scripts/audit/validate_indentation.py
        ;;
    audit)
        echo "🔺 Structural Audits 🔺"
        run_test "Layer6-API-Contract" scripts/audit/validate_api_contract.py
        run_test "Layer7-Config-Keys" scripts/audit/validate_config_keys.py
        run_test "Layer8-Indentation" scripts/audit/validate_indentation.py
        run_test "Pool-Structure" scripts/audit/pool_structure_audit.py
        ;;
    *)
        echo "Usage: $0 {l0|l1|l2|l3|property|golden|golden-strict|quick|all|audit}"
        exit 1
        ;;
esac

echo ""
echo "============================================"
if [ $FAIL_COUNT -eq 0 ]; then
    echo "  ✅ 全部测试通过"
else
    echo "  ❌ $FAIL_COUNT 个测试失败"
fi
echo "============================================"
exit $FAIL_COUNT
