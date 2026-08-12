#!/bin/env bash
# scripts/post_upgrade_check.sh — Gateway升级后自动校验 (v4.6.2)
#
# 解决教训62: Gateway/平台版本升级后cron必须全量验证。
# 升级后运行此脚本，自动检查:
#   1. cron任务是否全部正常
#   2. 关键数据文件是否完整
#   3. Dashboard是否可访问
#   4. adaptive_params Schema是否通过
#
# 用法:
#   bash scripts/post_upgrade_check.sh
#   bash scripts/post_upgrade_check.sh --quick   # 快速模式(跳过慢检查)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${PROJECT_ROOT}/.venv/bin/python3"
QUICK=false

for arg in "$@"; do
    case "$arg" in
        --quick) QUICK=true ;;
    esac
done

echo "============================================================"
echo "🔍 DSL Gateway升级后校验 (v4.6.2)"
echo "   时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

FAILURES=0

# 1. Cron完整性审计
echo ""
echo "📋 1/5 Cron完整性审计"
if command -v openclaw &>/dev/null; then
    if ${PYTHON} "${PROJECT_ROOT}/scripts/audit_cron_delivery.py" --skip-llm 2>&1 | tail -3; then
        echo "   ✅ Cron审计通过"
    else
        echo "   ⚠️  Cron审计发现问题（见上方输出）"
        FAILURES=$((FAILURES + 1))
    fi
else
    echo "   ⚠️  openclaw CLI不可用，跳过cron审计"
fi

# 2. adaptive_params Schema校验
echo ""
echo "📋 2/5 adaptive_params Schema校验"
if ${PYTHON} "${PROJECT_ROOT}/scripts/validate_adaptive_params.py" --quiet; then
    echo "   ✅ Schema校验通过"
else
    echo "   ❌ Schema校验失败!"
    ${PYTHON} "${PROJECT_ROOT}/scripts/validate_adaptive_params.py"
    FAILURES=$((FAILURES + 1))
fi

# 3. 关键数据文件完整性
echo ""
echo "📋 3/5 关键数据文件检查"
CRITICAL_FILES=(
    "config/adaptive_params.yaml"
    "config/master_stock_pool.yaml"
    "cache/daily_predict.json"
    "confidence_data/prediction_calibration.json"
)
for file in "${CRITICAL_FILES[@]}"; do
    full_path="${PROJECT_ROOT}/${file}"
    if [[ -f "$full_path" ]]; then
        size=$(wc -c < "$full_path" | tr -d ' ')
        if [[ "$size" -gt 10 ]]; then
            echo "   ✅ $file (${size} bytes)"
        else
            echo "   ⚠️  $file 存在但几乎为空 (${size} bytes)"
            FAILURES=$((FAILURES + 1))
        fi
    else
        echo "   ❌ $file 不存在!"
        FAILURES=$((FAILURES + 1))
    fi
done

# 4. Dashboard可达性
echo ""
echo "📋 4/5 Dashboard 可达性"
DASHBOARD_URL="http://localhost:8888/api/version"
if command -v curl &>/dev/null; then
    if version=$(curl -s --max-time 5 "$DASHBOARD_URL" 2>/dev/null); then
        echo "   ✅ Dashboard 可达, version=${version}"
    else
        echo "   ⚠️  Dashboard 不可达 ($DASHBOARD_URL)"
        FAILURES=$((FAILURES + 1))
    fi
else
    echo "   ⚠️  curl不可用，跳过Dashboard检查"
fi

# 5. Python环境 + 核心模块导入 (快速检查)
echo ""
echo "📋 5/5 Python核心模块导入"
MODULES=(
    "core.resilience"
    "core.circuit_breaker"
    "core.error_handler"
    "core.robustness_guard"
    "core.market_regime_detector"
    "common.feishu_utils"
    "common.config"
)
IMPORT_FAILS=0
for mod in "${MODULES[@]}"; do
    if ${PYTHON} -c "import sys; sys.path.insert(0,'${PROJECT_ROOT}'); import ${mod}" 2>/dev/null; then
        echo "   ✅ ${mod}"
    else
        echo "   ❌ ${mod} 导入失败"
        IMPORT_FAILS=$((IMPORT_FAILS + 1))
    fi
done
if [[ "$IMPORT_FAILS" -gt 0 ]]; then
    FAILURES=$((FAILURES + IMPORT_FAILS))
fi

# 总结
echo ""
echo "============================================================"
if [[ "$FAILURES" -eq 0 ]]; then
    echo "✅ 全部校验通过 — 系统就绪"
    echo "============================================================"
    exit 0
else
    echo "🔴 发现 ${FAILURES} 项问题 — 请优先修复cron和Schema问题"
    echo "============================================================"
    exit 1
fi
