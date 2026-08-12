#!/bin/bash
# DSL Test Pyramid — Master Runner
# 用法: ./harness.sh test [l0|l1|l2|l3|property|golden|all]

cd "$(dirname "$0")"

run_test() {
    local name="$1" script="$2"
    echo ""
    PYTHONPATH="$(dirname "$0"):${PYTHONPATH:-}" .venv/bin/python3 "$script" 2>&1 | grep -v "error_handler\|feishu_alert\|INFO\|ERROR\|WARNING"
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
        echo "Usage: $0 {l0|l1|l2|l3|property|golden|quick|all|audit}"
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
