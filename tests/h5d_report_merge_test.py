import json
import os
import time
from pathlib import Path

import yaml


def _write_json(path: Path, data: dict, mtime: float):
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _h5d_report(symbols, predicted_return=0.01, direction_accuracy=0.51):
    return {
        symbol: {
            "h5d": {
                "predicted_return": predicted_return,
                "direction_accuracy": direction_accuracy,
                "clf_accuracy": direction_accuracy,
                "lgb_accuracy": direction_accuracy - 0.01,
                "xgb_accuracy": direction_accuracy - 0.02,
                "cv_accuracy": direction_accuracy - 0.03,
                "cv_clf_accuracy": direction_accuracy - 0.04,
            }
        }
        for symbol in symbols
    }


def test_recent_partial_report_overlays_latest_full_report(monkeypatch, tmp_path):
    from scripts import batch_predict

    enhanced_dir = tmp_path / "reports" / "predictor"
    enhanced_dir.mkdir(parents=True)
    pool_path = tmp_path / "config" / "master_stock_pool.yaml"
    pool_path.parent.mkdir(parents=True)

    pool_symbols = [f"{i:06d}" for i in range(1, 43)]
    pool_path.write_text(
        yaml.safe_dump({"master_pool": [{"symbol": s, "name": s} for s in pool_symbols]}),
        encoding="utf-8",
    )

    now = time.time()
    full_symbols = pool_symbols[:24]
    full_report = _h5d_report(full_symbols, predicted_return=0.01, direction_accuracy=0.52)
    partial_report = _h5d_report(["000001"], predicted_return=0.05, direction_accuracy=0.61)

    _write_json(
        enhanced_dir / "prediction_enhanced_20260604_163225.json",
        full_report,
        now - 4 * 24 * 3600,
    )
    _write_json(
        enhanced_dir / "prediction_enhanced_20260607_090749.json",
        partial_report,
        now - 3600,
    )

    monkeypatch.setattr(batch_predict, "ENHANCED_DIR", str(enhanced_dir))
    monkeypatch.setattr(batch_predict, "POOL_PATH", str(pool_path))

    merged = batch_predict.load_enhanced_predictions()

    assert len(merged) == len(full_symbols)
    assert merged["000001"]["predicted_return"] == 0.05
    assert merged["000001"]["direction_accuracy"] == 0.61
    assert merged["000002"]["predicted_return"] == 0.01
    assert merged["000002"]["direction_accuracy"] == 0.52


def test_save_checkpoint_writes_per_symbol_last_train_time(monkeypatch, tmp_path):
    from scripts import batch_train

    monkeypatch.setattr(batch_train, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(batch_train, "_sync_accuracy_to_calibration_once", lambda _results: None)

    results = [
        {
            "symbol": "000001",
            "status": "success",
            "direction_accuracy": 0.57,
        }
    ]
    stocks = [{"symbol": "000001", "name": "test"}]

    batch_train._save_checkpoint(results, stocks, batch_num=1)

    status_path = tmp_path / "cache" / "training_status.json"
    training_status = json.loads(status_path.read_text(encoding="utf-8"))
    symbol_status = training_status["000001"]

    assert symbol_status["last_train_date"]
    assert symbol_status["last_train_time"]
    assert symbol_status["status"] == "success"
    assert symbol_status["direction_accuracy"] == 0.57
    assert symbol_status["model_fresh"] is True
    assert training_status["_last_batch_train_time"] == symbol_status["last_train_time"]
