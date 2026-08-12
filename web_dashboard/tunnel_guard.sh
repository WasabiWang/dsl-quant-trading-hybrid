#!/bin/bash
# DSL Dashboard — Cloudflare Tunnel 守护脚本
# 自动检测/恢复tunnel, 记录URL变更

TUNNEL_LOG="/tmp/cloudflared-tunnel.log"
TUNNEL_PID="/tmp/cloudflared-tunnel.pid"
TUNNEL_URL="/tmp/cloudflared-url.txt"
DASHBOARD_PORT=8888

get_url() {
    grep -o 'https://[a-z0-9.-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | tail -1
}

tunnel_alive() {
    [ -f "$TUNNEL_PID" ] && kill -0 "$(cat "$TUNNEL_PID")" 2>/dev/null
}

dashboard_alive() {
    curl -s -o /dev/null -w "%{http_code}" "http://localhost:$DASHBOARD_PORT" 2>/dev/null | grep -q 200
}

# ── 启动 ──
start_tunnel() {
    OLD_URL=$(get_url)
    
    if tunnel_alive; then
        NEW_URL=$(get_url)
        if [ -n "$NEW_URL" ] && curl -s -o /dev/null -w "%{http_code}" "$NEW_URL" --max-time 5 2>/dev/null | grep -q 200; then
            echo "🟢 Tunnel已在运行: $(cat $TUNNEL_URL 2>/dev/null)"
            return 0
        fi
        echo "⚠️ Tunnel已死, 重启中..."
        kill "$(cat "$TUNNEL_PID")" 2>/dev/null
        sleep 1
    fi
    
    if ! dashboard_alive; then
        echo "❌ Dashboard未运行, 先启动Dashboard"
        return 1
    fi
    
    echo "🌐 启动Cloudflare Tunnel..."
    nohup cloudflared tunnel --url "http://localhost:$DASHBOARD_PORT" \
        > "$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID"
    
    # 等待URL生成 (最多30秒)
    for i in $(seq 1 15); do
        sleep 2
        NEW_URL=$(get_url)
        if [ -n "$NEW_URL" ]; then
            echo "$NEW_URL" > "$TUNNEL_URL"
            if [ -n "$OLD_URL" ] && [ "$NEW_URL" != "$OLD_URL" ]; then
                echo "🔔 URL已变更!"
                echo "   旧: $OLD_URL"
                echo "   新: $NEW_URL"
            fi
            echo "✅ Tunnel就绪: $NEW_URL"
            return 0
        fi
    done
    
    echo "⚠️ URL未生成 (30s超时), 检查: tail -f $TUNNEL_LOG"
    return 1
}

# ── 状态 ──
status() {
    echo "Dashboard: $(dashboard_alive && echo '🟢 运行中' || echo '🔴 未运行')"
    echo -n "Tunnel: "
    if tunnel_alive; then
        URL=$(cat "$TUNNEL_URL" 2>/dev/null)
        if curl -s -o /dev/null "$URL" --max-time 5 2>/dev/null; then
            echo "🟢 运行中"
            echo "  URL: $URL"
        else
            echo "🟡 进程存在但URL不可达"
        fi
    else
        echo "🔴 未运行"
    fi
}

# ── 守护(定期检查) ──
watchdog() {
    while true; do
        if ! tunnel_alive || ! dashboard_alive; then
            echo "[$(date +%H:%M)] Tunnel异常, 重启..."
            start_tunnel
        fi
        sleep 300  # 每5分钟检查
    done
}

case "${1:-start}" in
    start) start_tunnel ;;
    stop) 
        [ -f "$TUNNEL_PID" ] && kill "$(cat "$TUNNEL_PID")" 2>/dev/null
        rm -f "$TUNNEL_PID"
        echo "⏹️ Tunnel已停止"
        ;;
    restart) 
        [ -f "$TUNNEL_PID" ] && kill "$(cat "$TUNNEL_PID")" 2>/dev/null
        rm -f "$TUNNEL_PID"; sleep 1
        start_tunnel
        ;;
    status) status ;;
    watchdog) watchdog ;;
    url) cat "$TUNNEL_URL" 2>/dev/null || echo "暂无URL" ;;
    *)
        echo "用法: $0 {start|stop|restart|status|watchdog|url}"
        ;;
esac
