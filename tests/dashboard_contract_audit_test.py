import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

FIXED_DASHBOARD_NOW = datetime(2026, 6, 9, 18, 0, 0)


def _write_json(path: Path, data: dict, mtime: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _write_pool(root: Path, size: int = 42) -> list[str]:
    symbols = [f"{i:06d}" for i in range(1, size + 1)]
    pool_path = root / "config" / "master_stock_pool.yaml"
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    pool_path.write_text(
        yaml.safe_dump({"master_pool": [{"symbol": s, "name": s} for s in symbols]}),
        encoding="utf-8",
    )
    return symbols


def _prediction(symbol: str, source: str = "h5d_enhanced", confidence=0.66,
                predicted_return=0.012, direction_accuracy=0.58) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "signal": "HOLD",
        "source": source,
        "confidence": confidence,
        "predicted_return": predicted_return,
        "direction_accuracy": direction_accuracy,
    }


def _enhanced_report(symbols: list[str]) -> dict:
    return {
        symbol: {"h5d": {"predicted_return": 0.01, "direction_accuracy": 0.57}}
        for symbol in symbols
    }


def test_layer4_fails_large_pool_fallback_and_default_pollution(monkeypatch, tmp_path):
    import scripts.audit.layer4_data_quality as layer4

    symbols = _write_pool(tmp_path)
    rows = [
        _prediction(s, source="pool_fallback", confidence=0.3,
                    predicted_return=0, direction_accuracy=0.5)
        for s in symbols[:40]
    ] + [
        _prediction(s)
        for s in symbols[40:]
    ]
    _write_json(tmp_path / "cache" / "daily_predict.json", {"predictions": rows})
    _write_json(tmp_path / "cache" / "training_status.json", {})
    monkeypatch.setattr(layer4, "PROJECT_ROOT", str(tmp_path))

    monitor = layer4.DataQualityMonitor()
    monitor.check_prediction_semantics()

    text = "\n".join(monitor.issues + monitor.warnings)
    assert "h5d_enhanced覆盖率" in text
    assert "pool_fallback占比" in text
    assert "confidence=0.3默认值" in text
    assert "predicted_return=0默认值" in text
    assert "direction_accuracy=0.5待训练哨兵" in text


def test_layer4_accepts_recent_partial_when_recent_full_base_exists(monkeypatch, tmp_path):
    import scripts.audit.layer4_data_quality as layer4

    symbols = _write_pool(tmp_path)
    pred_dir = tmp_path / "reports" / "predictor"
    now = time.time()
    _write_json(
        pred_dir / "prediction_enhanced_20260604_163225.json",
        _enhanced_report(symbols[:24]),
        now - 4 * 24 * 3600,
    )
    _write_json(
        pred_dir / "prediction_enhanced_20260607_090749.json",
        _enhanced_report([symbols[0]]),
        now - 3600,
    )
    monkeypatch.setattr(layer4, "PROJECT_ROOT", str(tmp_path))

    monitor = layer4.DataQualityMonitor()
    monitor.check_enhanced_report_coverage()

    assert not monitor.issues
    assert any("小批量" in item and "全量基底" in item for item in monitor.passes)


def test_layer4_fails_when_recent_partial_has_no_recent_full_base(monkeypatch, tmp_path):
    import scripts.audit.layer4_data_quality as layer4

    symbols = _write_pool(tmp_path)
    pred_dir = tmp_path / "reports" / "predictor"
    now = time.time()
    _write_json(
        pred_dir / "prediction_enhanced_20260601_163225.json",
        _enhanced_report(symbols[:24]),
        now - 8 * 24 * 3600,
    )
    _write_json(
        pred_dir / "prediction_enhanced_20260607_090749.json",
        _enhanced_report([symbols[0]]),
        now - 3600,
    )
    monkeypatch.setattr(layer4, "PROJECT_ROOT", str(tmp_path))

    monitor = layer4.DataQualityMonitor()
    monitor.check_enhanced_report_coverage()

    assert any("7天内无全量enhanced报告" in item for item in monitor.issues)


def test_layer4_flags_trained_symbol_that_dashboard_would_show_pending(monkeypatch, tmp_path):
    import scripts.audit.layer4_data_quality as layer4

    symbols = _write_pool(tmp_path, size=10)
    rows = [_prediction(symbols[0], direction_accuracy=0.5)]
    rows.extend(_prediction(s) for s in symbols[1:])
    _write_json(tmp_path / "cache" / "daily_predict.json", {"predictions": rows})
    _write_json(
        tmp_path / "cache" / "training_status.json",
        {
            symbols[0]: {
                "status": "success",
                "model_fresh": True,
                "last_train_time": "2026-06-08T16:00:00",
            }
        },
    )
    monkeypatch.setattr(layer4, "PROJECT_ROOT", str(tmp_path))

    monitor = layer4.DataQualityMonitor()
    monitor.check_prediction_semantics()

    assert any(
        "Dashboard待训练/默认值污染已训练标的" in item
        for item in monitor.issues + monitor.warnings
    )


def _fake_full_dashboard() -> dict:
    predictions = [
        _prediction("000001"),
        _prediction("000002", confidence=0.7, predicted_return=-0.01, direction_accuracy=0.62),
    ]
    return {
        "status": {"health": "ok", "version": "test"},
        "pool": {"total": 2, "stocks": [{"symbol": "000001"}, {"symbol": "000002"}], "tiers": {}},
        "predictions": {"total": len(predictions), "predictions": predictions},
        "calibration": {
            "summary": {"total": 2, "retrain_urgent": 0, "retrain_planned": 0, "normal": 2},
            "stocks": [
                {"symbol": "000001", "accuracy": 0.58, "calibration_status": "normal"},
                {"symbol": "000002", "accuracy": 0.62, "calibration_status": "normal"},
            ],
        },
        "portfolio": {
            "total_positions": 1,
            "cash": 1000,
            "total_value": 2100,
            "positions": [{"symbol": "000001", "quantity": 100, "market_value": 1100}],
        },
        "pipeline": {
            "phases_order": ["phase1"],
            "timeline": {"phase1": {"tasks": [{"task_id": "batch_predict", "status": "completed"}]}},
            "stats": {"total": 1},
        },
        "blackswan": {
            "active": False,
            "severity": "low",
            "events": [],
            "market_prices": {},
            "top_threats": [],
            "position_ratio": 1.0,
            "lppl": {},
        },
        "attribution": {},
    }


def test_dashboard_static_contract_covers_every_tab_and_fetch():
    from scripts.audit.dashboard_contract_audit import DashboardContractAudit

    audit = DashboardContractAudit()
    audit.check_static_frontend_contract()

    assert not audit.issues
    assert set(audit.details["tabs"]) == {
        "overview", "predictions", "models", "progress", "blackswan", "paper-trader"
    }
    assert "/api/full" in audit.details["fetches"]
    assert "/api/cron/trigger/" in audit.details["mutation_fetches"]


def test_dashboard_contract_core_api_shapes_and_cross_module_consistency(monkeypatch):
    from scripts.audit.dashboard_contract_audit import DashboardContractAudit

    audit = DashboardContractAudit()
    full = _fake_full_dashboard()
    audit.check_full_dashboard_contract(full)
    audit.check_prediction_module(full)
    audit.check_model_module(full)
    audit.check_pipeline_module(full)
    monkeypatch.setattr(
        audit,
        "_read_paper_trader_snapshot",
        lambda: {
            "summary": {"total_value": 2100},
            "positions": [{"symbol": "000001", "quantity": 100, "market_value": 1100}],
        },
    )
    audit.check_paper_trader_consistency(full)

    assert not audit.issues
    assert any("预测模块total一致" in item for item in audit.passes)
    assert any("/api/full.portfolio" in item for item in audit.passes)


def _patch_dashboard_adapter_paths(monkeypatch, tmp_path):
    from web_dashboard import data_adapter

    project_root = tmp_path
    monkeypatch.setattr(data_adapter, "PROJECT_ROOT", str(project_root))
    monkeypatch.setattr(data_adapter, "DATA_DIR", str(project_root / "data"))
    monkeypatch.setattr(data_adapter, "CONFIG_DIR", str(project_root / "config"))
    monkeypatch.setattr(data_adapter, "CACHE_DIR", str(project_root / "cache"))
    monkeypatch.setattr(data_adapter, "CONFIDENCE_DIR", str(project_root / "confidence_data"))
    monkeypatch.setattr(data_adapter, "MODELS_DIR", str(project_root / "models"))
    monkeypatch.setattr(data_adapter, "LOGS_DIR", str(project_root / "logs"))

    original_expanduser = data_adapter.os.path.expanduser

    def fake_expanduser(path):
        if path.startswith("~/.openclaw/cron"):
            return str(project_root / "openclaw" / "cron" / Path(path).name)
        return original_expanduser(path)

    monkeypatch.setattr(data_adapter.os.path, "expanduser", fake_expanduser)
    
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return FIXED_DASHBOARD_NOW.replace(tzinfo=tz)
            return FIXED_DASHBOARD_NOW

    monkeypatch.setattr(data_adapter, "datetime", FixedDatetime)
    return data_adapter


def _write_cron_fixture(root: Path, job: dict, state: dict):
    cron_dir = root / "openclaw" / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    _write_json(cron_dir / "jobs.json", {"jobs": [job]})
    _write_json(cron_dir / "jobs-state.json", {"jobs": {job["id"]: {"state": state}}})


def _cron_expr_at(hour: int, minute: int) -> tuple[str, datetime]:
    scheduled = FIXED_DASHBOARD_NOW.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return f"{scheduled.minute} {scheduled.hour} * * *", scheduled


def test_pipeline_keeps_completed_progress_when_cron_wrapper_times_out(monkeypatch, tmp_path):
    adapter = _patch_dashboard_adapter_paths(monkeypatch, tmp_path)
    expr, scheduled = _cron_expr_at(11, 0)
    today = FIXED_DASHBOARD_NOW.strftime("%Y%m%d")
    updated_at = datetime.now().isoformat()
    _write_json(
        tmp_path / "cache" / "progress" / f"intraday_monitor_1100_{today}_110003.json",
        {
            "task_id": f"intraday_monitor_1100_{today}_110003",
            "task_name": "intraday_monitor_1100",
            "status": "completed",
            "message": "信号监控完成 [11:00]",
            "progress": {"step": 3, "total": 3},
            "started_at": scheduled.isoformat(),
            "updated_at": updated_at,
        },
    )
    job = {
        "id": "job-intraday-1100",
        "name": "DSL盘中信号监控 11:00",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": expr},
    }
    _write_cron_fixture(
        tmp_path,
        job,
        {
            "lastRunStatus": "error",
            "lastRunAtMs": int((scheduled + timedelta(minutes=1)).timestamp() * 1000),
            "lastDurationMs": 180_000,
            "lastError": "cron: job execution timed out (last phase: model-call-started)",
        },
    )

    tasks = adapter.get_pipeline_timeline()["timeline"]["intraday"]["tasks"]
    task = tasks[0]

    assert task["logical_id"] == "intraday_monitor_1100"
    assert task["status"] == "completed"
    assert task["wrapper_status"] == "timeout"
    assert "cron wrapper超时" in task["message"]
    assert task["completed_at"] == updated_at


def test_pipeline_keeps_timeout_when_no_progress_or_output_evidence(monkeypatch, tmp_path):
    adapter = _patch_dashboard_adapter_paths(monkeypatch, tmp_path)
    expr, scheduled = _cron_expr_at(9, 0)
    job = {
        "id": "job-pre-market-refresh",
        "name": "DSL盘前数据刷新(09:00)",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": expr},
    }
    _write_cron_fixture(
        tmp_path,
        job,
        {
            "lastRunStatus": "error",
            "lastRunAtMs": int((scheduled + timedelta(minutes=1)).timestamp() * 1000),
            "lastDurationMs": 302_000,
            "lastError": "cron: job execution timed out (last phase: model-call-started)",
        },
    )

    tasks = adapter.get_pipeline_timeline()["timeline"]["premarket"]["tasks"]
    task = tasks[0]

    assert task["logical_id"] == "pre_market_refresh"
    assert task["status"] == "timeout"
    assert task["wrapper_status"] == "timeout"
    assert "运行超时" in task["message"]


def test_pool_warmup_progress_maps_to_pipeline_task(monkeypatch, tmp_path):
    adapter = _patch_dashboard_adapter_paths(monkeypatch, tmp_path)
    expr, scheduled = _cron_expr_at(8, 50)
    today = FIXED_DASHBOARD_NOW.strftime("%Y%m%d")
    completed_at = datetime.now().isoformat()
    _write_json(
        tmp_path / "cache" / "progress" / f"pool_warmup_{today}_085000.json",
        {
            "task_id": f"pool_warmup_{today}_085000",
            "task_name": "pool_warmup",
            "status": "completed",
            "message": "连接池预热完成",
            "progress": {"step": 2, "total": 2},
            "started_at": scheduled.isoformat(),
            "completed_at": completed_at,
            "updated_at": completed_at,
        },
    )
    job = {
        "id": "job-pool-warmup",
        "name": "DSL模型连接池预热(08:50)",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": expr},
    }
    _write_cron_fixture(
        tmp_path,
        job,
        {
            "lastRunStatus": "ok",
            "lastRunAtMs": int((scheduled + timedelta(minutes=1)).timestamp() * 1000),
            "lastDurationMs": 5_000,
            "lastError": "",
        },
    )

    tasks = adapter.get_pipeline_timeline()["timeline"]["premarket"]["tasks"]
    task = tasks[0]

    assert task["logical_id"] == "pool_warmup"
    assert task["status"] == "completed"


def test_predictions_overlay_authoritative_master_pool_tier(monkeypatch, tmp_path):
    adapter = _patch_dashboard_adapter_paths(monkeypatch, tmp_path)
    pool_path = tmp_path / "config" / "master_stock_pool.yaml"
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    pool_path.write_text(
        yaml.safe_dump({
            "master_pool": [{
                "symbol": "000858",
                "name": "五粮液",
                "tier": "flex",
                "sector": "食品饮料",
                "concept": "白酒",
                "score": 50,
            }]
        }, allow_unicode=True),
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "cache" / "daily_predict.json",
        {
            "timestamp": "2026-06-12T14:30:00",
            "predictions": [{
                "symbol": "000858",
                "name": "五粮液",
                "tier": "growth",
                "layer": "成长池",
                "signal": "buy",
                "confidence": 0.82,
            }],
        },
    )

    result = adapter.get_predictions()
    row = result["predictions"][0]

    assert row["tier"] == "flex"
    assert row["layer"] == "观察池"
    assert row["sector"] == "食品饮料"
    assert row["cached_tier"] == "growth"



def test_progress_tracker_extra_fields_do_not_override_reserved_status(monkeypatch, tmp_path):
    from common import progress_tracker

    monkeypatch.setattr(progress_tracker, "PROGRESS_DIR", tmp_path)
    tracker = progress_tracker.ProgressTracker("reserved_collision", total_steps=1)
    tracker.complete("done", status=200)

    files = list(tmp_path.glob("reserved_collision_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))

    assert data["status"] == "completed"
    assert data["extra_status"] == 200
    assert data["completed_at"]


def test_rank_ic_contract_three_layers_and_static_no_false_failure():
    from web_dashboard.data_adapter import get_rank_ic

    ic = get_rank_ic()
    assert "current_summary" in ic
    assert "historical_summary" in ic
    assert "risk_gate" in ic

    appjs_path = Path(__file__).resolve().parents[1] / "web_dashboard" / "static" / "app.js"
    appjs = appjs_path.read_text(encoding="utf-8")
    assert "模型预测力当前失效" not in appjs
    # stale 与 insufficient_data 必须有独立状态映射
    assert "insufficient_data" in appjs
    assert "stale" in appjs
