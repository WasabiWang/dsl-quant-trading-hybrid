"""Training tools: retrain, model accuracy, calibration."""

import json, os, sys

_DSL_ROOT = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")


def _ensure_dsl():
    if _DSL_ROOT not in sys.path:
        sys.path.insert(0, _DSL_ROOT)
    if os.getcwd() != _DSL_ROOT:
        os.chdir(_DSL_ROOT)


async def trigger_retrain(symbol: str) -> str:
    """触发单只股票模型重训练。返回训练结果和精度。"""
    _ensure_dsl()
    import warnings
    warnings.filterwarnings("ignore")
    from scripts.train_predictor_v3 import train_single_stock
    result = train_single_stock(symbol, symbol)
    if result and "error" not in result:
        return json.dumps({
            "symbol": result["symbol"],
            "direction_accuracy": result["direction_accuracy"],
            "cv_accuracy": result["cv_accuracy"],
            "features_used": result["features_used"],
            "ensemble_weights": result["ensemble_weights"],
            "status": "success",
        }, ensure_ascii=False, indent=2)
    return json.dumps({"status": "failed", "error": str(result)}, ensure_ascii=False)


async def get_model_quality(symbol: str = "") -> str:
    """获取模型质量报告：精度、R²、校准状态。"""
    _ensure_dsl()
    from web_dashboard.data_adapter import get_calibration
    cal = get_calibration()
    stocks = cal["stocks"]
    if symbol:
        stocks = [s for s in stocks if s["symbol"] == symbol]
    return json.dumps({
        "overall_accuracy": cal["overall"]["accuracy_rate"],
        "summary": cal["summary"],
        "stocks": stocks[:20] if not symbol else stocks,
    }, ensure_ascii=False, indent=2)


async def get_retrain_queue() -> str:
    """获取重训练队列状态。"""
    _ensure_dsl()
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        mgr = get_retrain_queue_manager()
        return json.dumps(mgr.get_summary(), ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
