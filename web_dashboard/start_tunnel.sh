#!/bin/bash
# Cloudflare Tunnel auto-start script for DSL Dashboard
LOG="/tmp/cloudflared-tunnel.log"
PID_FILE="/tmp/cloudflared-tunnel.pid"

start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Tunnel already running (PID: $(cat $PID_FILE))"
    return
  fi
  echo "🌐 Starting Cloudflare Tunnel..."
  nohup cloudflared tunnel --url http://localhost:8888 > "$LOG" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 3
  URL=$(grep -o 'https://[a-z0-9.-]*\.trycloudflare\.com' "$LOG" | tail -1)
  if [ -n "$URL" ]; then
    echo "$URL" > /tmp/cloudflared-url.txt
    echo "✅ Tunnel URL: $URL"
  else
    echo "⚠️ Tunnel started, waiting for URL... (check $LOG)"
  fi
}

stop() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill "$PID" 2>/dev/null && echo "⏹️ Tunnel stopped (PID: $PID)"
    rm -f "$PID_FILE"
  fi
}

status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "🟢 Running"
    cat /tmp/cloudflared-url.txt 2>/dev/null
  else
    echo "🔴 Not running"
  fi
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "Usage: $0 {start|stop|restart|status}" ;;
esac
