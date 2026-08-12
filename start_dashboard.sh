#!/bin/bash
# 启动绩效看板服务
echo "🚀 正在启动DSL量化交易系统绩效看板..."
cd "$(dirname "$0")"

# 1. 自动备份持仓数据（保留最近7天备份）
BACKUP_FILE="data/simulation_portfolio.json.bak.$(date +%Y%m%d_%H%M%S)"
cp data/simulation_portfolio.json "$BACKUP_FILE"
echo "✅ 数据已备份到: $BACKUP_FILE"
# 清理7天前的旧备份
find data -name "simulation_portfolio.json.bak.*" -mtime +7 -delete
echo "✅ 已清理7天以上旧备份"

# 2. 检查8082端口占用，杀掉旧进程
PORT=8082
if command -v lsof >/dev/null 2>&1; then
    if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
        OLD_PID=$(lsof -Pi :$PORT -sTCP:LISTEN -t)
        echo "⚠️  端口$PORT已被进程$OLD_PID占用，正在终止..."
        kill -9 $OLD_PID
        sleep 2
    fi
else
    # 兼容mac没有lsof的情况
    OLD_PID=$(netstat -anvp tcp 2>/dev/null | grep LISTEN | grep "\.8082 " | awk '{print $9}' | cut -d '/' -f1)
    if [ -n "$OLD_PID" ]; then
        echo "⚠️  端口$PORT已被进程$OLD_PID占用，正在终止..."
        kill -9 $OLD_PID
        sleep 2
    fi
fi

# 3. 检查是否安装了flask
if ! python3 -c "import flask" >/dev/null 2>&1; then
    echo "📦 安装依赖flask..."
    pip3 install flask
fi

# 4. 后台启动服务，输出日志
echo "✅ 服务启动中，访问地址: http://localhost:$PORT"
nohup python3 dashboard_api.py > logs/dashboard.log 2>&1 &
echo "✅ 服务已后台启动，PID: $!"
echo "📝 日志路径: logs/dashboard.log"
