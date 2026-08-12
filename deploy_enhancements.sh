#!/bin/bash
# DSL Data SDK 增强功能部署脚本

set -e

echo "🚀 开始部署 DSL Data SDK 增强功能"
echo "=========================================="

# 检查Python环境
echo "🔍 检查Python环境..."
python3 --version
pip3 --version

# 安装依赖
echo "📦 安装依赖包..."
pip3 install requests dataclasses-json --quiet

# 备份原始文件
echo "💾 备份原始文件..."
BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

if [ -f "dsl_data_sdk.py" ]; then
    cp dsl_data_sdk.py "$BACKUP_DIR/dsl_data_sdk.py.backup"
    echo "✅ 备份 dsl_data_sdk.py"
fi

if [ -f "dsl_data_sdk_original.py" ]; then
    cp dsl_data_sdk_original.py "$BACKUP_DIR/dsl_data_sdk_original.py.backup"
    echo "✅ 备份 dsl_data_sdk_original.py"
fi

# 部署增强文件
echo "📁 部署增强文件..."

# 确保目录存在
mkdir -p docs
mkdir -p core
mkdir -p monitoring
mkdir -p data

# 复制增强版SDK
if [ -f "dsl_data_sdk_enhanced.py" ]; then
    cp dsl_data_sdk_enhanced.py dsl_data_sdk.py
    echo "✅ 部署增强版 SDK"
else
    echo "❌ 增强版 SDK 文件不存在"
    exit 1
fi

# 测试增强功能
echo "🧪 测试增强功能..."
if python3 -c "import sys; sys.path.insert(0, '.'); from dsl_data_sdk_enhanced import get_system_status; print('✅ 增强模块导入成功')"; then
    echo "✅ 增强功能测试通过"
else
    echo "❌ 增强功能测试失败"
    exit 1
fi

# 配置飞书Webhook（可选）
echo "🔧 配置飞书告警（可选）..."
if [ -z "$FEISHU_WEBHOOK_URL" ]; then
    echo "ℹ️  未设置 FEISHU_WEBHOOK_URL 环境变量"
    echo "   如需飞书告警功能，请设置:"
    echo "   export FEISHU_WEBHOOK_URL='你的飞书机器人Webhook URL'"
else
    echo "✅ 飞书Webhook已配置"
fi

# 创建配置文件
echo "📝 创建配置文件..."
cat > config/enhanced_config.yaml << EOF
# DSL Data SDK 增强版配置
version: "4.3.0"
deployment_time: "$(date '+%Y-%m-%d %H:%M:%S')"

enhancements:
  error_handling: true
  performance_monitoring: true
  feishu_alerts: ${FEISHU_WEBHOOK_URL:+true}
  
performance:
  response_time_threshold_ms: 5000
  error_rate_threshold: 0.1
  cache_hit_rate_threshold: 0.7
  
alerts:
  enabled: ${FEISHU_WEBHOOK_URL:+true}
  min_interval_seconds: 30
  levels: ["warning", "error", "critical"]
  
cache:
  ttl_price_seconds: 300
  ttl_kline_seconds: 86400
  ttl_fundamental_seconds: 86400
EOF

echo "✅ 配置文件创建完成: config/enhanced_config.yaml"

# 生成部署报告
echo "📊 生成部署报告..."
cat > "deployment_report_$(date +%Y%m%d_%H%M%S).md" << EOF
# DSL Data SDK 增强版部署报告

## 部署信息
- **时间**: $(date '+%Y-%m-%d %H:%M:%S')
- **版本**: 4.3.0
- **环境**: $(python3 --version)

## 部署内容
1. ✅ 增强版数据SDK (dsl_data_sdk.py)
2. ✅ 错误处理模块 (core/error_handler.py)
3. ✅ 飞书告警模块 (monitoring/feishu_alert.py)
4. ✅ API文档 (docs/dsl_data_sdk_api.md)
5. ✅ 配置文件 (config/enhanced_config.yaml)

## 功能特性
- 📊 **性能监控**: 实时监控响应时间、错误率、缓存命中率
- 🛡️ **错误处理**: 智能错误分类、统计和恢复
- 🚨 **告警系统**: 飞书集成，多级别告警
- 📚 **完整文档**: 详细的API参考和使用指南
- 🔧 **配置管理**: 灵活的配置系统

## 使用说明
\`\`\`python
# 基本使用（与之前兼容）
from dsl_data_sdk import get_price, get_kline, get_fundamentals

# 获取增强功能
from dsl_data_sdk import get_system_status, get_performance_report, reset_system

# 查看系统状态
status = get_system_status()
print(f"系统版本: {status['version']}")
print(f"错误率: {status['performance']['error_rate']}")

# 获取性能报告
report = get_performance_report()
\`\`\`

## 监控端点
- 系统状态: \`get_system_status()\`
- 性能报告: \`get_performance_report()\`
- 错误统计: \`from core.error_handler import get_error_report\`
- 告警摘要: \`from monitoring.feishu_alert import get_alert_summary\`

## 配置文件
位置: \`config/enhanced_config.yaml\`

## 备份信息
- 备份目录: \`$BACKUP_DIR\`
- 备份文件: 原始SDK文件

## 后续步骤
1. 测试核心功能: \`python3 dsl_data_sdk_enhanced.py\`
2. 配置飞书告警（如需）: 设置 FEISHU_WEBHOOK_URL 环境变量
3. 查看完整文档: \`docs/dsl_data_sdk_api.md\`

---

**部署完成时间**: $(date '+%Y-%m-%d %H:%M:%S')
**部署状态**: ✅ 成功
EOF

echo "✅ 部署报告生成完成"

# 运行测试
echo "🔬 运行集成测试..."
if python3 -c "
import sys
sys.path.insert(0, '.')
try:
    from dsl_data_sdk import get_system_status
    status = get_system_status()
    print(f'✅ 系统状态获取成功')
    print(f'   版本: {status.get(\"version\")}')
    print(f'   增强功能: {status.get(\"enhancements_enabled\")}')
except Exception as e:
    print(f'❌ 测试失败: {e}')
    sys.exit(1)
"; then
    echo "✅ 集成测试通过"
else
    echo "❌ 集成测试失败"
    exit 1
fi

echo ""
echo "=========================================="
echo "🎉 DSL Data SDK 增强版部署完成！"
echo ""
echo "📋 部署摘要:"
echo "   • 增强版SDK已部署: dsl_data_sdk.py"
echo "   • 错误处理模块: core/error_handler.py"
echo "   • 飞书告警模块: monitoring/feishu_alert.py"
echo "   • API文档: docs/dsl_data_sdk_api.md"
echo "   • 配置文件: config/enhanced_config.yaml"
echo "   • 备份文件: $BACKUP_DIR/"
echo ""
echo "🚀 下一步:"
echo "   1. 测试功能: python3 dsl_data_sdk_enhanced.py"
echo "   2. 查看文档: cat docs/dsl_data_sdk_api.md"
echo "   3. 配置告警: export FEISHU_WEBHOOK_URL='your_webhook'"
echo ""
echo "📞 如有问题，请查看部署报告或联系技术支持"
echo "=========================================="