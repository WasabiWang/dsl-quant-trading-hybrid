#!/usr/bin/env python3
"""
DSL v4.5.3d Dashboard — Web UI实时仪表板
启动: streamlit run scripts/dashboard.py --server.port 8502
依赖: streamlit, plotly, jinja2
"""
import os, sys, json, yaml, sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

st.set_page_config(
    page_title="DSL Quant Dashboard v4.5.3d",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── 样式 ──
st.markdown("""
<style>
  .metric-positive {color:#2ecc71;font-weight:bold}
  .metric-negative {color:#e74c3c;font-weight:bold}
  .signal-buy {background:#eafaf1;border-left:4px solid #2ecc71;padding:8px}
  .signal-sell {background:#fdedec;border-left:4px solid #e74c3c;padding:8px}
  .risk-high {color:#e74c3c;font-weight:bold}
  .progress-bar {height:6px;border-radius:3px;background:#3498db}
</style>
""", unsafe_allow_html=True)

# ── 数据加载函数 ──
@st.cache_data(ttl=60)
def load_predictions():
    pred_file = PROJECT_ROOT / "cache" / "daily_predict.json"
    if not pred_file.exists():
        return None, None
    with open(pred_file) as f:
        data = json.load(f)
    predictions = data.get("predictions", [])
    meta = {k: v for k, v in data.items() if k != "predictions"}
    return predictions, meta

@st.cache_data(ttl=120)
def load_portfolio():
    db_path = PROJECT_ROOT / "data" / "paper_trading.db"
    if not db_path.exists():
        return None, None, None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    ledger = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM ledger").fetchall()}
    positions = [dict(r) for r in conn.execute("SELECT * FROM positions").fetchall()]
    trades = [dict(r) for r in conn.execute("SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT 20").fetchall()]
    conn.close()
    
    cash = float(ledger.get("current_cash", 0))
    pos_value = sum(p.get("quantity", 0) * p.get("current_price", 0) for p in positions)
    total_value = cash + pos_value
    initial = float(ledger.get("initial_capital", 1000000))
    pnl = total_value - initial
    pnl_pct = (pnl / initial * 100) if initial > 0 else 0
    
    portfolio = {
        "cash": cash, "positions_value": pos_value, "total_value": total_value,
        "initial_capital": initial, "pnl": pnl, "pnl_pct": pnl_pct,
        "position_count": len(positions)
    }
    return portfolio, positions, trades

@st.cache_data(ttl=30)
def load_circuit_breaker():
    cb_file = PROJECT_ROOT / "data" / "circuit_breaker.json"
    if not cb_file.exists():
        return {"trading_paused": False}
    with open(cb_file) as f:
        return json.load(f)

@st.cache_data(ttl=60)
def load_calibration():
    cal_file = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"
    if not cal_file.exists():
        return None
    with open(cal_file) as f:
        return json.load(f)

@st.cache_data(ttl=10)
def load_progress():
    try:
        from common.progress_tracker import get_all_progress
        return get_all_progress()
    except ImportError:
        return []

@st.cache_data(ttl=300)
def load_pool():
    pool_file = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
    with open(pool_file) as f:
        mp = yaml.safe_load(f)["master_pool"]
    return [{"symbol": str(s["symbol"]).zfill(6), "name": s["name"], "tier": s["tier"], "score": s["score"]} for s in mp]

# ── 主界面 ──
st.title("📊 DSL量化交易系统 v4.5.3d")
st.caption(f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} | 数据自动刷新")

# ── Sidebar ──
with st.sidebar:
    st.header("⚙️ 系统状态")
    
    # 熔断器
    cb = load_circuit_breaker()
    if cb.get("trading_paused"):
        st.error("🔴 交易已暂停")
    else:
        st.success("🟢 交易正常")
    
    st.metric("今日交易", cb.get("today_trade_count", 0))
    st.caption(f"日内回撤: {cb.get('today_drawdown', 0):.1%}")
    
    # 进度
    st.header("📋 Cron进度")
    progress = load_progress()
    if progress:
        for p in progress[:5]:
            icon = {"running": "🔄", "completed": "✅", "failed": "❌"}.get(p["status"], "⏳")
            elapsed = p.get("elapsed_seconds", 0)
            st.text(f"{icon} {p['task_name']} ({elapsed:.0f}s)")
            st.progress(p["progress"]["step"] / max(p["progress"]["total"], 1))
    else:
        st.info("暂无运行中的任务")
    
    st.divider()
    st.caption(f"Tag: v4.5.3d-backup-20260505-0032")

# ── Tab布局 ──
tab1, tab2, tab3, tab4 = st.tabs(["📈 信号面板", "💰 持仓损益", "📊 校准趋势", "🦢 风险监控"])

# ── Tab 1: 信号面板 ──
with tab1:
    predictions, meta = load_predictions()
    
    col1, col2, col3, col4 = st.columns(4)
    if meta:
        col1.metric("总标的", meta.get("total_stocks", "?"))
        col2.metric("高信度", meta.get("high_confidence", "?"))
        col3.metric("交叉确认", meta.get("cross_confirmed", "?"))
        col4.metric("H5D命中", meta.get("high_confidence_h5d", "?"))
    
    if predictions:
        # Buy/Sell/Hold分布
        signals = Counter(p.get("signal", "hold") for p in predictions)
        fig_sig = go.Figure(go.Pie(
            labels=["买", "卖", "持"],
            values=[signals.get("buy", 0), signals.get("sell", 0), signals.get("hold", 0)],
            marker_colors=["#2ecc71", "#e74c3c", "#95a5a6"],
            hole=0.4
        ))
        fig_sig.update_layout(height=250, margin=dict(l=0,r=0,t=0,b=0))
        col_pie, col_list = st.columns([1, 2])
        with col_pie:
            st.plotly_chart(fig_sig, use_container_width=True)
        
        with col_list:
            # 高信度信号展示
            high_conf = [p for p in predictions if p.get("signal") != "hold" and p.get("confidence", 0) >= 0.55]
            if high_conf:
                st.subheader(f"🎯 高信度信号 ({len(high_conf)})")
                df_signals = pd.DataFrame([{
                    "标的": f"{p.get('symbol','')} {p.get('name','')}",
                    "信号": "📈 买" if p.get("signal") == "buy" else "📉 卖",
                    "置信度": f"{p.get('confidence',0):.1%}",
                    "方向精度": f"{p.get('direction_accuracy',0):.1%}"
                } for p in high_conf[:10]])
                st.dataframe(df_signals, use_container_width=True, hide_index=True)
            else:
                st.info("无高信度信号")
    else:
        st.warning("daily_predict.json 不存在 (可能非交易日)")

# ── Tab 2: 持仓损益 ──
with tab2:
    portfolio, positions, trades = load_portfolio()
    
    if portfolio:
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("总资产", f"¥{portfolio['total_value']:,.0f}")
        col2.metric("现金", f"¥{portfolio['cash']:,.0f}")
        col3.metric("持仓市值", f"¥{portfolio['positions_value']:,.0f}")
        pnl_color = "metric-positive" if portfolio['pnl'] >= 0 else "metric-negative"
        col4.metric("累计盈亏", f"¥{portfolio['pnl']:,.0f}")
        col5.metric("收益率", f"{portfolio['pnl_pct']:.2f}%")
        
        if positions:
            st.subheader(f"📦 持仓明细 ({len(positions)})")
            df_pos = pd.DataFrame([{
                "代码": p["stock_code"],
                "数量": p["quantity"],
                "均价": f"¥{p['avg_cost']:.2f}",
                "现价": f"¥{p.get('current_price',0):.2f}",
                "市值": f"¥{p['quantity']*p.get('current_price',0):,.0f}",
                "盈亏": f"¥{(p.get('current_price',0)-p['avg_cost'])*p['quantity']:,.0f}"
            } for p in positions])
            st.dataframe(df_pos, use_container_width=True, hide_index=True)
        
        if trades:
            st.subheader(f"📜 最近交易 ({len(trades)})")
            df_trades = pd.DataFrame([{
                "时间": t.get("timestamp","")[:16],
                "代码": t.get("stock_code",""),
                "操作": t.get("action",""),
                "价格": f"¥{t.get('price',0):.2f}",
                "数量": t.get("quantity",0),
                "金额": f"¥{t.get('amount',0):,.0f}",
                "原因": t.get("reason","")
            } for t in trades[:10]])
            st.dataframe(df_trades, use_container_width=True, hide_index=True)
    else:
        st.info("paper_trading.db 不存在或无数据")

# ── Tab 3: 校准趋势 ──
with tab3:
    cal = load_calibration()
    if cal:
        col1, col2, col3 = st.columns(3)
        stats = cal.get("overall_stats", {})
        col1.metric("总体精度", f"{stats.get('accuracy_rate',0):.1%}")
        col2.metric("平均置信度", f"{stats.get('avg_confidence',0):.1%}")
        col3.metric("校准误差", f"{stats.get('calibration_error',0):.3f}")
        
        # Stock accuracy chart
        stock_acc = cal.get("stock_accuracy", {})
        if stock_acc:
            acc_data = []
            for sym, sa in stock_acc.items():
                name = sa.get("name", sym)
                last_acc = sa.get("last_accuracy", sa.get("mean_accuracy", 0))
                if last_acc > 0:
                    acc_data.append({"股票": f"{sym} {name}", "精度": last_acc})
            
            if acc_data:
                df_acc = pd.DataFrame(acc_data).sort_values("精度", ascending=True)
                fig = go.Figure(go.Bar(
                    x=df_acc["精度"], y=df_acc["股票"],
                    orientation='h',
                    marker_color=["#e74c3c" if a < 0.45 else "#f39c12" if a < 0.50 else "#2ecc71" for a in df_acc["精度"]]
                ))
                fig.update_layout(height=max(400, len(df_acc)*20), margin=dict(l=0,r=0,t=0,b=0),
                                 xaxis_title="方向精度", xaxis_tickformat=".0%")
                st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("无校准数据")

# ── Tab 4: 风险监控 ──
with tab4:
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🦢 黑天鹅状态")
        try:
            bs_dir = Path.home() / ".openclaw" / "workspace" / "memory" / "black-swan"
            bs_files = sorted(bs_dir.glob("analysis-*.json"))
            if bs_files:
                with open(bs_files[-1]) as f:
                    bs = json.load(f)
                
                bs_date = bs.get("analysis_date", bs.get("date", "?"))[:10]
                events = bs.get("events_severity_ge_3", bs.get("events", []))
                risk = bs.get("overall_risk_level", bs.get("risk_matrix", {}).get("overall_assessment", "?"))
                
                st.metric("分析日期", bs_date)
                st.metric("风险等级", risk)
                st.metric("高风险事件", len(events))
                
                if events:
                    for e in events[:5]:
                        sev = "🔴"*min(e.get("severity",0), 3) + "⚪"*max(3-e.get("severity",0),0)
                        st.text(f"{sev} {e.get('title','')[:50]}")
            else:
                st.info("无黑天鹅分析数据")
        except Exception as e:
            st.warning(f"无法加载: {e}")
    
    with col2:
        st.subheader("📊 股票池分布")
        pool = load_pool()
        if pool:
            tiers = Counter(p["tier"] for p in pool)
            fig = go.Figure(go.Pie(
                labels=[f"{t} ({c})" for t, c in tiers.items()],
                values=list(tiers.values()),
                hole=0.4,
                marker_colors=["#2ecc71", "#3498db", "#9b59b6", "#e67e22", "#e74c3c"]
            ))
            fig.update_layout(height=300, margin=dict(l=0,r=0,t=0,b=0))
            st.plotly_chart(fig, use_container_width=True)
            
            st.caption(f"总标的: {len(pool)} | Tiers: {dict(tiers)}")

# ── Footer ──
st.divider()
st.caption(f"DSL v4.5.3d | {datetime.now().strftime('%Y-%m-%d %H:%M')} | 数据每60秒自动刷新 | backup tag: v4.5.3d-backup-20250505-0032")
