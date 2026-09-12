"""DSL Test Pyramid — L0: Unit tests (function-level, <1s)."""

import json
import sys
import tempfile
from pathlib import Path
from datetime import date, datetime, timedelta

import pytest
import pandas as pd
import yaml

from core.dsl_engine import DSLExecutor
from config.holiday_calendar import is_trading_day, get_next_trading_day
from common.file_lock import locked_json_write, locked_json_read, locked_rw
from web_dashboard.data_adapter import _fallback_position_ratio
from core.data_freshness import assess_daily_predict_freshness
from core.dsl_engine import AShareFeatureExtender
from core.retrain_queue_manager import RetrainQueueManager
from scripts.audit.run_all_audits import classify_layer_result


class TestMairuiDataAdapter:
    """L0.x: Mairui K-line adapter must return real OHLCV, not indicator-only data."""

    def test_trade_datetime_rejects_numeric_indicator_values(self):
        from predictor.mairui_data import _coerce_trade_datetime

        parsed = _coerce_trade_datetime(pd.Series(["2026-06-05 00:00:00", "1063", "1204"]))
        assert parsed.iloc[0] == pd.Timestamp("2026-06-05 00:00:00")
        assert pd.isna(parsed.iloc[1])
        assert pd.isna(parsed.iloc[2])

    def test_history_kline_uses_ohlcv_and_filters_date_window(self, monkeypatch):
        import predictor.mairui_data as mairui

        def fake_kline(*args, **kwargs):
            return [
                {"t": "2026-05-30 00:00:00", "o": 9, "h": 10, "l": 8, "c": 9.5, "v": 100, "a": 950},
                {"t": "2026-06-03 00:00:00", "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 200, "a": 2100},
                {"t": "2026-06-05 00:00:00", "o": 11, "h": 12, "l": 10, "c": 11.5, "v": 300, "a": 3450},
            ]

        monkeypatch.setattr(mairui, "MAIRUI_AVAILABLE", True)
        monkeypatch.setattr(mairui, "LICENCE", "test-licence")
        monkeypatch.setattr(mairui, "_get_kline_history", fake_kline)

        df = mairui.get_history_kline("600519.SH", start_date="2026-06-01", end_date="2026-06-05")
        assert list(df.index) == [pd.Timestamp("2026-06-03"), pd.Timestamp("2026-06-05")]
        assert {"open", "high", "low", "close", "volume", "amount"}.issubset(df.columns)
        assert df.loc[pd.Timestamp("2026-06-05"), "close"] == 11.5


class TestMachineReadableAuditOutput:
    """L0.x: helper logs must not pollute JSON stdout."""

    def test_akshare_retry_logs_to_stderr(self, capsys, monkeypatch):
        import common.akshare_utils as ak_utils

        monkeypatch.setattr(ak_utils, "MAX_RETRIES", 1)

        @ak_utils.with_retry
        def always_fails():
            raise RuntimeError("network down")

        assert always_fails() is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "always_fails" in captured.err


class TestHolidayCalendar:
    """L0.1: Holiday calendar — boundary and weekend tests."""

    def test_new_year_holiday(self):
        assert not is_trading_day(date(2026, 1, 1), "A_SHARE")

    def test_jan2_new_year_holiday(self):
        assert not is_trading_day(date(2026, 1, 2), "A_SHARE")

    def test_spring_festival(self):
        assert not is_trading_day(date(2026, 2, 17), "A_SHARE")

    def test_may_day(self):
        assert not is_trading_day(date(2026, 5, 1), "A_SHARE")

    def test_may6_post_holiday(self):
        assert is_trading_day(date(2026, 5, 6), "A_SHARE")

    def test_national_day(self):
        assert not is_trading_day(date(2026, 10, 1), "A_SHARE")

    def test_oct8_post_holiday(self):
        assert is_trading_day(date(2026, 10, 8), "A_SHARE")

    def test_saturday_not_trading(self):
        assert not is_trading_day(date(2026, 5, 9), "A_SHARE")

    def test_sunday_not_trading(self):
        assert not is_trading_day(date(2026, 5, 10), "A_SHARE")

    def test_next_trading_from_holiday(self):
        assert get_next_trading_day("A_SHARE", date(2026, 5, 1)) == date(2026, 5, 6)

    def test_next_trading_from_friday(self):
        assert get_next_trading_day("A_SHARE", date(2026, 5, 8)) == date(2026, 5, 11)


class TestFileLock:
    """L0.2: File lock — JSON read/write and concurrency safety."""

    def test_json_write_read(self, temp_dir):
        tmp = str(temp_dir / "test.json")
        assert locked_json_write(tmp, {"a": 1, "b": [2, 3]})
        assert locked_json_read(tmp) == {"a": 1, "b": [2, 3]}

    def test_nested_json(self, temp_dir):
        tmp = str(temp_dir / "nested.json")
        locked_json_write(tmp, {"nested": {"deep": True, "cnt": 42}})
        data = locked_json_read(tmp)
        assert data["nested"]["cnt"] == 42

    def test_locked_rw_context(self, temp_dir):
        tmp = str(temp_dir / "rw.json")
        with locked_rw(tmp) as (lock, data):
            data["modified"] = "yes"
            data["counter"] = data.get("counter", 0) + 1
        final = locked_json_read(tmp)
        assert final["modified"] == "yes"
        assert final["counter"] == 1

    def test_read_missing_file(self, temp_dir):
        tmp = str(temp_dir / "nonexistent.json")
        assert locked_json_read(tmp) == {}

    def test_concurrent_safe(self, temp_dir):
        tmp = str(temp_dir / "concurrent.json")
        Path(tmp).touch()
        with locked_rw(tmp) as (lock, data):
            data["concurrent"] = "safe"
        assert locked_json_read(tmp)["concurrent"] == "safe"


class TestDSLConditionExpressions:
    """L0.x: DSL condition semantics."""

    def test_infix_comparison_matches_function_primitive(self):
        executor = DSLExecutor()
        indicators = {
            "ma5": pd.Series([1.0, 2.0, 3.0]),
            "ma20": pd.Series([2.0, 2.0, 2.0]),
        }
        assert executor._evaluate_condition(indicators, "ma5 > ma20", 2)
        assert executor._evaluate_condition(indicators, "greater_than(ma5, ma20)", 2)
        assert not executor._evaluate_condition(indicators, "ma5 > ma20", 0)

    def test_infix_comparison_supports_numeric_threshold(self):
        executor = DSLExecutor()
        indicators = {"rsi14": pd.Series([35.0, 42.0, 29.0])}
        assert executor._evaluate_condition(indicators, "rsi14 <= 30", 2)
        assert not executor._evaluate_condition(indicators, "rsi14 <= 30", 1)

    def test_condition_expression_rejects_calls(self):
        executor = DSLExecutor()
        indicators = {"ma5": pd.Series([1.0])}
        assert not executor._evaluate_condition(indicators, "__import__('os').system('true')", 0)

    def test_condition_expression_supports_boolean_and(self):
        executor = DSLExecutor()
        indicators = {
            "ma5": pd.Series([3.0]),
            "ma20": pd.Series([2.0]),
            "rsi14": pd.Series([28.0]),
        }
        assert executor._evaluate_condition(indicators, "ma5 > ma20 and rsi14 < 30", 0)

    def test_run_strategy_produces_golden_trade_record(self):
        strategy = {
            "name": "golden_ma_reversal",
            "symbol": "000001",
            "timeframe": "daily",
            "indicators": [
                {"name": "ma3", "type": "ma", "params": {"period": 3}},
                {"name": "ma8", "type": "ma", "params": {"period": 8}},
            ],
            "signals": [
                {"condition": "ma3 > ma8", "action": "buy"},
                {"condition": "ma3 < ma8", "action": "sell"},
            ],
        }
        prices = ([10.0] * 20) + [10.5, 11.0, 11.5, 12.0, 12.5, 13.0] + [12.0, 11.0, 10.0, 9.5, 9.0, 8.5]
        data = pd.DataFrame({
            "close": prices,
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "volume": [1000000] * len(prices),
        }, index=pd.date_range("2026-01-01", periods=len(prices)))
        result = DSLExecutor().run_strategy(strategy, data)
        assert result["total_trades"] >= 1
        assert result["completed_trades"] >= 1
        trades = result["results"]["trades"]
        assert len(trades) >= 2
        assert trades[0]["action"] == "buy"
        assert trades[1]["action"] == "sell"
        assert {"date", "action", "price", "quantity"}.issubset(trades[0])


class TestAuditRunAllClassification:
    """L0.x: audit runner must not report false green."""

    def test_layer1_tiny_zero_accuracy_fails(self):
        status, reason = classify_layer_result("layer1", 0, {
            "direction_accuracy": {"total_predictions": 1, "direction_accuracy": 0.0}
        })
        assert status == "failed"
        assert "样本" in reason

    def test_layer1_explicit_insufficient_evidence_is_warning(self):
        status, reason = classify_layer_result("layer1", 2, {
            "status": "warning",
            "status_reason": "当前版本验证样本不足: 0 < 5",
            "validated_count": 0,
        })
        assert status == "warning"
        assert "不足" in reason

    def test_layer4_warning_count_is_warning(self):
        status, reason = classify_layer_result("layer4", 0, {
            "issue_count": 0,
            "warning_count": 2,
        })
        assert status == "warning"
        assert "警告" in reason


class TestLayer4DataQualityClassification:
    """L0.x: Layer4 should classify controlled stale inputs by executable risk."""

    def test_wrong_target_planned_trades_is_controlled_by_fail_closed(self, temp_dir, monkeypatch):
        import scripts.audit.layer4_data_quality as layer4

        cache_dir = temp_dir / "cache"
        cache_dir.mkdir()
        (cache_dir / "daily_predict.json").write_text(json.dumps({
            "predict_date": "2026-06-08",
            "predict_time": "2026-06-07T17:10:00",
            "predictions": [],
        }), encoding="utf-8")
        (cache_dir / "planned_trades.json").write_text(json.dumps({
            "target_date": "2026-06-05",
            "generated_at": "2026-06-05T09:20:00",
            "trades": [{"code": "000001", "action": "SELL"}],
        }), encoding="utf-8")
        monkeypatch.setattr(layer4, "PROJECT_ROOT", str(temp_dir))
        monkeypatch.setattr(
            layer4.DataQualityMonitor,
            "_expected_plan_target_date",
            staticmethod(lambda now=None: "2026-06-08"),
        )

        monitor = layer4.DataQualityMonitor()
        monitor.check_cache_freshness()
        assert any("执行层已fail-closed" in item for item in monitor.passes)
        assert not any("planned_trades.json" in item for item in monitor.warnings + monitor.issues)


class TestLayer1BacktestMaturity:
    """L0.x: Layer1 must not validate prediction horizons that have not matured."""

    def test_unmatured_current_prediction_is_warning_not_future_fetch(self, temp_dir, monkeypatch):
        import scripts.audit.layer1_backtest_accuracy as layer1

        project = Path(temp_dir)
        (project / "cache").mkdir()
        (project / "confidence_data").mkdir()
        (project / "VERSION").write_text("v4.6.2\n", encoding="utf-8")
        (project / "cache" / "daily_predict.json").write_text(json.dumps({
            "predict_date": "2026-06-08",
            "version": "v4.6.2",
            "predictions": [{
                "symbol": "600519.SH",
                "signal": "buy",
                "predicted_return": 0.01,
                "confidence": 0.65,
                "horizon": "5d",
            }],
        }), encoding="utf-8")
        (project / "confidence_data" / "prediction_calibration.json").write_text(json.dumps({
            "daily_records": []
        }), encoding="utf-8")

        def fail_if_called(*args, **kwargs):
            raise AssertionError("unmatured predictions must not fetch K-lines")

        fake_sdk = type("FakeSdk", (), {
            "get_kline": staticmethod(fail_if_called),
            "normalize_symbol": staticmethod(lambda code: code),
        })
        monkeypatch.setitem(sys.modules, "dsl_data_sdk_original", fake_sdk)
        monkeypatch.setattr(layer1, "PROJECT_ROOT", str(project))
        monkeypatch.setattr(layer1, "_is_trading_day", lambda day: day.weekday() < 5)

        validated, metadata = layer1.load_prediction_history(
            days=30, as_of=date(2026, 6, 7), include_metadata=True)
        historical = layer1.summarize_historical_calibration(
            days=30, as_of=date(2026, 6, 7), current_version="v4.6.2")
        report = layer1.compute_accuracy_report(validated, metadata, historical)

        assert validated == []
        assert metadata["skipped_unmatured"] == 1
        assert metadata["unmatured_examples"][0]["mature_on"] == "2026-06-15"
        assert report["status"] == "warning"
        assert report["validated_count"] == 0
        assert "未成熟" in report["status_reason"]

    def test_historical_calibration_is_version_separated(self, temp_dir, monkeypatch):
        import scripts.audit.layer1_backtest_accuracy as layer1

        project = Path(temp_dir)
        (project / "confidence_data").mkdir()
        (project / "VERSION").write_text("v4.6.2\n", encoding="utf-8")
        (project / "confidence_data" / "prediction_calibration.json").write_text(json.dumps({
            "daily_records": [
                {
                    "date": "2026-06-01",
                    "version": "v4.5.21",
                    "stocks": [
                        {"signal": "buy", "realized_checked": True, "realized_correct": False},
                        {"signal": "sell", "realized_checked": True, "realized_correct": True},
                    ],
                },
                {
                    "date": "2026-06-02",
                    "version": "v4.6.2",
                    "stocks": [
                        {"signal": "buy", "realized_checked": True, "realized_correct": True},
                        {"signal": "hold", "realized_checked": True, "realized_correct": True},
                    ],
                },
            ]
        }), encoding="utf-8")
        monkeypatch.setattr(layer1, "PROJECT_ROOT", str(project))

        summary = layer1.summarize_historical_calibration(
            days=30, as_of=date(2026, 6, 7), current_version="v4.6.2")

        assert summary["total"] == 3
        assert summary["legacy_total"] == 2
        assert summary["current_version_total"] == 1
        assert summary["by_version"]["v4.5.21"]["accuracy"] == 0.5
        assert summary["by_version"]["v4.6.2"]["accuracy"] == 1.0


class TestDashboardAuthDefaults:
    """L0.x: missing web_auth.json must not mean public no-auth access."""

    def test_no_auth_bootstrap_is_loopback_only(self, temp_dir, monkeypatch):
        import web_dashboard.server as server

        class Client:
            def __init__(self, host):
                self.host = host

        class Request:
            def __init__(self, host_header, client_host):
                self.headers = {"host": host_header}
                self.client = Client(client_host)

        missing_auth = temp_dir / "missing_web_auth.json"
        monkeypatch.setattr(server, "AUTH_FILE", str(missing_auth))
        monkeypatch.delenv("DSL_DASHBOARD_ALLOW_NO_AUTH", raising=False)

        assert server._pub_noauth(Request("localhost:8888", "127.0.0.1")) is True
        assert server._pub_noauth(Request("dsl.example.com", "203.0.113.9")) is False


class TestBatchPredictCalibrationGate:
    """L0.x: low calibration accuracy must not open new BUY exposure."""

    def test_low_calibration_blocks_buy_but_allows_sell(self):
        from scripts.batch_predict import (
            _apply_calibration_buy_gate,
            _calibration_allows_buy,
        )

        calibration = {
            "000063": {"last_accuracy": 0.4648},
            "600036": {"last_accuracy": 0.4085},
        }
        buy = {
            "symbol": "000063",
            "signal": "buy",
            "confidence_level": "high",
            "signal_weight": 0.8,
        }
        sell = {
            "symbol": "600036",
            "signal": "sell",
            "confidence_level": "high",
            "signal_weight": 0.8,
        }

        gated_buy = _apply_calibration_buy_gate(buy, calibration)
        gated_sell = _apply_calibration_buy_gate(sell, calibration)

        assert gated_buy["signal"] == "hold"
        assert gated_buy["raw_signal"] == "buy"
        assert gated_buy["calibration_buy_blocked"] is True
        assert gated_buy["signal_weight"] <= 0.39
        assert gated_sell["signal"] == "sell"
        assert "calibration_buy_blocked" not in gated_sell
        assert _calibration_allows_buy(calibration, "000063") is False


class TestDataFreshness:
    """L0.x: daily_predict freshness BUY/SELL gate."""

    def test_missing_prediction_blocks_buy_allows_sell(self, temp_dir):
        result = assess_daily_predict_freshness(
            path=temp_dir / "missing.json",
            now=datetime(2026, 6, 5, 10, 0),
        )
        assert result["status"] == "missing"
        assert result["allow_buy"] is False
        assert result["allow_sell"] is True

    def test_fresh_prediction_allows_buy(self, temp_dir):
        fp = temp_dir / "daily_predict.json"
        fp.write_text(json.dumps({
            "predict_date": "2026-06-05",
            "predict_time": "2026-06-05T09:10:00",
            "predictions": [],
        }), encoding="utf-8")
        result = assess_daily_predict_freshness(
            path=fp,
            now=datetime(2026, 6, 5, 10, 0),
        )
        assert result["status"] == "fresh"
        assert result["allow_buy"] is True
        assert result["allow_sell"] is True

    def test_next_trading_day_prediction_allows_buy_on_weekend(self, temp_dir):
        fp = temp_dir / "daily_predict.json"
        fp.write_text(json.dumps({
            "predict_date": "2026-06-08",
            "predict_time": "2026-06-07T17:10:00",
            "predictions": [],
        }), encoding="utf-8")
        result = assess_daily_predict_freshness(
            path=fp,
            now=datetime(2026, 6, 7, 18, 0),
        )
        assert result["status"] == "fresh"
        assert result["allow_buy"] is True

    def test_stale_prediction_blocks_buy_allows_sell(self, temp_dir):
        fp = temp_dir / "daily_predict.json"
        fp.write_text(json.dumps({
            "predict_date": "2026-06-01",
            "predict_time": "2026-06-01T09:10:00",
            "predictions": [],
        }), encoding="utf-8")
        result = assess_daily_predict_freshness(
            path=fp,
            now=datetime(2026, 6, 5, 10, 0),
        )
        assert result["status"] == "stale"
        assert result["allow_buy"] is False
        assert result["allow_sell"] is True


class TestRetrainQueueSync:
    """L0.x: calibration low-accuracy -> retrain queue."""

    def test_sync_low_accuracy_queues_critical_and_medium(self, temp_dir):
        calibration_path = temp_dir / "prediction_calibration.json"
        degraded_path = temp_dir / "degraded_models.json"
        queue_path = temp_dir / "retrain_queue.json"
        calibration_path.write_text(json.dumps({
            "stock_accuracy": {
                "000001": {"last_accuracy": 0.44, "accuracies": [0.44]},
                "000002": {"last_accuracy": 0.49, "accuracies": [0.49]},
                "000003": {"last_accuracy": 0.55, "accuracies": [0.55]},
            }
        }), encoding="utf-8")
        degraded_path.write_text("{}", encoding="utf-8")

        mgr = RetrainQueueManager(queue_path=queue_path)
        summary = mgr.sync_low_accuracy_from_calibration(calibration_path, degraded_path)
        assert summary["added"] == 2
        assert summary["critical"] == 1
        assert summary["medium"] == 1

        second = mgr.sync_low_accuracy_from_calibration(calibration_path, degraded_path)
        assert second["added"] == 0
        assert second["skipped"] == 2

        queued = mgr.list_pending()
        assert {item["symbol"] for item in queued} == {"000001", "000002"}
        assert {item["priority"] for item in queued} == {"critical", "medium"}

    def test_completed_entry_does_not_suppress_new_low_accuracy_queue(self, temp_dir):
        calibration_path = temp_dir / "prediction_calibration.json"
        queue_path = temp_dir / "retrain_queue.json"
        calibration_path.write_text(json.dumps({
            "stock_accuracy": {
                "000001": {"last_accuracy": 0.44, "accuracies": [0.44]},
            }
        }), encoding="utf-8")

        mgr = RetrainQueueManager(queue_path=queue_path)
        assert mgr.sync_low_accuracy_from_calibration(calibration_path)["added"] == 1
        assert mgr.mark_completed("000001", {"accuracy": 0.46})
        assert mgr.sync_low_accuracy_from_calibration(calibration_path)["added"] == 1
        assert len(mgr.list_pending()) == 1


class TestTrainPredictorV3Regression:
    """L0.x: training path regression checks."""

    def test_train_rank_log_uses_defined_train_end_date(self, project_root):
        source = (project_root / "scripts" / "train_predictor_v3.py").read_text(encoding="utf-8")
        assert "截止{test_end_date}" not in source
        assert "截止{end_date}" in source

    def test_train_rank_features_use_real_dates_and_enter_feature_cols(self, project_root):
        source = (project_root / "scripts" / "train_predictor_v3.py").read_text(encoding="utf-8")
        assert '.set_index("date", drop=False)' in source
        assert "_add_cross_sectional_features(train, code" in source
        assert "_add_cross_sectional_features(val, code" in source
        assert "_add_cross_sectional_features(test, code" in source
        assert "feature_cols = [c for c in train.columns" in source
        assert "_aligned_feature_frame = pd.concat" in source

    def test_train_rank_cache_only_stores_current_snapshot(self, project_root):
        source = (project_root / "scripts" / "train_predictor_v3.py").read_text(encoding="utf-8")
        assert "cache_current_snapshot = end_date is None" in source
        assert "if _global_rank_cache is not None and cache_current_snapshot" in source
        assert "if cache_current_snapshot:\n            _global_rank_cache = result" in source
        assert "start = (end_dt - timedelta(days=TRAIN_YEARS * 365)).strftime" in source

    def test_pool_predictor_fills_rank_features_neutrally(self, project_root):
        source = (project_root / "core" / "pool_predictor.py").read_text(encoding="utf-8")
        assert "feat.startswith('rank_')" in source
        assert "df[feat] = 0.5" in source
        assert "feat.startswith('rel_')" in source
        assert "df[feat] = 0.0" in source

    def test_pool_predictor_applies_v3_scaler_and_metadata(self, project_root):
        source = (project_root / "core" / "pool_predictor.py").read_text(encoding="utf-8")
        assert "scaler_path = os.path.join(code_dir, 'scaler.pkl')" in source
        assert "'scaler': scaler" in source
        assert "latest_features = scaler.transform(latest_features)" in source
        assert "'val_dir_accuracy': metadata.get('val_dir_accuracy')" in source
        assert "'clf_accuracy': metadata.get('clf_accuracy')" in source
        assert "'lgb_polarity': metadata.get('lgb_polarity', 1)" in source
        assert "'clf_polarity': metadata.get('clf_polarity', 1)" in source
        assert "pred_return *= int(model_data.get('lgb_polarity', 1) or 1)" in source
        assert "clf_proba = 1 - clf_proba" in source

    def test_train_predictor_uses_validation_only_polarity_calibration(self, project_root):
        source = (project_root / "scripts" / "train_predictor_v3.py").read_text(encoding="utf-8")
        assert "lgb_polarity = -1 if lgb_acc < 0.5 else 1" in source
        assert "clf_polarity = -1 if clf_acc < 0.5 else 1" in source
        assert "lgb_eff_acc = max(lgb_acc, 1 - lgb_acc)" in source
        assert "test_lgb_sig = ((lgb_test_preds > 0).astype(float) * 2 - 1) * lgb_polarity" in source
        assert "'lgb_polarity': int(lgb_polarity)" in source
        assert "'clf_polarity': int(clf_polarity)" in source

    def test_batch_train_calibration_sync_is_idempotent_per_process(self, project_root):
        source = (project_root / "scripts" / "batch_train.py").read_text(encoding="utf-8")
        assert "synced_accuracy_keys" in source
        assert "def _sync_accuracy_to_calibration_once" in source
        assert "_checkpoint_state[\"finalized\"] = True" in source
        assert "if _checkpoint_state.get(\"finalized\")" in source
        assert "低精度闭环复核" in source
        assert "resync = mgr.sync_low_accuracy_from_calibration()" in source

    def test_pool_predictor_restores_compute_features_and_uses_train_v3_builder(self, project_root):
        source = (project_root / "core" / "pool_predictor.py").read_text(encoding="utf-8")
        assert "def _compute_features(self, df, feature_cols):" in source
        assert "def _compute_train_v3_features(self, df, feature_cols):" in source
        assert "from scripts.train_predictor_v3 import build_features" in source
        assert "X = self._compute_train_v3_features(df, feature_cols)" in source


class TestAShareFeatureExtender:
    """L0.x: A-share feature extension."""

    def test_add_consecutive_limit_up_counts_runs(self):
        df = pd.DataFrame({"limit_up": [True, True, False, True, False, True, True, True]})
        out = AShareFeatureExtender.add_consecutive_limit_up(df)
        assert out["consecutive_limit_up"].tolist() == [1, 2, 0, 1, 0, 1, 2, 3]
        assert "consecutive_limit_up" not in df.columns


class TestPoolStructure:
    """L0.3: Master stock pool — structure, fields, tier distribution."""

    @pytest.fixture
    def pool(self, project_root):
        with open(project_root / "config" / "master_stock_pool.yaml") as f:
            return yaml.safe_load(f)["master_pool"]

    def test_required_fields(self, pool):
        required = ["symbol", "name", "tier", "score"]
        for s in pool[:5]:
            for r in required:
                assert r in s, f"{s['symbol']} missing {r}"

    def test_tier_distribution(self, pool):
        tiers = {}
        for s in pool:
            tiers[s.get("tier", "?")] = tiers.get(s.get("tier", "?"), 0) + 1
        assert "bluechip" in tiers
        assert "core" in tiers
        assert "growth" in tiers

    def test_no_duplicates(self, pool):
        symbols = [str(s["symbol"]).zfill(6) for s in pool]
        assert len(symbols) == len(set(symbols))

    def test_pool_size(self, pool):
        assert 30 <= len(pool) <= 60, f"{len(pool)} stocks"

    def test_active_sector_concentration(self, pool):
        policy_path = Path(__file__).resolve().parent.parent / "config" / "pool_structure_policy.yaml"
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        observation = set(policy["observation_tiers"])
        max_ratio = policy["limits"]["max_active_sector_ratio"]
        active = [s for s in pool if s.get("tier") not in observation]
        sectors = {}
        for stock in active:
            sectors[stock.get("sector", "")] = sectors.get(stock.get("sector", ""), 0) + 1
        assert sectors
        assert max(sectors.values()) / len(active) <= max_ratio

    def test_stock_pool_matches_master_tier_sector_and_allocation(self, pool, project_root):
        stock_pool = yaml.safe_load((project_root / "config" / "stock_pool.yaml").read_text(encoding="utf-8"))
        master_meta = {
            str(s["symbol"]).zfill(6): (s.get("tier"), s.get("sector", ""))
            for s in pool
        }
        stock_meta = {}
        allocation_sum = 0.0
        for tier, cfg in stock_pool.get("tiers", {}).items():
            allocation_sum += float(cfg.get("allocation", 0) or 0)
            for stock in cfg.get("stocks", []):
                stock_meta[str(stock["code"]).zfill(6)] = (tier, stock.get("sector", ""))
        assert set(master_meta) == set(stock_meta)
        assert master_meta == stock_meta
        assert abs(allocation_sum - 1.0) <= 0.001

    def test_policy_pair_controls_are_reflected_in_master_tiers(self, pool, project_root):
        policy = yaml.safe_load((project_root / "config" / "pool_structure_policy.yaml").read_text(encoding="utf-8"))
        observation = set(policy["observation_tiers"])
        tiers = {str(s["symbol"]).zfill(6): s.get("tier") for s in pool}
        for control in policy["pair_controls"]:
            active = [
                symbol for symbol in control["symbols"]
                if tiers.get(symbol) not in observation
            ]
            assert len(active) <= control["active_limit"]


class TestBatchPredictQualityGates:
    """L0.x: Prediction output must fail closed for weak BUY signals."""

    def test_model_quality_buy_gate_blocks_low_accuracy_buy(self):
        from scripts.batch_predict import _apply_model_quality_buy_gate

        record = {
            "symbol": "300274",
            "signal": "buy",
            "direction_accuracy": 0.49,
            "confidence_level": "high",
            "signal_weight": 0.8,
        }
        out = _apply_model_quality_buy_gate(record)
        assert out["signal"] == "hold"
        assert out["confidence_level"] == "low"
        assert out["model_quality_buy_blocked"] is True
        assert out["signal_weight"] <= 0.39

    def test_sector_quality_buy_gate_blocks_weak_sector_buy(self):
        from scripts.batch_predict import _apply_sector_quality_buy_gate

        rows = [
            {"symbol": "a", "sector": "电力设备", "tier": "core", "signal": "buy", "direction_accuracy": 0.45, "confidence_level": "high", "signal_weight": 0.8},
            {"symbol": "b", "sector": "电力设备", "tier": "core", "signal": "hold", "direction_accuracy": 0.47, "confidence_level": "low", "signal_weight": 0.2},
            {"symbol": "c", "sector": "食品饮料", "tier": "core", "signal": "buy", "direction_accuracy": 0.65, "confidence_level": "high", "signal_weight": 0.8},
        ]
        blocked = _apply_sector_quality_buy_gate(rows)
        assert blocked == {"a"}
        assert rows[0]["signal"] == "hold"
        assert rows[0]["sector_quality_buy_blocked"] is True
        assert rows[2]["signal"] == "buy"


class TestCalibrationData:
    """L0.4: Calibration data — keys and validity."""

    @pytest.fixture
    def cal(self, project_root):
        with open(project_root / "confidence_data" / "prediction_calibration.json") as f:
            return json.load(f)

    def test_has_top_level_keys(self, cal):
        assert "overall_stats" in cal
        assert "stock_accuracy" in cal
        assert "daily_records" in cal

    def test_stats_valid(self, cal):
        stats = cal["overall_stats"]
        assert "accuracy_rate" in stats
        assert 0 <= stats.get("accuracy_rate", -1) <= 1
        assert "last_date" in stats


class TestCircuitBreaker:
    """L0.5: Circuit breaker — data file integrity."""

    @pytest.fixture
    def cb(self, project_root):
        with open(project_root / "data" / "circuit_breaker.json") as f:
            return json.load(f)

    def test_required_keys(self, cb):
        required = ["trading_paused", "today_trade_count", "last_trade_date",
                     "consecutive_failed_trades", "today_drawdown"]
        for k in required:
            assert k in cb

    def test_not_paused(self, cb):
        assert not cb.get("trading_paused", True)

    def test_date_current(self, cb):
        today = datetime.now().date()
        latest_trading_day = datetime.now().date()
        while not is_trading_day(latest_trading_day, "A_SHARE"):
            latest_trading_day -= timedelta(days=1)
        assert cb.get("last_trade_date") in {
            today.strftime("%Y-%m-%d"),
            latest_trading_day.strftime("%Y-%m-%d"),
        }


class TestFeedbackController:
    """L0.6: Feedback controller — black swan events and position ratios."""

    def test_events_severity_ge_3_key(self):
        data = {
            "analysis_date": "2026-05-05T12:00:00",
            "events_severity_ge_3": [
                {"event_id": "BS-001", "category": "geopolitical", "severity": 5,
                 "title": "Test", "confidence": 0.8, "description": "",
                 "source": "test", "published": "2026-05-05", "trend": "escalating",
                 "behavior_analysis": {}, "historical_calibration": {},
                 "structured_predictions": [], "market_impact": {}},
                {"event_id": "BS-002", "category": "financial", "severity": 3,
                 "title": "Low", "confidence": 0.6, "description": "",
                 "source": "test", "published": "2026-05-05", "trend": "stable",
                 "behavior_analysis": {}, "historical_calibration": {},
                 "structured_predictions": [], "market_impact": {}},
            ],
            "market_prices": {}, "scan_summary": {}, "calibration_update": {},
        }
        events = data.get("events_severity_ge_3", [])
        assert len(events) == 2

    def test_old_events_key_absent(self):
        data = {"events_severity_ge_3": []}
        assert len(data.get("events", [])) == 0

    def test_event_id_field(self):
        ev = {"event_id": "BS-001"}
        assert ev.get("event_id") == "BS-001"
        assert ev.get("event_id", ev.get("id")) == "BS-001"

    @pytest.mark.parametrize("severity,expected", [
        (9, 0.10), (8, 0.10), (7, 0.10), (6, 0.10), (5, 0.10),
        (4, 0.25), (3, 0.50), (2, 0.75), (1, 1.0), (0, 1.0),
    ])
    def test_fallback_position_ratio(self, severity, expected):
        assert _fallback_position_ratio(severity) == expected

    def test_category_weights(self):
        weights = {
            "geopolitical": 1.0, "geopolitical_military": 1.0,
            "financial": 0.9, "policy": 0.9,
            "tech": 0.7, "health": 0.6, "natural": 0.5, "social": 0.5,
        }
        assert weights.get("geopolitical", 0) == 1.0
        assert weights.get("financial", 0) == 0.9


class TestPositionRatioConsistency:
    """L0.7: position_ratio field name consistency check."""

    def test_adapt_params_has_bs_ratio(self, project_root):
        with open(project_root / "config" / "adaptive_params.yaml") as f:
            ap = yaml.safe_load(f)
        bs_ratio = ap.get("risk", {}).get("black_swan_position_ratio", None)
        assert bs_ratio is not None
        assert 0.0 <= bs_ratio <= 1.0
