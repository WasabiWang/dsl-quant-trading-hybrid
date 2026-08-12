#!/bin/bash
# DSL稳定性监控配置脚本

echo "🔧 配置DSL稳定性监控"
echo "======================"

# 检查脚本存在
if [ ! -f "scripts/stability_monitor.py" ]; then
    echo "❌ 稳定性监控脚本不存在"
    exit 1
fi

echo "✅ 稳定性监控脚本存在"

# 创建日志目录
LOG_DIR="$HOME/.openclaw/logs/stability"
mkdir -p "$LOG_DIR"
echo "✅ 日志目录: $LOG_DIR"

# 测试监控脚本
echo ""
echo "🧪 测试监控脚本..."
python3 scripts/stability_monitor.py status

# 创建cron任务配置
echo ""
echo "📋 建议的cron配置:"
echo "------------------"
echo "# DSL稳定性监控任务"
echo "# 每小时检查系统状态"
echo "0 * * * * cd $PWD && python3 scripts/stability_monitor.py status >> $LOG_DIR/status_\$(date +\%Y\%m\%d).log 2>&1"
echo ""
echo "# 每天9点检查系统健康"
echo "0 9 * * * cd $PWD && python3 scripts/stability_monitor.py health >> $LOG_DIR/health_\$(date +\%Y\%m\%d).log 2>&1"
echo ""
echo "# 每天2点重置熔断器（预防性维护）"
echo "0 2 * * * cd $PWD && python3 -c \"from dsl_data_sdk import reset_data_fetch_circuit; reset_data_fetch_circuit()\" >> $LOG_DIR/reset_\$(date +\%Y\%m\%d).log 2>&1"

echo ""
echo "💡 使用方法:"
echo "-----------"
echo "1. 手动检查状态: python3 scripts/stability_monitor.py status"
echo "2. 检查系统健康: python3 scripts/stability_monitor.py health"
echo "3. 重置熔断器: python3 scripts/stability_monitor.py reset data_fetch"
echo "4. 详细状态: python3 scripts/stability_monitor.py status --detailed"

echo ""
echo "🎯 配置完成!"
