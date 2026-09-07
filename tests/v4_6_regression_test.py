#!/usr/bin/env python3
"""v4.6.x safe regression tests.

These tests are intentionally side-effect bounded: pytest collection performs
no service restarts, no real Dashboard mutation, and no writes to the production
paper_trading.db.
"""
import ast
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_h5d_signal_weight_is_continuous():
    from scripts.batch_predict import compute_h5d_signal

    r = compute_h5d_signal(0.008, 0.48)
    assert r["signal"] == "buy"
    assert r["signal_weight"] > 0.3
    assert abs(r["confidence"] - 0.48) < 0.001

    low = compute_h5d_signal(0.008, 0.449)
    high = compute_h5d_signal(0.008, 0.451)
    assert abs(high["signal_weight"] - low["signal_weight"]) < 0.02

    assert compute_h5d_signal(0.025, 0.38)["signal"] == "buy"
    assert compute_h5d_signal(0.0003, 0.30)["signal"] == "hold"


def test_risk_manager_stop_loss_and_trailing_thresholds(monkeypatch):
    from core.risk_manager import RiskManager
    from scripts.paper_trader import get_stop_loss_pct

    rm = RiskManager()
    assert get_stop_loss_pct("000001") < 0
    assert abs(rm._get_trailing_drawdown_threshold(0.15) - 0.05) < 0.01
    assert abs(rm._get_trailing_drawdown_threshold(1.20) - 0.15) < 0.01

    result = rm.check_single_position_take_profit(
        "000001", "测试紧止损", avg_price=10.0, current_price=9.5, peak_price=10.05
    )
    assert result["triggered"] is True

    monkeypatch.setattr(rm, "_check_rs_strong", lambda s: True)
    monkeypatch.setattr(rm, "_check_trend_intact", lambda s, p: True)
    hold = rm.check_single_position_take_profit(
        "000001", "牛股", avg_price=20, current_price=52, peak_price=55
    )
    assert hold["triggered"] is False


def test_morning_decision_static_contracts():
    src = (PROJECT_ROOT / "scripts" / "morning_decision.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    plan_trades = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "plan_trades"
    )
    assert all(a.arg != "stock_accuracy_map" for a in plan_trades.args.args)
    assert any(isinstance(n, ast.FunctionDef) and n.name == "_check_signal_weight" for n in ast.walk(tree))
    assert "ENABLE_DUAL_AGENT_VERIFY" not in src
    assert "def _load_stock_accuracy_map" not in src


def test_broker_market_data_and_config_imports():
    from execution_engine.brokers.factory import get_broker
    from execution_engine.brokers.xtquant_broker import OrderStatus, XtQuantBroker
    from execution_engine.market_data import get_market_client

    assert XtQuantBroker is not None
    assert get_broker is not None
    assert len(OrderStatus) == 8
    assert get_market_client().is_connected()

    with open(PROJECT_ROOT / "config" / "feature_flags.yaml", encoding="utf-8") as f:
        flags = yaml.safe_load(f)
    assert "live_trading" in flags
    assert "execute_live_trades" not in str(flags.get("claude_code", {}).get("restricted", []))

    with open(PROJECT_ROOT / "config" / "adaptive_params.yaml", encoding="utf-8") as f:
        adaptive = yaml.safe_load(f)
    assert "trading" in adaptive
    assert "slippage" in adaptive.get("trading", {})


def test_reconciliation_engine_uses_isolated_sqlite(monkeypatch, tmp_path):
    import execution_engine.reconciliation as reconciliation

    db_path = tmp_path / "paper_trading.db"
    monkeypatch.setattr(reconciliation, "DB_PATH", str(db_path))

    recon = reconciliation.ReconciliationEngine()
    oid = recon.record_signal("000001", "BUY", 100, 10.0, 0.7, "test")
    assert len(oid) > 10

    recon.mark_executed(oid, 999, 10.02, 100)
    order = recon.get_order(oid)
    assert order is not None
    assert order.status == "EXECUTED"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM reconciliation WHERE order_id=?", (oid,)).fetchone()
    assert row[0] == "EXECUTED"


def test_dashboard_adapter_contract_is_readable():
    from web_dashboard.data_adapter import get_full_dashboard

    full = get_full_dashboard()
    assert isinstance(full, dict)
    assert "status" in full
    assert "pipeline" in full
    assert "predictions" in full


def test_scheduled_executor_blocks_stale_buy_preserves_sell(monkeypatch):
    from core import data_freshness
    from scripts import execute_scheduled_trades as scheduled

    monkeypatch.setattr(
        data_freshness,
        "assess_daily_predict_freshness",
        lambda: {
            "status": "missing",
            "allow_buy": False,
            "allow_sell": True,
            "reason": "daily_predict缺失",
        },
    )
    trades = [
        {"code": "000001", "action": "BUY", "quantity": 100, "price": 10},
        {"code": "000002", "action": "SELL", "quantity": 100, "price": 11},
    ]

    filtered, freshness = scheduled._filter_buys_when_prediction_stale(trades)
    assert freshness["allow_buy"] is False
    assert filtered == [trades[1]]


def test_scheduled_executor_initializes_qty_and_ref_price_before_risk_gates():
    src = (PROJECT_ROOT / "scripts" / "execute_scheduled_trades.py").read_text(encoding="utf-8")

    qty_init = src.index('qty = int(trade.get("quantity", 100) or 100)')
    quality_gate = src.index("精度<40%禁入")
    ref_price_init = src.index("ref_price = _trade_reference_price(trade)")
    single_limit_gate = src.index("单票仓位硬上限")

    assert qty_init < quality_gate
    assert ref_price_init < single_limit_gate


def test_cron_config_static_entries_present():
    cron_path = Path(os.path.expanduser("~/.openclaw/cron/jobs.json"))
    if not cron_path.exists():
        pytest.skip("local OpenClaw cron jobs.json not present")

    cron_jobs = json.loads(cron_path.read_text(encoding="utf-8")).get("jobs", [])
    cron_names = {j.get("name") for j in cron_jobs}
    assert any("预测" in name and "09:00" in name for name in cron_names)
    assert "个股分批预测(17:00)" in cron_names
    assert "A股盘前决策(09:20)" in cron_names
    assert "A股盘前交易预案(evening)" in cron_names


def test_legacy_conservative_gate_keeps_one_new_position():
    from scripts.rank_ic_monitor import resolve_rank_ic_new_position_cap

    gate = {"policy": "legacy_conservative", "max_new_positions": 1}
    assert resolve_rank_ic_new_position_cap(gate, available=5) == 1


def test_morning_decision_consumes_explicit_risk_gate():
    src = (PROJECT_ROOT / "scripts" / "morning_decision.py").read_text(encoding="utf-8")
    # 盘前决策必须读显式 risk_gate (而非把展示状态当交易限制)
    assert "resolve_rank_ic_new_position_cap" in src
    assert "risk_gate" in src
