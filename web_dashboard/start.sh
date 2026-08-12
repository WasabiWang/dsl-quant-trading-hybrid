#!/bin/bash
# DSL Web Dashboard 启动器
# 用法: ./web_dashboard/start.sh [stop|restart|status|logs]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$(dirname "$SCRIPT_DIR")"
PID_FILE="$SCRIPT_DIR/.dashboard.pid"
LOG_FILE="$SCRIPT_DIR/dashboard.log"
PORT=${DSL_DASHBOARD_PORT:-8888}

start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "⚠️  Dashboard已在运行 (PID: $(cat "$PID_FILE"))"
    return
  fi
  echo "🚀 启动DSL Dashboard: http://localhost:$PORT"
  cd "$WORKSPACE"
  nohup python3 "$SCRIPT_DIR/server.py" >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅ Dashboard已启动 (PID: $(cat "$PID_FILE"))"
  else
    echo "❌ 启动失败，查看日志: tail -f $LOG_FILE"
  fi
  # 自动重启Cloudflare Tunnel（如已启用）
  bash "$SCRIPT_DIR/start_tunnel.sh" start 2>/dev/null &
}

stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "⚠️  未找到PID文件"
    return
  fi
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "⏹️  Dashboard已停止 (PID: $PID)"
  else
    echo "⚠️  进程已不存在"
  fi
  rm -f "$PID_FILE"
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "🟢 运行中 (PID: $(cat "$PID_FILE"), 端口: $PORT)"
    curl -s http://localhost:$PORT/api/status | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'   信号: 🟢{d[\"signal_count_buy\"]} 🔴{d[\"signal_count_sell\"]} ⚪{d[\"signal_count_hold\"]}  | 精度: {(d[\"accuracy_rate\"]*100):.1f}%')" 2>/dev/null || echo "   ⚠️  API不可达"
  else
    echo "🔴 未运行"
  fi
}

logs() {
  if [ -f "$LOG_FILE" ]; then
    tail -f "$LOG_FILE"
  else
    echo "⚠️  日志文件不存在"
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  logs) logs ;;
  *) echo "用法: $0 {start|stop|restart|status|logs}" ;;
esac
