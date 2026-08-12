#!/usr/bin/env python3
"""
DSL量化交易系统 - 数据适配器
读取DSL系统输出文件，为Web Dashboard提供结构化数据
"""
import os, json, yaml, sys, importlib, subprocess
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
CONFIDENCE_DIR = os.path.join(PROJECT_ROOT, "confidence_data")
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")


# ── P1-4: 数据陈旧度检测 ──
def _file_staleness(path: str) -> dict:
    if not os.path.exists(path):
        return {"exists": False, "age_hours": -1, "mtime": ""}
    mtime = os.path.getmtime(path)
    age = (time.time() - mtime) / 3600
    return {"exists": True, "age_hours": round(age, 1),
            "mtime": datetime.fromtimestamp(mtime).isoformat(),
            "stale": age > 24}

def _repair_llm_json(text: str) -> str:
    """修复LLM生成JSON的常见错误：未转义引号、多余逗号
    
    典型问题：LLM在JSON字符串值中输出ASCII双引号却不转义：
      "title": "下周"超级央行周"..."  →  inner " breaks JSON
    
    策略：对按行检测JSON字符串值中的未转义ASCII引号并转义。
    """
    lines = text.split('\n')
    fixed = []
    for line in lines:
        stripped = line.strip()
        # 跳过非字符串值行（数组/花括号/键值对中的非字符串值）
        if not stripped:
            fixed.append(line)
            continue
        # 检查是否为JSON字符串行：以 " 开头，以 " 或 ", 结尾
        if not (stripped.startswith('"') and (stripped.endswith('",') or stripped.endswith('"'))):
            fixed.append(line)
            continue
        raw = line.rstrip('\n')
        indices = [i for i, c in enumerate(raw) if c == '"']
        if len(indices) <= 2:
            fixed.append(line)
            continue
        # 判断是否有 key: value 格式
        colon_idx = raw.find(': ')
        if colon_idx > 0 and colon_idx < indices[-1] and raw[colon_idx - 1] in ('"', ' '):
            # "key": "value" 格式 — value 起始于 colon后的第一个 "
            val_start = raw.index('"', colon_idx)
        else:
            # 简单字符串 "value"
            val_start = indices[0]
        val_end = indices[-1]
        interior = [i for i in indices if val_start < i < val_end]
        if not interior:
            fixed.append(line)
            continue
        line_list = list(raw)
        for idx in reversed(interior):
            if idx > 0 and line_list[idx - 1] == '\\':
                continue
            line_list.insert(idx, '\\')
        fixed.append(''.join(line_list))
    return '\n'.join(fixed)


def safe_read_json(path: str, max_age_hours: float = 0) -> Optional[Dict]:
    """安全读取JSON文件，max_age_hours>0时拒绝陈旧数据
    
    增加LLM JSON修复：当标准解析失败时，自动修复常见LLM输出错误。
    """
    try:
        if os.path.exists(path):
            if max_age_hours > 0:
                stale = _file_staleness(path)
                if stale["age_hours"] > max_age_hours:
                    print(f"[data_adapter] {path} 已过期 ({stale['age_hours']}h > {max_age_hours}h)")
                    return None
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                # 尝试LLM修复
                fixed = _repair_llm_json(raw)
                try:
                    result = json.loads(fixed)
                    print(f"[data_adapter] {path} LLM JSON修复成功 ({len(fixed) - len(raw)} chars changed)")
                    # 写回修复后的内容
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(fixed)
                    return result
                except json.JSONDecodeError:
                    print(f"[data_adapter] {path} LLM JSON修复也失败: {e}")
                    return None
    except Exception as e:
        print(f"[data_adapter] JSON读取失败 {path}: {e}")
    return None


def safe_read_yaml(path: str) -> Optional[Dict]:
    """安全读取YAML文件"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    except Exception as e:
        print(f"[data_adapter] YAML读取失败 {path}: {e}")
    return None


def _read_version() -> Dict[str, str]:
    """读取VERSION文件"""
    vpath = os.path.join(PROJECT_ROOT, "VERSION")
    result = {"version": "?", "build": "", "codename": ""}
    try:
        if os.path.exists(vpath):
            with open(vpath, "r") as f:
                lines = f.read().strip().split("\n")
            if lines:
                result["version"] = lines[0].strip()
            for l in lines[1:]:
                if l.startswith("build:"):
                    result["build"] = l.split(":", 1)[1].strip()
                elif l.startswith("codename:"):
                    result["codename"] = l.split(":", 1)[1].strip()
    except Exception:
        pass
    return result


# ── 系统状态 ──

def _get_latest_backtest_performance() -> dict:
    """读取最新walkforward回测绩效（用于总览页展示）"""
    import glob
    bt_dir = os.path.join(PROJECT_ROOT, "reports", "backtest")
    pattern = os.path.join(bt_dir, "walkforward_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return {}
    data = safe_read_json(files[-1])
    if data and data.get("performance"):
        return data["performance"]
    return {}


def _get_paper_trading_metrics() -> dict:
    """从paper_trading.db读取模拟交易实际盈亏（返回值统一为百分比格式，如 42.53 表示 42.53%）"""
    import sqlite3
    result = {"total_return_pct": 0, "win_rate": 0}
    db_path = os.path.join(DATA_DIR, "paper_trading.db")
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT key, value FROM performance_metrics").fetchall()
        for key, value in rows:
            try:
                val = float(value)
                # 如果total_return_pct存储为小数(0.412=41.2%), 转换为百分比格式
                if key == "total_return_pct" and -1 < val < 1:
                    val = round(val * 100, 2)
                result[key] = val
            except (ValueError, TypeError):
                pass
        # 如果performance_metrics没有total_return_pct，从账本计算
        if not result.get("total_return_pct"):
            initial = conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()
            cash = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
            if initial and cash:
                init_val = float(initial[0])
                cash_val = float(cash[0])
                pos_rows = conn.execute("SELECT quantity, current_price FROM positions").fetchall()
                mkt_val = sum(qty * price for qty, price in pos_rows)
                total = cash_val + mkt_val
                if init_val > 0:
                    result["total_return_pct"] = round((total / init_val - 1) * 100, 2)
        conn.close()
    except Exception:
        pass
    return result

def _get_paper_trading_summary() -> dict:
    """从paper_trading.db读取模拟盘实时摘要（总览页绩效卡数据源, v4.6.9）
    
    返回: initial_capital/cash/market_value/total_value/total_pnl/
          total_pnl_pct/position_count/total_return_pct/win_rate
    """
    import sqlite3
    result = {"initial_capital": 1000000, "cash": 0, "market_value": 0,
              "total_value": 0, "total_pnl": 0, "total_pnl_pct": 0,
              "position_count": 0, "total_return_pct": 0, "win_rate": 0}
    db_path = os.path.join(DATA_DIR, "paper_trading.db")
    if not os.path.exists(db_path):
        return result
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        ledger = {}
        for r in conn.execute("SELECT key, value FROM ledger").fetchall():
            try:
                ledger[r["key"]] = float(r["value"])
            except (ValueError, TypeError):
                ledger[r["key"]] = r["value"]
        initial = float(ledger.get("initial_capital", 1000000))
        cash = float(ledger.get("current_cash", 0))
        pos_rows = conn.execute("SELECT quantity, current_price FROM positions").fetchall()
        mkt_val = sum((r["quantity"] or 0) * (r["current_price"] or 0) for r in pos_rows)
        total_val = cash + mkt_val
        # performance_metrics
        perf = {}
        for r in conn.execute("SELECT key, value FROM performance_metrics").fetchall():
            try:
                perf[r["key"]] = float(r["value"])
            except (ValueError, TypeError):
                pass
        conn.close()
        result.update({
            "initial_capital": round(initial, 2),
            "cash": round(cash, 2),
            "market_value": round(mkt_val, 2),
            "total_value": round(total_val, 2),
            "total_pnl": round(total_val - initial, 2),
            "total_pnl_pct": round((total_val / initial - 1) * 100, 2) if initial else 0,
            "position_count": len(pos_rows),
            "total_return_pct": perf.get("total_return_pct", round((total_val / initial - 1) * 100, 2) if initial else 0),
            "win_rate": perf.get("win_rate", 0),
        })
    except Exception:
        pass
    return result


def get_system_status() -> Dict[str, Any]:
    """获取系统整体状态"""
    circuit = safe_read_json(os.path.join(DATA_DIR, "circuit_breaker.json")) or {}
    error_stats = safe_read_json(os.path.join(DATA_DIR, "error_stats.json")) or {}
    calib = safe_read_json(os.path.join(CONFIDENCE_DIR, "prediction_calibration.json")) or {}
    daily_predict = _read_latest_daily_predict() or {}

    # 健康状况
    health = "healthy"
    if circuit.get("trading_paused"):
        health = "paused"
    elif error_stats.get("total_errors", 0) > 5:
        health = "degraded"

    # 今日交易
    today_trades = circuit.get("today_trades", circuit.get("today_trade_count", 0))

    # 信号统计 - v4.5.7: 从daily_predict.json实时计数(不再用daily_records累加)
    buy = sell = hold = 0
    for p in daily_predict.get("predictions", []):
        sig = p.get("signal", "").lower()
        if sig == "buy":
            buy += 1
        elif sig == "sell":
            sell += 1
        elif sig == "hold":
            hold += 1

    # 模型精度
    stock_acc = calib.get("stock_accuracy", {})
    total_stocks = len(stock_acc)

    # 假日状态
    is_trading_day = _check_trading_day()

    # 从overall_stats取精度率
    overall = calib.get("overall_stats", {})

    # 真实绩效数据: 回测 + 模拟交易
    bt_perf = _get_latest_backtest_performance()
    paper_metrics = _get_paper_trading_metrics()

    return {
        "health": health,
        "health_trading_paused": circuit.get("trading_paused", False),
        "trading_paused_reason": circuit.get("paused_reason", ""),
        "today_trades": today_trades,
        "consecutive_failures": circuit.get("consecutive_failures", 0),
        "last_trade_date": circuit.get("last_trade_date", ""),
        "total_errors": error_stats.get("total_errors", 0),
        "signal_count_buy": buy,
        "signal_count_sell": sell,
        "signal_count_hold": hold,
        "total_stocks": total_stocks,
        "is_trading_day": is_trading_day,
        "accuracy_rate": overall.get("accuracy_rate", 0),
        "correct_predictions": overall.get("correct_predictions", 0),
        "total_predictions": overall.get("total_predictions", 0),
        "last_predict_update": calib.get("last_updated", ""),
        # P1-4: 数据文件陈旧度
        "data_staleness": {
            "daily_predict": _file_staleness(os.path.join(CACHE_DIR, "daily_predict.json")),
            "prediction_calibration": _file_staleness(os.path.join(CONFIDENCE_DIR, "prediction_calibration.json")),
            "cron_status": _file_staleness(os.path.join(DATA_DIR, "cron_status.json")),
            "master_pool": _file_staleness(os.path.join(CONFIG_DIR, "master_stock_pool.yaml")),
            "adaptive_params": _file_staleness(os.path.join(CONFIG_DIR, "adaptive_params.yaml")),
        },
        # v4.5.9: 黑天鹅+position_ratio同步到status API
        "black_swan_active": get_blackswan().get("active", False),
        "position_ratio": get_blackswan().get("position_ratio", 1.0),
        "version": _read_version().get("version", "?"),
        # v4.6.9: 模拟盘真实绩效（总览页绩效卡数据源, 替代回测数字）
        "paper": _get_paper_trading_summary(),
        # 真实绩效数据（回测, 保留用于其他页面/回测对比）
        "backtest": {
            "total_return": bt_perf.get("total_return_pct", 0),
            "annual_return": bt_perf.get("annual_return_pct", 0),
            "win_rate": bt_perf.get("win_rate_pct", 0),
            "profit_factor": bt_perf.get("profit_factor", 0),
            "sharpe_ratio": bt_perf.get("sharpe_ratio", 0),
            "max_drawdown": abs(bt_perf.get("max_drawdown_pct", 0)),
            "total_trades": bt_perf.get("total_trades", 0),
            "source_file": _latest_backtest_file(),
        },
        # 兼容字段（总览页改版前引用）
        "total_return": bt_perf.get("total_return_pct", paper_metrics.get("total_return_pct", 0)),
        "annual_return": bt_perf.get("annual_return_pct", 0),
        "win_rate": bt_perf.get("win_rate_pct", paper_metrics.get("win_rate", 0)),
        "profit_factor": bt_perf.get("profit_factor", 0),
        "sharpe_ratio": bt_perf.get("sharpe_ratio", 0),
        "max_drawdown": abs(bt_perf.get("max_drawdown_pct", 0)),
    }


def _latest_backtest_file() -> str:
    """返回最新回测文件名（用于标注数据来源）"""
    import glob
    bt_dir = os.path.join(PROJECT_ROOT, "reports", "backtest")
    files = sorted(glob.glob(os.path.join(bt_dir, "walkforward_*.json")))
    return os.path.basename(files[-1]) if files else ""


def _check_trading_day() -> bool:
    """检查今天是否A股交易日 — 委派到 holiday_calendar.py"""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from config.holiday_calendar import is_trading_day
        return is_trading_day(market="A_SHARE")
    except Exception:
        # fallback: 周末制
        return date.today().weekday() < 5


# ── 股票池 ──

def get_stock_pool() -> Dict[str, Any]:
    """获取股票池信息"""
    pool = safe_read_yaml(os.path.join(CONFIG_DIR, "master_stock_pool.yaml")) or {}
    stocks = pool.get("master_pool", [])
    tiers = {"bluechip": 0, "core": 0, "growth": 0, "cyclical": 0, "flex": 0}
    for s in stocks:
        t = s.get("tier", "core")
        tiers[t] = tiers.get(t, 0) + 1
    return {
        "total": len(stocks),
        "tiers": tiers,
        "stocks": stocks
    }


def _stock_pool_meta_by_symbol() -> Dict[str, Dict[str, Any]]:
    """Return authoritative stock metadata keyed by zero-padded symbol."""
    pool = safe_read_yaml(os.path.join(CONFIG_DIR, "master_stock_pool.yaml")) or {}
    meta = {}
    for stock in pool.get("master_pool", []) or []:
        symbol = str(stock.get("symbol", stock.get("code", ""))).zfill(6)
        if symbol:
            meta[symbol] = stock
    return meta


def _tier_layer_name(tier: str) -> str:
    return {
        "bluechip": "蓝筹池",
        "core": "核心池",
        "growth": "成长池",
        "cyclical": "周期池",
        "flex": "观察池",
    }.get(tier, tier or "")


def _enrich_predictions_with_pool_meta(predictions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Overlay mutable pool metadata from master_stock_pool.yaml onto prediction rows."""
    meta_by_symbol = _stock_pool_meta_by_symbol()
    enriched = []
    for pred in predictions:
        row = dict(pred)
        symbol = str(row.get("symbol", row.get("code", ""))).zfill(6)
        meta = meta_by_symbol.get(symbol)
        if meta:
            original_tier = row.get("tier")
            row["symbol"] = symbol
            row["name"] = meta.get("name", row.get("name", ""))
            row["tier"] = meta.get("tier", row.get("tier", ""))
            row["sector"] = meta.get("sector", row.get("sector", ""))
            row["concept"] = meta.get("concept", row.get("concept", ""))
            row["layer"] = _tier_layer_name(row.get("tier", ""))
            if meta.get("score") not in (None, ""):
                row["pool_score"] = meta.get("score")
            if original_tier and original_tier != row.get("tier"):
                row["cached_tier"] = original_tier
        enriched.append(row)
    return enriched


# ── 最新预测 ──

def _read_latest_daily_predict() -> Optional[Dict]:
    """尝试读取daily_predict.json，优先读取非holiday_bak版本"""
    primary = os.path.join(CACHE_DIR, "daily_predict.json")
    backup = os.path.join(CACHE_DIR, "daily_predict.json.holiday_bak")
    prev = os.path.join(CACHE_DIR, "daily_predict.json.prev")

    data = safe_read_json(primary)
    if data and data.get("predictions"):
        return data
    # v4.5.7: 假日时回退到holiday_bak (含完整h20d/source/cross_confirmed数据)
    data = safe_read_json(backup)
    if data and data.get("predictions"):
        return data
    data = safe_read_json(prev)
    if data and data.get("predictions"):
        return data
    # 最终fallback: 从校准数据的daily_records提取
    calib = safe_read_json(os.path.join(CONFIDENCE_DIR, "prediction_calibration.json")) or {}
    records = calib.get("daily_records", [])
    if records:
        latest = records[-1]  # 最新一天
        stocks = latest.get("stocks", [])
        return {
            "timestamp": latest.get("date", ""),
            "predictions": stocks
        }
    return None


def get_predictions() -> Dict[str, Any]:
    """获取最新预测结果"""
    data = _read_latest_daily_predict() or {}
    predictions = _enrich_predictions_with_pool_meta(data.get("predictions", []) or [])
    return {
        "timestamp": data.get("timestamp", data.get("date", "")),
        "total": len(predictions),
        "predictions": predictions
    }


# ── 模型校准 ──

def get_calibration() -> Dict[str, Any]:
    """获取模型校准数据"""
    calib = safe_read_json(os.path.join(CONFIDENCE_DIR, "prediction_calibration.json")) or {}
    stock_acc = calib.get("stock_accuracy", {})

    # v4.6.9d: 阈值动态读取 — 供前端过滤使用, 避免前端硬编码0.45与后端脱节
    thresholds = {"retrain_urgent": _RETRAIN_URGENT, "retrain_planned": _RETRAIN_PLANNED}
    try:
        _t_urgent = float(calib.get("retrain_urgent", _RETRAIN_URGENT))
        _t_planned = float(calib.get("retrain_planned", _RETRAIN_PLANNED))
        if 0 < _t_urgent < _t_planned < 1:
            thresholds = {"retrain_urgent": _t_urgent, "retrain_planned": _t_planned}
    except (ValueError, TypeError):
        pass

    # 整理为每只标的的精度数据
    calibrated = []
    # 读取模型文件时间戳作为训练时间
    model_train_times = {}
    models_dir = os.path.join(PROJECT_ROOT, "models")
    if os.path.exists(models_dir):
        for d in os.listdir(models_dir):
            dpath = os.path.join(models_dir, d)
            if os.path.isdir(dpath) and not d.startswith("_"):
                pkl_files = [f for f in os.listdir(dpath) if f.endswith(".pkl")]
                if pkl_files:
                    mtimes = []
                    for pf in pkl_files:
                        fpath = os.path.join(dpath, pf)
                        try:
                            mtimes.append(os.path.getmtime(fpath))
                        except:
                            pass
                    if mtimes:
                        latest_ts = datetime.fromtimestamp(max(mtimes))
                        model_train_times[d] = latest_ts.strftime("%m-%d %H:%M")
    
    for symbol, data in stock_acc.items():
        if isinstance(data, dict):
            acc = data.get("last_accuracy", 0) or 0
            h20d_acc = data.get("h20d_accuracy", 0) or 0
            # v4.6.9d: 精度历史数组 (供前端sparkline), 取最近20条非0值
            acc_hist = [float(x) for x in data.get("accuracies", []) if isinstance(x, (int, float)) and x > 0]
            calibrated.append({
                "symbol": symbol,
                "name": data.get("name", ""),
                "accuracy": acc,
                "h20d_accuracy": h20d_acc,
                "mean_accuracy": data.get("mean_accuracy", 0),
                "realized_correct": data.get("realized_correct", 0),
                "h20d_correct": data.get("h20d_correct", 0),
                "h20d_total": data.get("h20d_total", 0),
                "total_predictions": len(data.get("accuracies", [])),
                "accuracy_history": acc_hist[-20:],
                "calibration_status": "retrain_urgent" if acc < thresholds["retrain_urgent"] else ("retrain_planned" if acc < thresholds["retrain_planned"] else "normal"),
                "last_update": "",
                "train_time": model_train_times.get(symbol, "")
            })

    # v4.5.23: 用daily_predict.json中的增强预测精度覆盖训练精度（页面显示实际用于交易的值）
    daily_predict = _read_latest_daily_predict()
    if daily_predict and daily_predict.get("predictions"):
        pred_map = {p.get("symbol", ""): p for p in daily_predict["predictions"]}
        calib_map = {s["symbol"]: s for s in calibrated}
        for sym, p in pred_map.items():
            if sym not in calib_map:
                continue
            enh_acc = p.get("direction_accuracy", p.get("accuracy", None))
            if enh_acc is not None and enh_acc > 0:
                calib_map[sym]["accuracy"] = round(enh_acc, 4)
                # 同步更新校准状态 (v4.6.9d: 用动态阈值)
                calib_map[sym]["calibration_status"] = (
                    "retrain_urgent" if enh_acc < thresholds["retrain_urgent"]
                    else "retrain_planned" if enh_acc < thresholds["retrain_planned"]
                    else "normal"
                )
            h20d = p.get("h20d", {})
            h20d_acc_val = h20d.get("direction_accuracy", None)
            if h20d_acc_val is not None:
                calib_map[sym]["h20d_accuracy"] = round(h20d_acc_val, 4)

    # v4.5.23: 用覆盖后的精度重新计算统计总数
    total = len(calibrated)
    retrain_urgent = sum(1 for s in calibrated if s.get("accuracy", 0) < thresholds["retrain_urgent"])
    retrain_planned = sum(1 for s in calibrated if thresholds["retrain_urgent"] <= s.get("accuracy", 0) < thresholds["retrain_planned"])
    normal = total - retrain_urgent - retrain_planned

    overall = calib.get("overall_stats", {})

    return {
        "summary": {
            "total": total,
            "retrain_urgent": retrain_urgent,
            "retrain_planned": retrain_planned,
            "normal": normal,
        },
        "thresholds": thresholds,
        "overall": {
            "accuracy_rate": overall.get("accuracy_rate", 0),
            "correct_predictions": overall.get("correct_predictions", 0),
            "total_predictions": overall.get("total_predictions", 0),
            "calibration_error": overall.get("calibration_error", 0),
        },
        "stocks": calibrated
    }


# ── 黑天鹅状态 ──

# ── 风险矩阵构建 (v4.6.x: 7因子风险矩阵 + 仓位分解) ──

# ── 方案D: 真实数据源 (北向资金 + Shibor流动性) ──

_FLOW_CACHE = {"ts": 0, "score": None, "note": ""}
_LIQ_CACHE = {"ts": 0, "score": None, "note": ""}
_CACHE_TTL = 6 * 3600  # 6小时缓存, 避免每次请求都打akshare


def _fetch_flow_score() -> tuple:
    """北向资金因子: 真实数据源 + 超时 + 缓存 + 降级

    v4.6.9c (方案D): 接入 akshare stock_hsgt_fund_flow_summary_em
    注意: 2024年后北向实时净买额停止披露(返回0), 因此:
    - 净买额>0 → 按正常映射
    - 净买额=0 但有涨跌家数 → 用市场宽度代理(上涨数占比)
    - 接口失败 → 降级中性 0.30 + 标注
    """
    global _FLOW_CACHE
    now = time.time()
    if _FLOW_CACHE["score"] is not None and now - _FLOW_CACHE["ts"] < _CACHE_TTL:
        return _FLOW_CACHE["score"], _FLOW_CACHE["note"]
    score, note = 0.30, "北向资金 (⚠️接口不可用, 中性占位)"
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is not None and len(df) > 0:
            north = df[df["资金方向"] == "北向"]
            if len(north) > 0:
                net = float(north["成交净买额"].iloc[0] or 0)
                up = float(north["上涨数"].iloc[0] or 0)
                down = float(north["下跌数"].iloc[0] or 0)
                total = up + down
                if abs(net) > 0.01:
                    # 净买额正常披露: 正流入→低风险, 净流出→高风险
                    # 映射: ±80亿 → 0.2~0.8
                    score = max(0.1, min(0.9, 0.5 - net / 200.0))
                    note = f"北向净买额 {net:+.0f}亿"
                elif total > 0:
                    # 2024+ 停披净买额 → 用市场宽度代理 (上涨占比低→高风险)
                    up_ratio = up / total
                    score = max(0.1, min(0.9, 0.9 - up_ratio * 0.8))
                    note = f"北向宽度 涨{int(up)}/{int(total)} ({up_ratio:.0%}) (净买额停披)"
                else:
                    score, note = 0.30, "北向资金 (无数据)"
        _FLOW_CACHE = {"ts": now, "score": score, "note": note}
    except Exception as e:
        note = f"北向资金 (⚠️获取失败: {str(e)[:40]})"
        _FLOW_CACHE = {"ts": now, "score": 0.30, "note": note}
    return score, note


def _fetch_liquidity_score() -> tuple:
    """流动性因子: Shibor隔夜利率 (资金面宽松→低风险, 紧张→高风险)

    v4.6.9c (方案D): 接入 akshare rate_interbank
    映射: 利率<1.5% → 宽松(0.2), 1.5-2.0% → 正常(0.4), >2.5% → 紧张(0.75)
    """
    global _LIQ_CACHE
    now = time.time()
    if _LIQ_CACHE["score"] is not None and now - _LIQ_CACHE["ts"] < _CACHE_TTL:
        return _LIQ_CACHE["score"], _LIQ_CACHE["note"]
    score, note = 0.35, "Shibor (⚠️获取失败, 中性占位)"
    try:
        import akshare as ak
        df = ak.rate_interbank(market="上海银行同业拆借市场", symbol="Shibor人民币", indicator="隔夜")
        if df is not None and len(df) > 0:
            rate = float(df["利率"].iloc[-1])
            if rate <= 1.2:
                score = 0.20
            elif rate <= 1.5:
                score = 0.30
            elif rate <= 2.0:
                score = 0.45
            elif rate <= 2.5:
                score = 0.60
            else:
                score = 0.75
            note = f"Shibor隔夜 {rate:.2f}%"
        _LIQ_CACHE = {"ts": now, "score": score, "note": note}
    except Exception as e:
        note = f"Shibor (⚠️获取失败: {str(e)[:40]})"
        _LIQ_CACHE = {"ts": now, "score": 0.35, "note": note}
    return score, note


def _build_risk_matrix(latest: dict, lppl_section: dict, bs_result: dict,
                       recommended_pos: float, risk_level: str,
                       blackswan_pos: float = None) -> dict:
    """从 black_swan_status.json + adaptive_params.yaml 构建7因子风险矩阵

    v4.6.9 P0: 统一数据源 — breakdown 与 _compute_position_ratio 同源
    (black_swan_status.json 优先, adaptive_params 仅作fallback),
    融合仓位用 morning_decision 同款加权公式, 消除 Dashboard 34% vs 执行55% 分歧。
    """
    adapt = safe_read_yaml(os.path.join(CONFIG_DIR, "adaptive_params.yaml")) or {}
    risk_cfg = adapt.get("risk", {}) or {}
    base_pos = risk_cfg.get("base_position_ratio", 0.6)

    # v4.6.9: black_swan 仓位与 _compute_position_ratio 同源
    # 真相源 = adaptive_params(feedback_controller 动态计算, 执行层同源)
    # black_swan_status.json 仅作fallback(其 position_ratio 可能被 lppl_to_dsl min() 单向压缩残留)
    if blackswan_pos is None:
        _ap_ratio = risk_cfg.get("black_swan_position_ratio", None)
        if isinstance(_ap_ratio, (int, float)) and risk_cfg.get("black_swan_active", False):
            blackswan_pos = float(_ap_ratio)
        else:
            bs_status = safe_read_json(os.path.join(DATA_DIR, "black_swan_status.json")) or {}
            _st_ratio = bs_status.get("position_ratio")
            if bs_status.get("active") and isinstance(_st_ratio, (int, float)):
                blackswan_pos = float(_st_ratio)
            else:
                blackswan_pos = 1.0
    bs_pos = blackswan_pos

    # v4.6.9: LPPL 仓位同源 lppl_section(position_ratio 来自 lppl_to_dsl)
    lppl_pos = lppl_section.get("position_ratio")
    if not isinstance(lppl_pos, (int, float)):
        lppl_pos = risk_cfg.get("lppl_position_ratio", 0.5)
    lppl_pos = float(lppl_pos)

    acc_raw = bs_result.get("accuracy_rate", bs_result.get("calibration_accuracy", 50))
    try:
        acc_rate = float(acc_raw) / 100.0 if float(acc_raw) > 1 else float(acc_raw)
    except (ValueError, TypeError):
        acc_rate = 0.5

    lppl_risk_score = float(lppl_section.get("risk_score", 0) or 0)
    lppl_score = min(1.0, lppl_risk_score / 100.0)

    # v4.6.9: us_bubble 优先读最新 pre_market risk json(盘前实况), fallback adaptive_params
    us_bubble = (latest.get("risk_matrix", {}) or {}).get("us_bubble", None)
    if not us_bubble:
        try:
            _pm_dir = os.path.join(CACHE_DIR, "pre_market")
            if os.path.isdir(_pm_dir):
                _pm_files = sorted([f for f in os.listdir(_pm_dir) if f.endswith("_risk.json")], reverse=True)
                if _pm_files:
                    _pm = safe_read_json(os.path.join(_pm_dir, _pm_files[0])) or {}
                    us_bubble = _pm.get("us_bubble", {}) or {}
        except Exception:
            pass
    if not us_bubble:
        us_bubble = adapt.get("risk", {}).get("us_bubble", {})
    vol_score = min(1.0, float(us_bubble.get("total_score", 30) or 30) / 100.0)

    commod = bs_result.get("commodity", {})
    gold_p = float(commod.get("gold_price", 4000) or 4000)
    oil_p = float(commod.get("oil_price", 70) or 70)
    credit_score = min(1.0, max(0.1, (gold_p / 5000.0 - 0.5) * 2 + (oil_p / 100.0 - 0.5) * 2))

    # v4.6.9c (方案D): 流动性/资金流向接入真实数据源 (北向+Shibor)
    # 带6h缓存 + 超时 + 降级, 失败时中性占位但标注原因
    flow_score, flow_note = _fetch_flow_score()
    liq_score, liq_note = _fetch_liquidity_score()
    us_spread = float(us_bubble.get("us_spread", 0.27) or 0.27)
    corr_score = min(1.0, max(0.1, abs(us_spread - 0.5) * 2))

    urg = lppl_section.get("urgency", "LOW")
    sentiment_map = {"CRITICAL": 0.70, "HIGH": 0.55, "ELEVATED": 0.40, "LOW": 0.25}
    sent_score = sentiment_map.get(urg, 0.25)

    # v4.6.9c (方案B): severity 直映因子 — 黑天鹅严重度直接计入分数
    # 权重从 lppl_bubble 拆分 (0.25 → lppl 0.15 + severity 0.10)
    # 消除 "severity=5 CRITICAL 但分数只有36" 的矛盾
    max_severity = 0
    sev_overview = (latest.get("severity_overview", {}) or {})
    _sev_raw = (sev_overview.get("current_overall_severity")
                or (latest.get("risk_matrix", {}) or {}).get("severity_level")
                or 0)
    try:
        max_severity = int(_sev_raw or 0)
    except (ValueError, TypeError):
        max_severity = 0
    if max_severity > 5:
        max_severity = min(5, max(1, round(max_severity / 2)))
    sev_score = min(1.0, max(0.0, max_severity / 5.0))

    weights = {"severity": 0.10, "lppl_bubble": 0.15, "volatility": 0.15, "credit": 0.15,
               "liquidity": 0.15, "correlation": 0.10, "flow": 0.10, "sentiment": 0.10}

    factors = {
        "severity":     {"score": round(sev_score, 3), "weight": 0.10,
                          "detail": f"黑天鹅severity={max_severity}/5"},
        "lppl_bubble":  {"score": round(lppl_score, 3), "weight": 0.15,
                          "detail": lppl_section.get("bubble_summary", "")[:80] or f"score={lppl_risk_score:.0f}"},
        "volatility":   {"score": round(vol_score, 3), "weight": 0.15,
                          "detail": f"US bubble score={us_bubble.get('total_score', '?')}"},
        "credit":       {"score": round(credit_score, 3), "weight": 0.15,
                          "detail": f"Gold=${gold_p:.0f} Oil=${oil_p:.0f}"},
        "liquidity":    {"score": round(liq_score, 3), "weight": 0.15,
                          "detail": liq_note},
        "correlation":  {"score": round(corr_score, 3), "weight": 0.10,
                          "detail": f"US spread={us_spread:.2f}%"},
        "flow":         {"score": round(flow_score, 3), "weight": 0.10,
                          "detail": flow_note},
        "sentiment":    {"score": round(sent_score, 3), "weight": 0.10,
                          "detail": f"LPPL urgency={urg}"},
    }

    raw_overall = sum(factors[k]["score"] * factors[k]["weight"] for k in factors)
    acc_discount = 0.5 + 0.5 * acc_rate
    weighted_score = raw_overall * acc_discount * 100

    # v4.6.9c (方案C): severity 作分数下限 — 等级与分数永不矛盾
    # floor = severity/5 × 100; score = max(加权分, floor)
    floor_score = max_severity / 5.0 * 100.0
    overall_score = round(max(weighted_score, floor_score), 1)

    recs = []
    if lppl_score > 0.6:
        recs.append(f"LPPL泡沫集中 (score={lppl_score:.0%}), 建议仓位≤{recommended_pos:.0%}")
    if vol_score > 0.5:
        recs.append("波动率偏高, 注意对冲")
    if credit_score > 0.5:
        recs.append("商品价格上涨, 通胀压力")
    recs.append(f"校准准确率: {acc_rate:.0%} — 因子置信度已同步调整")

    return {
        "overall_score": overall_score,
        "risk_level": risk_level,
        "position_ratio": recommended_pos,
        "position_ratio_breakdown": {
            "base": round(base_pos, 2),
            "black_swan": round(bs_pos, 2),
            "lppl": round(lppl_pos, 2),
            "fused": round(recommended_pos, 2),
        },
        "factors": factors,
        "weights": weights,
        "recommendations": recs,
        "calibration": {"accuracy_rate": round(acc_rate, 3)},
    }


def _build_lppl_detailed(latest: dict, lppl_section: dict, lppl_risk: float) -> dict:
    """从 black_swan_status.json + LPPL 报告构建深度诊断"""
    targets = {}
    raw_targets = lppl_section.get("targets", {})
    for name, td in raw_targets.items():
        if not isinstance(td, dict):
            continue
        targets[name] = {
            "strength": td.get("strength", 0),
            "crash_probability": td.get("crash_prob", 0),
            "days_to_critical": td.get("days_to_critical"),
            "critical_date": td.get("critical_date"),
            "regime": td.get("regime", "normal"),
            "r_squared": td.get("r_squared", 0),
            "recommendation": td.get("recommendation", ""),
        }

    try:
        reports_dir = os.path.join(CACHE_DIR, "reports")
        if os.path.exists(reports_dir):
            lppl_files = sorted(
                [f for f in os.listdir(reports_dir) if f.startswith("lppl_daily_") and f.endswith(".json")],
                reverse=True
            )
            if lppl_files:
                with open(os.path.join(reports_dir, lppl_files[0]), "r") as f:
                    content = f.read()
                import re as _re
                for m in _re.finditer(r'\{.*\}', content, _re.DOTALL):
                    try:
                        raw = json.loads(m.group())
                        report_lppl = raw.get("lppl", {})
                        for name, rd in report_lppl.items():
                            if isinstance(rd, dict) and name in targets:
                                rsq = rd.get("r_squared")
                                if rsq is not None and rsq > 0:
                                    targets[name]["r_squared"] = round(rsq, 4)
                                bs = rd.get("bubble_strength")
                                if bs is not None:
                                    targets[name]["strength"] = bs
                                cp = rd.get("crash_probability")
                                if cp is not None:
                                    targets[name]["crash_probability"] = cp
                        break
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    min_days = None
    for t in targets.values():
        d = t.get("days_to_critical")
        if d and d is not None and d < 999:
            if min_days is None or d < min_days:
                min_days = d

    urg = lppl_section.get("urgency", "LOW")
    sector_signals = lppl_section.get("sector_signals", []) or []
    sec_sigs = []
    for s in sector_signals:
        if isinstance(s, dict):
            sec_sigs.append({
                "sector": s.get("sector", ""),
                "direction": s.get("direction", ""),
                "confidence": s.get("confidence", 0),
                "action": s.get("action", ""),
                "stocks": (s.get("stocks") or [])[:5],
            })

    return {
        "overall": {"level": urg, "score": lppl_risk, "min_days": min_days},
        "targets": targets,
        "sector_signals": sec_sigs,
    }


def _build_risk_history(fused_pos: float = None, fused_risk_level: str = "") -> list:
    """从 cache/pre_market/*_risk.json 构建风险时间序列
    
    Args:
        fused_pos: 当前融合建议仓位（来自risk_matrix），用来覆盖最后数据点的position_ratio
        fused_risk_level: 当前融合风险等级
    """
    pre_market_dir = os.path.join(CACHE_DIR, "pre_market")
    if not os.path.exists(pre_market_dir):
        return []

    history = []
    risk_files = sorted(
        [f for f in os.listdir(pre_market_dir) if f.endswith("_risk.json")],
        reverse=False
    )
    for fname in risk_files:
        fpath = os.path.join(pre_market_dir, fname)
        data = safe_read_json(fpath)
        if not data:
            continue
        date_str = fname.replace("_risk.json", "")
        if len(date_str) == 8:
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        lppl_info = data.get("lppl", {})
        risk_score = float(lppl_info.get("risk_score", 0) or 0)
        pos_ratio = float(data.get("position_ratio", data.get("recommended_position_ratio", 1.0)) or 1.0)
        urg = lppl_info.get("urgency", "LOW")

        # v4.6.9: risk_score 缺失时从 severity 映射, 避免历史图全0失真
        if risk_score <= 0:
            _sev = int(data.get("black_swan_max_severity", 0) or 0)
            if _sev >= 5:
                risk_score = 80.0
            elif _sev == 4:
                risk_score = 60.0
            elif _sev == 3:
                risk_score = 40.0
            elif _sev == 2:
                risk_score = 25.0
        # v4.6.9: risk_level 缺失时从 severity 推导
        if urg == "LOW" and not lppl_info.get("risk_score"):
            _sev = int(data.get("black_swan_max_severity", 0) or 0)
            if _sev >= 5:
                urg = "CRITICAL"
            elif _sev == 4:
                urg = "HIGH"
            elif _sev >= 2:
                urg = "ELEVATED"

        history.append({
            "date": date_str,
            "risk_score": round(risk_score, 1),
            "position_ratio": round(pos_ratio, 2),
            "risk_level": urg,
            "event": "",
        })

    try:
        adapt_path = os.path.join(CONFIG_DIR, "adaptive_params.yaml")
        adapt = safe_read_yaml(adapt_path) or {}
        for fb in adapt.get("feedback_history", []):
            if not isinstance(fb, dict):
                continue
            fb_date = fb.get("date", "")
            for h in history:
                if h["date"].startswith(fb_date[:10]) and not h["event"]:
                    lesson = fb.get("lesson", "")[:60]
                    h["event"] = lesson.replace("**", "").split("→")[0].strip()
                    break
    except Exception:
        pass

    # v4.6.9: 用融合仓位+融合等级覆盖最后数据点 (口径统一: 总览/黑天鹅页都显示融合值)
    if fused_pos is not None and fused_pos > 0 and len(history) >= 1:
        old_ratio = history[-1]["position_ratio"]
        history[-1]["position_ratio"] = round(fused_pos, 2)
        if fused_risk_level:
            history[-1]["risk_level"] = fused_risk_level
        if abs(fused_pos - old_ratio) > 0.05:
            # 标注融合调整，让前端可区分
            note = f"融合{fused_risk_level}: {int(fused_pos*100)}% (pre_market: {int(old_ratio*100)}%)"
            if history[-1]["event"]:
                history[-1]["event"] += " | " + note
            else:
                history[-1]["event"] = note

    return history


def get_blackswan() -> Dict[str, Any]:
    """获取黑天鹅监控状态 — v4.5.7 直读memory/black-swan/analysis-*.json"""
    # 直接读取最新黑天鹅分析文件
    bs_dir = os.path.join(os.path.dirname(DATA_DIR), "memory", "black-swan")
    if not os.path.exists(bs_dir):
        bs_dir = os.path.expanduser("~/.openclaw/workspace/memory/black-swan")
    
    result = {
        "active": False, "severity": 0, "events": [],
        "overall": "", "market_prices": {},
        "top_threats": [], "calibration_accuracy": "",
        "position_ratio": 1.0, "last_check": "",
        "source": "memory/black-swan"
    }
    
    if not os.path.exists(bs_dir):
        bs_dir = os.path.expanduser("~/.openclaw/workspace/memory/black-swan")
    if not os.path.exists(bs_dir):
        return result
    
    files = sorted([f for f in os.listdir(bs_dir) if f.startswith("analysis-") and f.endswith(".json")], reverse=True)
    if not files:
        return result
    
    latest = safe_read_json(os.path.join(bs_dir, files[0]))
    if not latest or not isinstance(latest, dict):
        return result
    
    # 提取核心信息
    # P1-5: severity归一化（5分制→1-5, 10分制→按比例映射到1-5）
    def _norm_severity(sev: int) -> int:
        if sev == 0: return 0
        if sev > 5:  # 10分制 → 映射到1-5
            return min(5, max(1, round(sev / 2)))
        return min(5, max(0, sev))

    events_raw = latest.get("events_severity_ge_3", latest.get("events", []))
    # v4.6: agent当前使用 daily_event_scan.events (嵌套dict: critical/high/medium/low)
    if not events_raw or (isinstance(events_raw, dict) and "critical" in events_raw):
        daily_scan = latest.get("daily_event_scan", {}) or {}
        scan_events = daily_scan.get("events", {}) if isinstance(daily_scan, dict) else {}
        if isinstance(scan_events, dict):
            events_raw = []
            for sev_key in ["critical", "high", "medium", "low"]:
                sev_list = scan_events.get(sev_key, [])
                if isinstance(sev_list, list):
                    for ev in sev_list:
                        if isinstance(ev, dict):
                            ev["_severity_label"] = sev_key
                        events_raw.append(ev)
        elif isinstance(scan_events, list):
            events_raw = scan_events
    scan = latest.get("scan_summary", {})
    # P0-fix: 分析文件使用 commodity_prices 而不是 market_prices
    prices = latest.get("commodity_prices", latest.get("market_prices", {}))
    # v4.6.9: analysis 缺 commodity_prices 时从 black_swan_status.json 兜底 (修复总览页商品价格全空)
    if not prices:
        _bs_status = safe_read_json(os.path.join(DATA_DIR, "black_swan_status.json")) or {}
        _bs_commod = _bs_status.get("commodity", {}) or {}
        if _bs_commod:
            prices = {"COMEX黄金": _bs_commod.get("gold_price", 0),
                      "WTI原油": _bs_commod.get("oil_price", 0),
                      "COMEX白银": _bs_commod.get("silver_price", 0)}
        # 再兜底: pre_market risk json (含 commodity + us_bubble)
        if not prices or not any(v for v in prices.values()):
            try:
                _pm_dir = os.path.join(CACHE_DIR, "pre_market")
                if os.path.isdir(_pm_dir):
                    _pm_files = sorted([f for f in os.listdir(_pm_dir) if f.endswith("_risk.json")], reverse=True)
                    if _pm_files:
                        _pm = safe_read_json(os.path.join(_pm_dir, _pm_files[0])) or {}
                        _pm_ub = _pm.get("us_bubble", {}) or {}
                        _pm_comm = _pm.get("commodity", {}) or {}
                        if _pm_ub:
                            prices = {"VIX": {"price": _pm_ub.get("vix", 0)}} if _pm_ub.get("vix") else prices
                        if _pm_comm:
                            prices = {"COMEX黄金": _pm_comm.get("gold_price", prices.get("COMEX黄金", 0)),
                                      "WTI原油": _pm_comm.get("oil_price", prices.get("WTI原油", 0)),
                                      "COMEX白银": _pm_comm.get("silver_price", prices.get("COMEX白银", 0))}
            except Exception:
                pass
    calib = latest.get("calibration_update", {})
    
    # v4.6.x: severity来源优先级 — 1) severity_overview (LLM综合判定) → 2) risk_matrix.severity_level → 3) max event severity
    sev_overview = latest.get("severity_overview", {}) or {}
    rm = latest.get("risk_matrix", {}) or {}
    risk_severity = (sev_overview.get("current_overall_severity")
                     or rm.get("severity_level")
                     or None)
    max_severity = _norm_severity(int(risk_severity)) if risk_severity is not None else 0

    # LLM综合分析文本（优先展示）
    llm_rationale = sev_overview.get("rationale", "")
    overall = rm.get("overall_assessment", scan.get("overall", ""))
    if not overall and scan:
        sevs = scan.get("by_severity", {})
        parts = [f'{v}{k.split("_")[-1]}' for k, v in sorted(sevs.items()) if v]
        overall = f'扫描{scan.get("total_events_detected","?")}事件: ' + ', '.join(parts) if parts else ''
    if llm_rationale:
        overall = llm_rationale[:500]  # LLM原文覆盖auto-generated overall

    events = []
    # 旧格式兼容: events_severity_ge_3 | v4.6: daily_event_scan flat
    events_raw = latest.get("events_severity_ge_3", latest.get("events", []))
    if not events_raw or (isinstance(events_raw, dict) and "critical" in events_raw):
        daily_scan = latest.get("daily_event_scan", {}) or {}
        scan_events = daily_scan.get("events", {}) if isinstance(daily_scan, dict) else {}
        if isinstance(scan_events, dict):
            events_raw = []
            for sev_key in ["critical", "high", "medium", "low"]:
                sev_list = scan_events.get(sev_key, [])
                if isinstance(sev_list, list):
                    for ev in sev_list:
                        if isinstance(ev, dict):
                            ev["_severity_label"] = sev_key
                        events_raw.append(ev)
    for ev in (events_raw if isinstance(events_raw, list) else []):
        sev = _norm_severity(int(ev.get("severity", 0) or 0))
        # v4.6: daily_event_scan 的事件用 _severity_label 映射 severity
        if sev == 0 and ev.get("_severity_label"):
            label_map = {"critical": 5, "high": 4, "medium": 3, "low": 2}
            sev = label_map.get(ev.get("_severity_label"), 0)
        if risk_severity is None and sev > max_severity:
            max_severity = sev
        events.append({
            "title": ev.get("title", ev.get("event_id", ""))[:120],
            "severity": sev,
            "category": ev.get("category", ""),
            "trend": ev.get("trend", ""),
            "source": ev.get("source", "")[:80],
            "published": ev.get("published", ""),
        })

    # v4.6.x: 从 risk_matrix 的三层结构读取威胁和事件 (high_risk / monitor_risk / severe_risk)
    # 旧字段 risk_matrix.top_threats 和 events_severity_ge_3 已废弃
    rm = latest.get("risk_matrix", {}) or {}

    # 事件列表：从三层风险数据构建
    if not events:
        severe = rm.get("severe_risk", None)  # 单条dict或None
        if severe and isinstance(severe, dict):
            events.append({
                "title": severe.get("immediate_title", severe.get("title", "")),
                "severity": 5,
                "category": "severe_risk",
                "trend": ", ".join(severe.get("triggers", [])[:3]),
                "source": "risk_matrix",
                "published": "",
            })
        for ev in (rm.get("high_risk", []) if isinstance(rm.get("high_risk"), list) else []):
            if isinstance(ev, dict):
                events.append({
                    "title": ev.get("title", ""),
                    "severity": 4,
                    "category": "high_risk",
                    "trend": ev.get("alert", "")[:120],
                    "source": "risk_matrix",
                    "published": "",
                })
        for ev in (rm.get("monitor_risk", []) if isinstance(rm.get("monitor_risk"), list) else []):
            if isinstance(ev, dict):
                events.append({
                    "title": ev.get("title", ""),
                    "severity": 2,
                    "category": "monitor_risk",
                    "trend": ev.get("detail", "")[:120],
                    "source": "risk_matrix",
                    "published": "",
                })

    # 商品价格 (兼容 dict 和 float 两种格式)
    market_prices = {}
    for key, label in [("COMEX黄金", "gold"), ("WTI原油", "oil"), ("COMEX白银", "silver")]:
        c = prices.get(key, None)
        if c is None:
            continue
        if isinstance(c, (int, float)):
            market_prices[label] = {"price": c, "change_pct": 0, "high_24h": 0, "low_24h": 0}
        elif isinstance(c, dict):
            market_prices[label] = {
                "price": c.get("price", 0),
                "change_pct": c.get("change_pct", 0),
                "high_24h": c.get("high_24h", 0),
                "low_24h": c.get("low_24h", 0),
            }
    # v4.6.9: 商品价格为0视为数据缺失, 用合理默认值兜底显示(避免总览页显示$0误导)
    _DEFAULTS = {"gold": 4000.0, "oil": 70.0, "silver": 30.0}
    for _k, _dft in _DEFAULTS.items():
        _mp = market_prices.get(_k)
        if isinstance(_mp, dict) and not _mp.get("price"):
            _mp["price"] = _dft
            _mp["_missing"] = True

    vix = prices.get("VIX", None)
    if vix is not None:
        if isinstance(vix, (int, float)):
            market_prices["vix"] = {"price": vix}
        elif isinstance(vix, dict):
            market_prices["vix"] = {"price": vix.get("price", 0)}
    
    # v4.6.x: 威胁列表从三层风险数据构建（high_risk + monitor_risk）
    threats = []
    for ev in (rm.get("high_risk", []) if isinstance(rm.get("high_risk"), list) else []):
        if isinstance(ev, dict):
            threats.append(ev.get("title", "") + ": " + ev.get("alert", "")[:80])
    for ev in (rm.get("monitor_risk", []) if isinstance(rm.get("monitor_risk"), list) else []):
        if isinstance(ev, dict):
            threats.append(ev.get("title", "") + ": " + ev.get("detail", "")[:80])
    if rm.get("severe_risk") and isinstance(rm["severe_risk"], dict):
        s = rm["severe_risk"]
        threats.insert(0, "🔴 " + s.get("immediate_title", s.get("title", ""))[:120])

    # v4.6.9: risk_matrix 场景格式(全大写键如 full_scale_iran_war)时提取为威胁列表
    # analysis 文件的 risk_matrix 是 {scenario_key: {probability, impact, scenario}} 结构
    if not threats:
        _scenario_map = {
            "full_scale_iran_war": "美伊全面战争", "nuclear_incident": "核事故",
            "tit_for_tat_prolonged": "以牙还牙长期化", "diplomatic_breakthrough": "外交突破",
            "china_drawn_into_conflict": "中国卷入冲突", "nato_russia_escalation": "北约-俄升级",
        }
        for _sk, _sl in _scenario_map.items():
            _sc = rm.get(_sk) if isinstance(rm, dict) else None
            if isinstance(_sc, dict):
                _prob = _sc.get("probability", 0)
                _scen = str(_sc.get("scenario", ""))[:100]
                threats.append(f"{_sl} (p={_prob:.0%}): {_scen}")
    
    # v4.5.9: 黑天鹅自动过期衰减 — 文件超过24h则逐日降低severity
    analysis_date_str = latest.get("analysis_date", "")
    if analysis_date_str:
        try:
            from datetime import datetime, timedelta
            adt = datetime.fromisoformat(analysis_date_str.replace("Z", "+00:00"))
            age_hours = (datetime.now().astimezone() - adt.replace(tzinfo=None)).total_seconds() / 3600
            if age_hours > 24:
                days_old = int(age_hours / 24)
                decay = min(days_old, 3)
                old_sev = max_severity
                max_severity = max(0, max_severity - decay)
                if old_sev != max_severity:
                    print(f"[blackswan] 自动衰减: severity {old_sev}→{max_severity} (文件{age_hours:.0f}h前)")
        except Exception:
            pass

    # P0-fix: LPPL 数据补充判定 — 即使 analysis JSON events 为空，LPPL 泡沫检测仍应触发告警
    lppl_section = latest.get("lppl", {})
    # Fallback: 如果analysis JSON无LPPL数据, 从black_swan_status.json读取
    if not lppl_section or not lppl_section.get("targets"):
        _bs_status = safe_read_json(os.path.join(DATA_DIR, "black_swan_status.json")) or {}
        _bs_lppl = _bs_status.get("lppl", {})
        if _bs_lppl and _bs_lppl.get("targets"):
            lppl_section = _bs_lppl
    if max_severity == 0 and lppl_section:
        lppl_risk = lppl_section.get("risk_score", 0) or 0
        lppl_urgency = lppl_section.get("urgency", "")
        if lppl_risk >= 70 or lppl_urgency == "CRITICAL":
            max_severity = 5
        elif lppl_risk >= 50 or lppl_urgency == "HIGH":
            max_severity = 4
        elif lppl_risk >= 25 or lppl_urgency == "ELEVATED":
            max_severity = 3
    # v4.6.x: 融合风险等级和仓位建议
    lppl_risk = lppl_section.get("risk_score", 0) or 0
    lppl_urgency = lppl_section.get("urgency", "")
    lppl_pos_ratio = (rm.get("lppl_position_ratio", None) or lppl_section.get("position_ratio", None))
    lppl_overall_level = (lppl_section.get("overall_risk", {}) or {}).get("level", "")

    blackswan_pos = _compute_position_ratio(max_severity)
    lppl_p = float(lppl_pos_ratio) if lppl_pos_ratio else 1.0
    # v4.6.x: 仓位融合 — 对齐 morning_decision.py 同款加权公式
    # morning_decision: fused = max(0.25, bs*0.6 + lppl*0.4); position = min(bs, fused)
    # 消除 Dashboard 显示(旧均值法34%) vs 执行层(加权法55%) 分歧
    if max_severity >= 3:
        fused = max(0.25, blackswan_pos * 0.6 + lppl_p * 0.4)
        recommended_pos = round(min(blackswan_pos, fused), 4)
    else:
        recommended_pos = round(min(blackswan_pos, lppl_p), 4)

    # 融合风险等级
    risk_level = "NORMAL"
    if max_severity >= 5 or lppl_urgency == "CRITICAL":
        risk_level = "CRITICAL"
    elif max_severity >= 4 or lppl_urgency == "HIGH":
        risk_level = "HIGH"
    elif max_severity >= 2 or lppl_urgency == "ELEVATED":
        risk_level = "ELEVATED"

    # 仓位依据: LLM原文优先, 回退到auto-generated
    rationale = llm_rationale[:300] if llm_rationale else ""
    if not rationale:
        parts = []
        if max_severity >= 3:
            parts.append(f"黑天鹅severity={max_severity}→仓位{blackswan_pos:.0%}")
        if lppl_urgency and lppl_urgency != "NORMAL":
            parts.append(f"LPPL={lppl_urgency}(score={lppl_risk})→仓位{lppl_p:.0%}")
        rationale = " | ".join(parts) if parts else "无风险压制"
    else:
       rationale += f" | 建议仓位: {recommended_pos:.0%} | S{max_severity}+LPPL={lppl_urgency}"

    # v4.6.x: 风险矩阵 + LPPL深度 + 风险历史
    risk_matrix = _build_risk_matrix(latest, lppl_section, safe_read_json(os.path.join(DATA_DIR, "black_swan_status.json")) or {},
                                     recommended_pos, risk_level, blackswan_pos=blackswan_pos)
    lppl_detailed = _build_lppl_detailed(latest, lppl_section, lppl_risk)
    risk_history = _build_risk_history(fused_pos=recommended_pos, fused_risk_level=risk_level)

    return {
        "active": max_severity >= 3,
        "severity": max_severity,
        "events": events[:15],
        "overall": overall[:500] if overall else "",
        "market_prices": market_prices,
        "top_threats": threats[:10],
        "calibration_accuracy": calib.get("overall_accuracy", ""),
        "position_ratio": blackswan_pos,
        "last_check": latest.get("analysis_date", files[0].replace("analysis-", "").replace(".json", "")),
        "source": f"memory/black-swan/{files[0]}",
        "lppl": latest.get("lppl", {}),
        "lppl_position_ratio": lppl_pos_ratio,
        "lppl_overall_level": lppl_overall_level,  # v4.6.x: LPPL全市场加权风险等级
        # v4.6.x: 融合风险字段
        "risk_level": risk_level,
        "recommended_position": recommended_pos,
        "position_rationale": rationale,
        # v4.6.x: 风险评估与仓位管理字段
        "risk_matrix": risk_matrix,
        "lppl_detailed": lppl_detailed,
        "risk_history": risk_history,
    }


# ── 模拟持仓 ──

def _load_stock_names() -> dict:
    """从master_stock_pool.yaml加载股票代码→名称映射"""
    try:
        import yaml
        path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                pool = yaml.safe_load(f)
            stocks = pool.get("master_pool", [])
            return {s.get("symbol", ""): s.get("name", "") for s in stocks}
    except Exception:
        pass
    return {}


def get_paper_trader_sync() -> Dict[str, Any]:
    """v4.6.9h.2: 模拟交易全量数据 (同步版, 供 /api/full 使用)

    与 server.py /api/paper-trader 返回同结构: positions/history/ledger/performance/summary
    消除前端对 /api/paper-trader 异步补丁的依赖(竞态+静默失败)
    """
    db_path = os.path.join(DATA_DIR, "paper_trading.db")
    result = {"positions": [], "history": [], "performance": {}, "ledger": {}, "summary": {}}
    if not os.path.exists(db_path):
        return result
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            name_map = _load_stock_names()
            # 止损值: 从缓存读, fallback计算
            def _fallback_sl(code):
                try:
                    return round(-0.08, 4)
                except Exception:
                    return -0.08
            stop_loss_map = {}
            try:
                from scripts.paper_trader import get_stop_loss_pct
                for c in [r["stock_code"] for r in conn.execute("SELECT stock_code FROM positions").fetchall()]:
                    try:
                        stop_loss_map[c] = get_stop_loss_pct(c)
                    except Exception:
                        stop_loss_map[c] = _fallback_sl(c)
            except Exception:
                for c in [r["stock_code"] for r in conn.execute("SELECT stock_code FROM positions").fetchall()]:
                    stop_loss_map[c] = _fallback_sl(c)

            positions = [
                {"symbol": r["stock_code"],
                 "name": name_map.get(r["stock_code"], ""),
                 "quantity": r["quantity"],
                 "avg_cost": round(r["avg_cost"], 2),
                 "current_price": round(r["current_price"], 2),
                 "market_value": round(r["quantity"] * r["current_price"], 2),
                 "cost_value": round(r["quantity"] * r["avg_cost"], 2),
                 "pnl": round(r["quantity"] * (r["current_price"] - r["avg_cost"]), 2),
                 "pnl_pct": round((r["current_price"] / r["avg_cost"] - 1) * 100, 2) if r["avg_cost"] else 0,
                 "stop_loss": stop_loss_map.get(r["stock_code"], _fallback_sl(r["stock_code"]))}
                for r in conn.execute("SELECT * FROM positions").fetchall()]
            history = [
                {"ts": r["timestamp"], "symbol": r["stock_code"], "action": r["action"],
                 "price": round(r["price"], 2), "qty": r["quantity"],
                 "amount": round(r["amount"], 2),
                 "commission": round(r["commission"], 2) if r["commission"] else 0,
                 "stamp_tax": round(r["stamp_tax"], 2) if r["stamp_tax"] else 0,
                 "total_fee": round(r["total_fee"], 2) if r["total_fee"] else 0,
                 "volume_ratio_pct": round(r["volume_ratio_pct"], 2) if r["volume_ratio_pct"] else 0,
                 "is_valid_for_metrics": r["is_valid_for_metrics"] if "is_valid_for_metrics" in r.keys() else 1,
                 "quality_flag": r["quality_flag"] if "quality_flag" in r.keys() else "valid",
                 "reason": r["reason"]}
                for r in conn.execute("SELECT * FROM trade_history ORDER BY id DESC LIMIT 50").fetchall()]
            ledger = {}
            for r in conn.execute("SELECT key, value FROM ledger").fetchall():
                try:
                    ledger[r["key"]] = float(r["value"])
                except (ValueError, TypeError):
                    ledger[r["key"]] = r["value"]
            performance = {}
            for r in conn.execute("SELECT key, value FROM performance_metrics").fetchall():
                try:
                    performance[r["key"]] = float(r["value"])
                except (ValueError, TypeError):
                    performance[r["key"]] = r["value"]
            result["positions"] = positions
            result["history"] = history
            result["ledger"] = ledger
            result["performance"] = performance

            initial = result["ledger"].get("initial_capital", 1000000)
            cash = result["ledger"].get("current_cash", 0)
            market_val = sum(p["market_value"] for p in result["positions"])
            total_val = cash + market_val
            result["summary"] = {
                "initial_capital": initial,
                "cash": round(cash, 2),
                "market_value": round(market_val, 2),
                "total_value": round(total_val, 2),
                "total_pnl": round(total_val - initial, 2),
                "total_pnl_pct": round((total_val / initial - 1) * 100, 2),
                "position_count": len(positions),
                # 兼容旧字段名(前端旧版)
                "total_positions": len(positions),
                "pnl_pct": round((total_val / initial - 1) * 100, 2) if initial else 0,
                # 无后台刷新(同步读DB缓存价), 标记价格来源
                "price_refreshed_at": None,
                "price_refresh_age_hours": -1,
            }
        finally:
            conn.close()
    except Exception as e:
        print(f"[data_adapter] get_paper_trader_sync error: {e}")
    return result


def get_portfolio() -> Dict[str, Any]:
    """获取模拟持仓（优先 JSON，回退 SQLite）"""
    name_map = _load_stock_names()

    # v4.6.3: paper_trading.db is the execution ledger source of truth.  Prefer it
    # even when stale JSON portfolio snapshots exist, so /api/full and
    # /api/paper-trader display the same positions and total_value.
    try:
        import sqlite3
        db_path = os.path.join(DATA_DIR, "paper_trading.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM positions").fetchall()
            if rows:
                positions = []
                for r in rows:
                    qty = r["quantity"]
                    avg = r["avg_cost"]
                    cur = r["current_price"]
                    positions.append({
                        "symbol": r["stock_code"],
                        "name": name_map.get(r["stock_code"], ""),
                        "market": r["market"] if "market" in r.keys() else "A",
                        "quantity": qty,
                        "shares": qty,
                        "avg_cost": avg,
                        "current_price": cur,
                        "market_value": qty * cur,
                        "pnl": qty * (cur - avg),
                        "pnl_pct": round((cur / avg - 1) * 100, 2) if avg else 0,
                    })
                cash_row = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
                cash = float(cash_row[0]) if cash_row else 100000.0
                initial_row = conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()
                initial = float(initial_row[0]) if initial_row else 1000000.0
                market_value = sum(p["market_value"] for p in positions)
                total_value = cash + market_value
                conn.close()
                return {
                    "total_positions": len(positions),
                    "total_value": total_value,
                    "cash": cash,
                    "pnl": total_value - initial,
                    "positions": positions,
                }
            conn.close()
    except Exception as e:
        print(f"[data_adapter] SQLite portfolio source error: {e}")

    portfolio = safe_read_json(os.path.join(DATA_DIR, "simulation_portfolio.json")) or {}
    positions = portfolio.get("positions", portfolio.get("holdings", []))
    if isinstance(positions, dict):
        positions = [{"symbol": k, **v} for k, v in positions.items()]
    for p in positions:
        sym = p.get("symbol", p.get("code", ""))
        if "name" not in p or not p["name"]:
            p["name"] = name_map.get(sym, "")
        # 统一 quantity/shares 字段
        if "quantity" not in p and "shares" in p:
            p["quantity"] = p["shares"]
        elif "shares" not in p and "quantity" in p:
            p["shares"] = p["quantity"]
    
    # 如果 JSON 文件持仓为空，从 paper_trading.db 读取真实数据
    if not positions:
        try:
            import sqlite3
            db_path = os.path.join(DATA_DIR, "paper_trading.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM positions").fetchall()
                if rows:
                    positions = []
                    cols = [desc[0] for desc in conn.execute("SELECT * FROM positions LIMIT 0").description]
                    for r in rows:
                        qty = r["quantity"]
                        avg = r["avg_cost"]
                        cur = r["current_price"]
                        market = r["market"] if "market" in cols else "A"
                        positions.append({
                            "symbol": r["stock_code"],
                            "name": name_map.get(r["stock_code"], ""),
                            "market": market,
                            "quantity": qty,
                            "avg_cost": avg,
                            "current_price": cur,
                            "market_value": qty * cur,
                            "pnl": qty * (cur - avg),
                            "pnl_pct": round((cur / avg - 1) * 100, 2) if avg else 0
                        })
                    # 计算总价值
                    market_value = sum(p["market_value"] for p in positions)
                    cash_row = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
                    cash = float(cash_row[0]) if cash_row else 100000.0
                    total_value = cash + market_value
                    initial_row = conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()
                    initial = float(initial_row[0]) if initial_row else 1000000.0
                    conn.close()
                    return {
                        "total_positions": len(positions),
                        "total_value": total_value,
                        "cash": cash,
                        "pnl": total_value - initial,
                        "positions": positions
                    }
                conn.close()
        except Exception as e:
            print(f"[data_adapter] SQLite fallback error: {e}")
    
    # 即使JSON有持仓，也从SQLite补充真实的cash/initial_capital
    cash = portfolio.get("cash", portfolio.get("available_cash", 0))
    initial = portfolio.get("initial_capital", 1000000.0)
    try:
        import sqlite3
        db_path = os.path.join(DATA_DIR, "paper_trading.db")
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            cash_row = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
            if cash_row:
                cash = float(cash_row[0])
            init_row = conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()
            if init_row:
                initial = float(init_row[0])
            conn.close()
    except Exception as e:
        print(f"[data_adapter] SQLite cash supplement error: {e}")
    
    # 计算持仓市值 + v4.5.9: 统一规范化持仓字段(JSON/SQLite来源字段名不一致)
    market_value = 0
    for p in positions:
        qty = p.get("quantity", p.get("shares", 0))
        avg = p.get("avg_cost", p.get("avg_price", 0))
        cur = p.get("current_price", 0)
        if "market_value" not in p or not p.get("market_value"):
            p["market_value"] = qty * cur
        if "avg_cost" not in p:
            p["avg_cost"] = avg
        if "pnl" not in p:
            p["pnl"] = qty * (cur - avg)
        if "pnl_pct" not in p:
            p["pnl_pct"] = round((cur / avg - 1) * 100, 2) if avg else 0
        market_value += p["market_value"]
    total_value = cash + market_value
    
    return {
        "total_positions": len(positions),
        "total_value": total_value,
        "cash": cash,
        "pnl": total_value - initial,
        "positions": positions
    }


# ── 交易记录 ──

def get_trading_ledger() -> Dict[str, Any]:
    """获取交易记录
    v4.6.9 fix: 兼容 paper_trading_ledger.json 实际字段 trade_history
    (原实现只读 trades/history, 与实际文件字段不匹配导致永远返回空)
    """
    ledger = safe_read_json(os.path.join(DATA_DIR, "paper_trading_ledger.json")) or {}
    trades = ledger.get("trades", ledger.get("history", []))
    # v4.6.9: 实际字段名是 trade_history
    if not trades:
        trades = ledger.get("trade_history", [])
    if isinstance(trades, dict):
        trades = trades.get("records", [])
    return {
        "total": len(trades),
        "trades": trades[-50:] if len(trades) > 50 else trades  # 最近50条
    }


# ── 自适应参数 ──

def get_adaptive_params() -> Dict[str, Any]:
    """获取自适应参数"""
    adaptive = safe_read_yaml(os.path.join(CONFIG_DIR, "adaptive_params.yaml")) or {}
    return adaptive


# ── 模型训练摘要 ──

def get_training_summary() -> Dict[str, Any]:
    """获取模型训练摘要"""
    summary = safe_read_json(os.path.join(MODELS_DIR, "training_results.json")) or {}
    return summary


# ── Cron任务与进度 ──

def get_progress() -> Dict[str, Any]:
    """获取任务进度 (合并 task_progress.json + cache/progress/*.json)"""
    # 旧数据源
    result = safe_read_json(os.path.join(DATA_DIR, "task_progress.json")) or {"tasks": [], "last_updated": ""}
    old_tasks = {t["task_id"]: t for t in result.get("tasks", [])}
    
    # 新数据源: cache/progress/*.json (ProgressTracker格式)
    import glob
    progress_dir = os.path.join(PROJECT_ROOT, "cache", "progress")
    if os.path.isdir(progress_dir):
        now_ts = time.time()
        for f in sorted(glob.glob(os.path.join(progress_dir, "*.json")),
                       key=os.path.getmtime, reverse=True):
            try:
                with open(f, "r") as fh:
                    data = json.load(fh)
                # 自动修复：running超过60分钟 → 仅内存中标记为crashed，不写回文件
                if data.get("status") == "running":
                    file_age = now_ts - os.path.getmtime(f)
                    if file_age > 3600:
                        # 仅在本次返回数据中标记，不破坏原始进度文件
                        # 如果进程实际仍在运行，下次写入会覆盖回running
                        data = dict(data)  # shallow copy to avoid mutating cached dict
                        data["status"] = "crashed"
                        data["message"] = f"超时(运行{file_age/60:.0f}min)"
                tid = data.get("task_id", "unknown")
                if tid not in old_tasks:
                    # 转换成与旧格式兼容的字段
                    old_tasks[tid] = {
                        "task_id": tid,
                        "task_name": data.get("task_name", "?"),
                        "status": data.get("status", "?"),
                        "message": data.get("message", ""),
                        "started_at": data.get("started_at", ""),
                        "completed_at": data.get("completed_at", data.get("updated_at", "")),
                        "progress_pct": round(
                            (data.get("progress", {}).get("step", 0) /
                             max(data.get("progress", {}).get("total", 1), 1)) * 100, 1
                        ),
                    }
            except Exception:
                pass
    
    # 合并且排序
    all_tasks = sorted(old_tasks.values(),
                      key=lambda t: t.get("started_at", ""), reverse=True)
    result["tasks"] = all_tasks
    result["total_tasks"] = len(all_tasks)
    result["last_updated"] = all_tasks[0].get("started_at", "") if all_tasks else ""
    result["source_files"] = ["task_progress.json", "cache/progress/*.json"]
    return result


def get_pool_history() -> Dict[str, Any]:
    """股票池变更历史: 比对archive中历史版本"""
    import yaml
    archive_dir = os.path.join(PROJECT_ROOT, "config", "archive")
    if not os.path.exists(archive_dir):
        return {"history": []}
    
    files = sorted([f for f in os.listdir(archive_dir) 
                    if f.startswith("master_stock_pool.yaml.") and f.endswith(".yaml" + f[len("master_stock_pool.yaml"):])],
                   reverse=True)
    if not files:
        return {"history": []}
    
    # Also read current pool
    current_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    versions = []
    
    for fname in files:
        ts_str = fname.replace("master_stock_pool.yaml.", "")
        try:
            ts = datetime.strptime(ts_str, "%Y%m%d_%H%M")
        except:
            ts = None
        fpath = os.path.join(archive_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                pool = yaml.safe_load(f) or {}
            stocks = pool.get("master_pool", [])
            syms = {s.get("symbol", ""): s.get("name", "") for s in stocks}
            tiers = {}
            for s in stocks:
                t = s.get("tier", "")
                if t not in tiers:
                    tiers[t] = 0
                tiers[t] += 1
            versions.append({
                "timestamp": ts_str,
                "date": ts.strftime("%m-%d %H:%M") if ts else ts_str,
                "total": len(stocks),
                "symbols": syms,
                "tiers": tiers,
            })
        except:
            continue
    
    # Compute diffs between consecutive versions
    changes = []
    for i in range(len(versions) - 1):
        older = versions[i + 1]
        newer = versions[i]
        old_syms = set(older["symbols"].keys())
        new_syms = set(newer["symbols"].keys())
        added = new_syms - old_syms
        removed = old_syms - new_syms
        change = {
            "old_date": older["date"],
            "new_date": newer["date"],
            "old_total": older["total"],
            "new_total": newer["total"],
            "added": [(s, newer["symbols"].get(s, "")) for s in sorted(added)],
            "removed": [(s, older["symbols"].get(s, "")) for s in sorted(removed)],
        }
        if added or removed:
            changes.append(change)
    
    return {"versions": versions, "changes": changes}


def get_accuracy_trend() -> Dict[str, Any]:
    """从daily_records提取每日精度趋势
    v4.5.7: 分离hold/非hold信号，暴露真实预测兑现精度
    """
    calib = safe_read_json(os.path.join(CONFIDENCE_DIR, "prediction_calibration.json")) or {}
    records = calib.get("daily_records", [])
    
    daily = []
    for r in records:
        stocks = r.get("stocks", [])
        # 整体（含hold）
        total_all = sum(1 for s in stocks if s.get("realized_checked"))
        correct_all = sum(1 for s in stocks if s.get("realized_correct"))
        # 纯非hold信号
        non_hold = [s for s in stocks if s.get("realized_checked") and s.get("signal", "hold") != "hold"]
        total_nh = len(non_hold)
        correct_nh = sum(1 for s in non_hold if s.get("realized_correct"))
        # 已验证兑现的（有realized_return的实际盈亏）
        verified = [s for s in non_hold if s.get("realized_return") is not None]
        total_ver = len(verified)
        correct_ver = sum(1 for s in verified if s.get("realized_correct"))
        
        daily.append({
            "date": r.get("date", "?"),
            "total": total_all,
            "correct": correct_all,
            "accuracy": round(correct_all / total_all, 4) if total_all > 0 else 0,
            "hold_signals": total_all - sum(1 for s in stocks if s.get("signal", "hold") != "hold"),
            "non_hold": {
                "total": total_nh,
                "correct": correct_nh,
                "accuracy": round(correct_nh / total_nh, 4) if total_nh > 0 else None,
            },
            "verified": {
                "total": total_ver,
                "correct": correct_ver,
                "accuracy": round(correct_ver / total_ver, 4) if total_ver > 0 else None,
            },
        })
    
    # 从stock_accuracy取每只标的的精度历史
    stock_acc = calib.get("stock_accuracy", {})
    acc_profiles = {}
    for sym, sdata in stock_acc.items():
        if isinstance(sdata, dict):
            accs = sdata.get("accuracies", [])
            if accs:
                acc_profiles[sym] = {
                    "name": sdata.get("name", ""),
                    "last": sdata.get("last_accuracy", 0),
                    "mean": sdata.get("mean_accuracy", sum(accs)/len(accs)),
                    "count": len(accs),
                }
    
    return {"daily_records": daily, "stock_profiles": acc_profiles}


def get_cron_status() -> List[Dict[str, Any]]:
    """获取Cron任务状态 — 从cron/jobs.json直接读取，刷新本地缓存"""
    # 先尝试刷新缓存（每5分钟最多一次）
    _refresh_cron_cache()
    # 读缓存
    status_file = os.path.join(DATA_DIR, "cron_status.json")
    data = safe_read_json(status_file)
    return data.get("jobs", data.get("cron_jobs", [])) if data else []


# ── cron缓存刷新(每5分钟最多一次) ──
_last_cron_refresh = 0.0

def _refresh_cron_cache():
    """从cron/jobs.json同步到data/cron_status.json（非OpenClaw环境则跳过）

    v4.6.9 fix: L2 使用完整路径+显式PATH, 修复 LaunchAgent 环境下
    PATH 不含 /opt/homebrew/bin 导致 openclaw 命令静默失败、缓存停更的问题。
    """
    global _last_cron_refresh
    now = time.time()
    if now - _last_cron_refresh < 300:
        return
    _last_cron_refresh = now
    
    # L1: 直接读cron/jobs.json（最快）
    cron_jobs_path = os.path.expanduser("~/.openclaw/cron/jobs.json")
    if os.path.exists(cron_jobs_path):
        try:
            cron_data = safe_read_json(cron_jobs_path)
            if cron_data and cron_data.get("jobs"):
                status_path = os.path.join(DATA_DIR, "cron_status.json")
                with open(status_path, "w", encoding="utf-8") as f:
                    json.dump(cron_data, f, ensure_ascii=False, indent=2)
                return
        except Exception:
            pass
    
    # L2: openclaw CLI (完整路径 + 显式PATH, 与 _build_pipeline_tasks 保持一致)
    try:
        import subprocess
        _OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
        _env = os.environ.copy()
        _env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + _env.get("PATH", "")
        result = subprocess.run([_OPENCLAW_BIN, "cron", "list", "--json"],
                                 capture_output=True, text=True, timeout=15, env=_env)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if data and data.get("jobs"):
                status_path = os.path.join(DATA_DIR, "cron_status.json")
                with open(status_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def trigger_cron_job(task_id: str) -> dict:
    """v4.6.x: 通过OpenClaw CLI触发单个cron任务

    Args:
        task_id: logical_id (如 'batch_predict', 'dsl_backup') 或 cron job UUID
    Returns:
        {"triggered": bool, "job_id": str, "job_name": str, "error": str}
    """
    # Step 1: 加载cron jobs
    cron_path = os.path.expanduser("~/.openclaw/cron/jobs.json")
    if not os.path.exists(cron_path):
        return {"triggered": False, "error": "cron jobs文件不存在"}

    try:
        with open(cron_path, "r") as f:
            cron_data = json.load(f)
    except Exception as e:
        return {"triggered": False, "error": f"读取cron jobs失败: {e}"}

    jobs = cron_data.get("jobs", [])

    # Step 2: 按task_id查找（先按UUID精确匹配，再按logical_id映射）
    found = None
    for j in jobs:
        if j.get("id") == task_id:
            found = j
            break

    if not found:
        # 按 _CRON_NAME_TO_LOGICAL 反向映射
        for j in jobs:
            name = j.get("name", "")
            base = name.split("(")[0].strip() if "(" in name else name
            mapped = _CRON_NAME_TO_LOGICAL.get(base, _CRON_NAME_TO_LOGICAL.get(name, ""))
            if mapped == task_id:
                found = j
                break

    if not found:
        return {"triggered": False, "error": f"未找到任务: {task_id}"}

    # Step 3: 检查是否enabled(disabled的也可手动触发)
    job_id = found.get("id")
    job_name = found.get("name", task_id)

    # Step 4: 调用openclaw cron run
    try:
        import subprocess
        result = subprocess.run(
            ["openclaw", "cron", "run", job_id, "--timeout", "300000"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            err = result.stderr[:200] if result.stderr else "exit code " + str(result.returncode)
            return {"triggered": False, "job_id": job_id, "job_name": job_name,
                    "error": f"CLI失败: {err}"}
        return {"triggered": True, "job_id": job_id, "job_name": job_name,
                "message": f"任务已触发: {job_name}"}
    except subprocess.TimeoutExpired:
        return {"triggered": False, "job_id": job_id, "job_name": job_name,
                "error": "CLI调用超时(10s)"}
    except FileNotFoundError:
        return {"triggered": False, "job_id": job_id, "job_name": job_name,
                "error": "openclaw CLI不可用"}
    except Exception as e:
        return {"triggered": False, "job_id": job_id, "job_name": job_name,
                "error": str(e)[:200]}


# ── 自适应阈值引擎 ──

def get_threshold_engine() -> Dict[str, Any]:
    """获取动态阈值引擎状态"""
    adaptive = safe_read_yaml(os.path.join(CONFIG_DIR, "adaptive_params.yaml")) or {}
    threshold = adaptive.get("threshold_engine", adaptive.get("L1_market_adjustment", {}))
    if isinstance(threshold, dict):
        return threshold
    return {}


# ── Pipeline Timeline ──
# v4.5.7: 从 cron/jobs.json 自动构建，不再硬编码

# P1-2: 校准重训阈值（可从adaptive_params.yaml覆盖）
_RETRAIN_URGENT = 0.45
_RETRAIN_PLANNED = 0.55
def _reload_calib_thresholds():
    """从adaptive_params.yaml加载校准阈值"""
    global _RETRAIN_URGENT, _RETRAIN_PLANNED
    try:
        path = os.path.join(CONFIG_DIR, "adaptive_params.yaml")
        if os.path.exists(path):
            import yaml
            with open(path, encoding="utf-8") as f:
                ap = yaml.safe_load(f) or {}
            calib = ap.get("calibration", {}) or {}
            _RETRAIN_URGENT = float(calib.get("retrain_urgent", _RETRAIN_URGENT))
            _RETRAIN_PLANNED = float(calib.get("retrain_planned", _RETRAIN_PLANNED))
    except Exception:
        pass
_reload_calib_thresholds()

_ICON_MAP = {
    "备份": "💾", "训练": "🧠", "预测": "🎯", "自省": "🪞", "反思": "🪞",
    "盘前": "📡", "决策": "⚖️", "交易": "📈", "信号": "📊", "日报": "📝",
    "健康": "🏥", "预案": "📐", "黑天鹅": "🦢", "监控": "📊",
}
_PHASE_RULES = [
    (0, 8, "night"), (8, 9.5, "premarket"), (9.5, 15, "intraday"),
    (15, 18, "postmarket"), (18, 24, "evening"),
]

def _get_phase(hour):
    for s, e, p in _PHASE_RULES:
        if s <= hour < e: return p
    return "evening"
def _get_icon(n):
    for kw, icon in _ICON_MAP.items():
        if kw in n: return icon
    return "📋"
# 非DSL核心任务（不显示在pipeline timeline中）
_NON_DSL_TASKS = {
    "深圳房价每周预测", "Alpha-Quant自迭代", "Cron Watchdog",
    "consciousness-reflect", "consciousness-skill-detect",  # OpenClaw系统级意识模块，非DSL任务
}

# Cron任务名称 → 逻辑ID映射（用于查找报告）
_CRON_NAME_TO_LOGICAL = {
    "DSL系统备份": "dsl_backup",
    "预测模型分批训练": "batch_train",
    "备份状态报告": "backup_report",
    "个股分批预测": "batch_predict_1700",
    "个股预测盘前刷新": "batch_predict_0900",  # v4.6: 09:00盘前刷新(改名后)
    "DSL盘前数据刷新": "pre_market_refresh",
    "A股盘前决策(09:20)": "pre_market_decision",
    "A股交易执行(09:30)": "trade_execution_0930",
    "DSL盘中信号监控 09:45": "intraday_monitor_0945",
    "DSL盘中信号监控 10:30": "intraday_monitor_1030",
    "DSL盘中信号监控 11:00": "intraday_monitor_1100",
    "DSL盘中信号监控 13:30": "intraday_monitor_1330",
    "DSL盘中信号监控 14:30": "intraday_monitor_1430",
    # v4.6.3: 括号命名别名（取代旧空格命名）
    "DSL盘中信号监控(09:45)": "intraday_monitor_0945",
    "DSL盘中信号监控(10:30)": "intraday_monitor_1030",
    "DSL盘中信号监控(11:00)": "intraday_monitor_1100",
    "DSL盘中信号监控(13:30)": "intraday_monitor_1330",
    "DSL盘中信号监控(14:30)": "intraday_monitor_1430",
    # 止损/止盈每时段独立 logical_id，避免 progress_map 折叠
    "DSL止损/止盈监控(09:50)": "stop_loss_monitor_0950",
    "DSL止损/止盈监控(10:35)": "stop_loss_monitor_1035",
    "DSL止损/止盈监控(11:05)": "stop_loss_monitor_1105",
    "DSL止损/止盈监控(13:35)": "stop_loss_monitor_1335",
    "DSL止损/止盈监控(14:35)": "stop_loss_monitor_1435",
    "A股收盘日报": "closing_daily_report",
    "DSL健康检查（修复版）": "health_check",
    "DSL健康检查(15:40)": "health_check",  # v4.6.3: 当前活跃任务名
    "自我反思触发": "self_reflection",
    "自我反思(20:00)": "self_reflection",  # v4.6.3: 当前活跃任务名
    "黑天鹅每日复盘": "blackswan_review",
    "A股盘前交易预案": "pre_market_plan",
    "A股盘前交易预案(evening)": "pre_market_plan",
    "模型周度复盘": "weekly_model_review",
    "周度复盘": "weekly_review",
    "股票池刷新": "weekly_pool_refresh",
    "DSL备份到OneDrive(02:00)": "dsl_backup",   # v4.5.17 harness review: 新增映射
    "DSL备份+Git同步": "dsl_backup",             # v4.6.x: 当前备份cron任务名(无后缀)
    "DSL备份+Git同步(02:10)": "dsl_backup",      # v4.6.x: 当前备份cron任务名(含时间)
    "DSL Git同步(03:30)": "dsl_backup",         # Git同步归类备份
    "Cron Watchdog": "_watchdog",                # v4.5.17: watchdog内部任务
    # v4.0 黑天鹅优化新增
    "黑天鹅预测自动验证(Mon+Thu)": "auto_verify",  # v4.6.3: 当前活跃任务名(改名自晚间)
    "LPPL泡沫检测(晨间)": "lppl_morning",
    "LPPL泡沫检测(08:10)": "lppl_morning",     # v4.6.x: 晨间泡沫检测(chron名含时间)
    "LPPL全量检测(周中)": "lppl_full",
    "黑天鹅预测自动验证(晚间)": "auto_verify",
    "DSL模型连接池预热(08:50)": "pool_warmup",  # v4.6.x: 模型连接池预热
}

def audit_cron_name_mappings() -> list:
    """v4.6.x: 审计 _CRON_NAME_TO_LOGICAL 映射是否缺少实际cron任务名
    返回所有缺少映射的cron任务列表，供 Dashboard/自动验证使用。
    """
    missing = []
    p = os.path.expanduser("~/.openclaw/cron/jobs.json")
    if not os.path.exists(p):
        return missing
    try:
        cron_data = json.load(open(p, encoding="utf-8"))
        for j in cron_data.get("jobs", []):
            name = j.get("name", "")
            if not j.get("enabled", True):
                continue
            if name in _NON_DSL_TASKS:
                continue
            # 检查是否有映射
            base = name.split("(")[0].strip() if "(" in name else name
            mapped = _CRON_NAME_TO_LOGICAL.get(base) or _CRON_NAME_TO_LOGICAL.get(name)
            if not mapped:
                missing.append({
                    "name": name,
                    "id": j.get("id", ""),
                    "schedule": j.get("schedule", {}).get("expr", ""),
                    "suggestion": f'添加 "{name}" 或 "{base}" → "{base.lower().replace(" ", "_").replace("(", "").replace(")", "")}" 到 _CRON_NAME_TO_LOGICAL'
                })
    except Exception:
        pass
    return missing


def _build_pipeline_tasks():
    """构建pipeline时序任务列表
    v4.6.3: 优先从Gateway API读取, fallback到本地legacy文件
    """
    tasks = []
    cron_data = None

    # Tier 1: openclaw cron list --json (live runtime state, works on OpenClaw v2026.6+)
    # 用完整路径+显式PATH避免 LaunchAgent 环境下 PATH 不含 /opt/homebrew/bin
    _OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"
    try:
        import subprocess, os as _os
        _env = _os.environ.copy()
        _env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + _env.get("PATH", "")
        _result = subprocess.run([_OPENCLAW_BIN, "cron", "list", "--json"], capture_output=True, timeout=10, text=True, env=_env)
        if _result.returncode == 0:
            cron_data = json.loads(_result.stdout)
    except Exception:
        pass

    # Tier 2: migrated legacy file
    if not cron_data or "jobs" not in cron_data:
        _paths = [
            os.path.expanduser("~/.openclaw/cron/jobs.json.migrated"),
            os.path.expanduser("~/.openclaw/cron/jobs.json"),
        ]
        for _p in _paths:
            if os.path.exists(_p):
                try:
                    cron_data = json.load(open(_p, encoding="utf-8"))
                    if "jobs" in cron_data:
                        break
                except Exception:
                    continue

    if not cron_data or "jobs" not in cron_data:
        return []

    for j in cron_data.get("jobs", []):
        name = j.get("name", "")
        if not j.get("enabled", True): continue
        if name in _NON_DSL_TASKS: continue  # 过滤非DSL核心任务
        # v4.6.4: 只读取 main agent 任务, 防止其他 agent (如alpha-quant-research) 重复任务污染 Dashboard
        _agent_id = j.get("agentId", "main")
        if _agent_id and _agent_id != "main":
            continue
        expr = j.get("schedule", {}).get("expr", "")
        if not expr: continue
        parts = expr.strip().split()
        if len(parts) < 5: continue
        try:
            h, m = int(parts[1]), int(parts[0])
        except ValueError: continue
        _cron_state = j.get("state", {}) if "state" in j else {}
        _last_run_ms = _cron_state.get("lastRunAtMs", 0)
        tasks.append({
            "task_id": j.get("id", name)[:16],
            "logical_id": _CRON_NAME_TO_LOGICAL.get(
                name.split("(")[0].strip() if "(" in name else name,
                _CRON_NAME_TO_LOGICAL.get(name, name)
            ),
            "name": name,
            "phase": _get_phase(h + m / 60.0),
            "time": f"{h:02d}:{m:02d}",
            "cron_state": _cron_state,  # v4.6.3: 透传cron运行时状态,供missed检测使用
            "cron_dow": parts[4],  # day-of-week field for weekend/holiday filtering
            "model": (j.get("payload",{}) or j).get("model", "flash").rsplit("/",1)[-1][:10],
            "icon": _get_icon(name),
            "desc": name,
        })
    tasks.sort(key=lambda t: int(t["time"].split(":")[0])*60+int(t["time"].split(":")[1]))
    return tasks

PIPELINE_TASKS = None  # v4.6: 延迟加载, 每次 get_pipeline_timeline() 调用时动态重建


def _get_pipeline_tasks() -> list:
    """v4.6: 动态构建任务列表, 不再缓存 — 解决 cron 变更后 Dashboard 不同步问题"""
    return _build_pipeline_tasks()


def _task_runs_today(cron_dow: str) -> bool:
    """Check if a cron day-of-week field includes today.
    cron DOW: 0=Sun, 1=Mon, ..., 6=Sat. Python weekday(): 0=Mon, ..., 6=Sun."""
    today_py = datetime.now().weekday()  # 0=Mon, 6=Sun
    cron_today = (today_py + 1) % 7      # 0=Sun, 1=Mon, ..., 6=Sat
    if cron_dow == "*":
        return True
    for part in cron_dow.split(","):
        part = part.strip()
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                if int(lo) <= cron_today <= int(hi):
                    return True
            except ValueError:
                continue
        elif part.isdigit() and int(part) == cron_today:
            return True
    return False


def get_pipeline_timeline() -> Dict[str, Any]:
    """获取全流程时序任务状态"""
    # 读取最新进度文件 (v4.5.9 fix: 仅加载今日文件, 避免N天前的旧状态)
    progress_dir = os.path.join(CACHE_DIR, "progress")
    progress_map = {}
    today_str = datetime.now().strftime("%Y%m%d")
    # P2-11: 对晚间任务(>=20h), 也检查昨日的progress文件以防跨天
    _current_hour = datetime.now().hour
    _yesterday_str = (datetime.now() - __import__("datetime").timedelta(days=1)).strftime("%Y%m%d")
    _check_dates = [today_str]
    if _current_hour < 6:  # 凌晨0-6点也检查昨天
        _check_dates.append(_yesterday_str)
    if os.path.exists(progress_dir):
        for fname in os.listdir(progress_dir):
            if not fname.endswith(".json"):
                continue
            # 仅处理今日(或凌晨时昨日)的 progress 文件
            if not any(d in fname for d in _check_dates):
                continue
            parts = fname.replace(".json", "").rsplit("_", 2)
            task_key = "_".join(parts[:-2]) if len(parts) >= 3 else parts[0]
            data = safe_read_json(os.path.join(progress_dir, fname))
            if data and data.get("task_id"):
                tid = data["task_id"]
                # 标准化 task_id (去掉时间戳后缀)
                base_tid = tid.rsplit("_", 2)[0] if tid.count("_") >= 2 else tid
                # P1-fix: 多时段任务(如stop_loss_monitor)按时间槽独立存储,避免折叠
                _time_slot = parts[-1][:4] if len(parts) >= 3 and (
                    "stop_loss" in base_tid
                ) else ""
                _storage_key = f"{base_tid}_{_time_slot}" if _time_slot else base_tid
                # 保留最新的记录
                existing = progress_map.get(_storage_key, {})
                existing_ts = existing.get("started_at", "")
                new_ts = data.get("started_at", "")
                if not existing_ts or new_ts > existing_ts:
                    # 映射到标准 task_id
                    mapped_id = _map_task_id(base_tid)
                    if _time_slot:
                        mapped_id = f"{mapped_id}_{_time_slot}"
                    progress_map[mapped_id] = {
                        "task_id": mapped_id,
                        "status": data.get("status", "unknown"),
                        "message": data.get("message", ""),
                        "step": data.get("step", 0),
                        "total": data.get("total", 0),
                        "progress_pct": data.get("progress_pct", 0),
                        "started_at": data.get("started_at", ""),
                        "completed_at": data.get("completed_at", data.get("updated_at", "")),
                        "last_update": data.get("last_update", data.get("updated_at", data.get("started_at", ""))),
                        "raw_file": fname,
                    }

    # 构建完整timeline
    phases_order = {"night": "🌙 凌晨", "premarket": "🌅 盘前", "intraday": "☀️ 盘中",
                     "postmarket": "🌤️ 收盘", "evening": "🌆 夜间", "weekly": "📅 周期"}
    timeline = {}
    for task_def in _get_pipeline_tasks():
        tid = task_def["task_id"]
        phase = task_def["phase"]
        if phase not in timeline:
            timeline[phase] = {"label": phases_order.get(phase, phase), "tasks": []}
        # 合并进度数据 — 多级查找: task_id → logical_id → _CRON_NAME_TO_LOGICAL映射值
        _lid = task_def.get("logical_id", "")
        _mapped_lid = _CRON_NAME_TO_LOGICAL.get(_lid, _CRON_NAME_TO_LOGICAL.get(_lid.replace("（", "("), ""))
        # 去掉时间后缀再试一次 (如"个股分批预测(17:00)" → "个股分批预测")
        _lid_base = _lid
        for _suffix in ["(02:00)", "(02:10)", "(03:30)", "(08:10)", "(16:00)", "(17:00)", "(09:20)", "(09:30)", "(09:45)", "(11:00)", "(14:00)", "(15:10)", "(15:40)", "（02:00）", "（02:10）", "（16:00）", "（17:00）", "（09:20）", "（09:30）"]:
            _lid_base = _lid_base.replace(_suffix, "")
        _mapped_base = _CRON_NAME_TO_LOGICAL.get(_lid_base, "")
        # v4.5.17: 别名映射表 — 所有任务均已独立创建ProgressTracker, 无需别名
        _alias_map = {}
        _alias_lid = _alias_map.get(_lid, "")
        # v4.6 fallback: batch_predict_0900/batch_predict_1700 → batch_predict (旧命名兼容)
        _fallback_predict = ""
        if _lid in ("batch_predict_0900", "batch_predict_1700") or _mapped_lid in ("batch_predict_0900", "batch_predict_1700"):
            _fallback_predict = "batch_predict"
        progress = (progress_map.get(tid)
                    or progress_map.get(_lid)
                    or progress_map.get(_mapped_lid)
                    or progress_map.get(_mapped_base)
                    or progress_map.get(_alias_lid)
                    or progress_map.get(_fallback_predict)
                    or {})
        task_entry = {**task_def}
        task_entry.update({
            "status": progress.get("status", "idle"),
            "message": progress.get("message", ""),
            "step": progress.get("step", 0),
            "total": progress.get("total", 0),
            "progress_pct": progress.get("progress_pct", 0),
            "started_at": progress.get("started_at", ""),
            "completed_at": progress.get("completed_at", ""),
            "last_update": progress.get("last_update", ""),
        })
        # 🔧 过期检测：running状态超过staleness_hours自动标记为completed
        staleness_hours = task_def.get("staleness_hours", 2)  # 默认2小时
        if task_entry["status"] == "running" and task_entry["started_at"]:
            try:
                started_dt = datetime.fromisoformat(task_entry["started_at"])
                elapsed_h = (datetime.now() - started_dt).total_seconds() / 3600
                if elapsed_h > staleness_hours:
                    # P2-10: 改为warning级别状态（不再标记为completed以避免误导）
                    task_entry["status"] = "stale"
                    task_entry["message"] = f"{task_entry['message']} (⚠️ 进程中断, 运行{elapsed_h:.1f}h未更新, 已自动标记)"
                    task_entry["completed_at"] = (started_dt + timedelta(hours=min(elapsed_h, staleness_hours))).isoformat()
            except (ValueError, TypeError):
                pass
        
        # v4.5.13: 针对无ProgressTracker的任务，检查其特有的输出文件
        if task_entry["status"] in ("idle", "running"):
            if _lid == "blackswan_review":
                _bs_file = os.path.expanduser("~/.openclaw/workspace/memory/black-swan/analysis-{}.json".format(datetime.now().strftime("%Y-%m-%d")))
                _resolved = False
                if os.path.exists(_bs_file):
                    try:
                        _bs_mt = datetime.fromtimestamp(os.stat(_bs_file).st_mtime)
                        task_entry["status"] = "completed"
                        task_entry["message"] = f"已完成 ({_bs_mt.strftime('%H:%M')})"
                        task_entry["started_at"] = _bs_mt.isoformat()
                        task_entry["completed_at"] = _bs_mt.isoformat()
                        _resolved = True
                    except OSError:
                        pass
                # v4.6.7: fallback — 如果analysis文件不存在, 检查cron_state (cron agentTurn无本地progress文件)
                if not _resolved:
                    _cron_state = task_entry.get('cron_state', {}) or {}
                    _last_run_ms = _cron_state.get('lastRunAtMs', 0)
                    _last_status = _cron_state.get('lastRunStatus', '')
                    if _last_run_ms and _last_status == 'ok':
                        _today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                        _run_dt = datetime.fromtimestamp(_last_run_ms / 1000)
                        if _run_dt >= _today_start:
                            task_entry["status"] = "completed"
                            task_entry["message"] = f"已完成 ({_run_dt.strftime('%H:%M')}, 飞书卡片已推送)"
                            task_entry["started_at"] = _run_dt.isoformat()
                            task_entry["completed_at"] = _run_dt.isoformat()
                            _resolved = True

        # v4.5.18: 跳票检测 — progress文件时间早于今日计划执行时间 → missed
        # v4.5.18 fix: 先检查今日是否在cron调度范围内, 不在则标记skipped
        if task_entry["status"] in ("completed", "running", "idle") and task_entry.get("time", ""):
            try:
                # 检查cron DOW字段：如果今天不在调度日内，标记为skipped跳过
                _cron_dow = task_entry.get("cron_dow", "*")
                if _cron_dow != "*" and not _task_runs_today(_cron_dow):
                    if task_entry["status"] in ("idle", "completed"):
                        _was_completed = task_entry["status"] == "completed"
                        task_entry["status"] = "skipped"
                        _manual_run = task_entry.get("started_at", "") and task_entry.get("started_at")[:10] == today_str
                        if _was_completed or _manual_run:
                            task_entry["message"] = "今日不执行 (非调度日, 手动测试残留)"
                        else:
                            task_entry["message"] = "今日不执行 (非调度日)"
                    timeline[phase]["tasks"].append(task_entry)
                    continue
                _time_parts = task_entry["time"].split(":")
                _sched_h, _sched_m = int(_time_parts[0]), int(_time_parts[1])
                _sched_today = datetime.now().replace(hour=_sched_h, minute=_sched_m, second=0, microsecond=0)
                # 如果计划时间已过去30+分钟（cron宽限期）
                if datetime.now() > _sched_today + timedelta(minutes=30):
                    # v4.5.22: 交易执行特殊检测 — 检查paper_trading.db确认是否实际执行
                    _lid = task_entry.get('logical_id', '')
                    if _lid == 'trade_execution_0930':
                        import sys as _sys, sqlite3 as _sql
                        _db_path = os.path.join(DATA_DIR, "paper_trading.db")
                        if os.path.exists(_db_path):
                            _conn = _sql.connect(_db_path)
                            # v4.5.22 fix: today_str格式是%Y%m%d, 但DB时间戳是%Y-%m-%dT...
                            _date_prefix = datetime.now().strftime("%Y-%m-%d")
                            _today_trades = _conn.execute(
                                "SELECT COUNT(*) FROM trade_history WHERE timestamp LIKE ?",
                                (_date_prefix + "%",)
                            ).fetchone()[0]
                            _conn.close()
                            if _today_trades > 0:
                                task_entry["status"] = "completed"
                                task_entry["message"] = f"已执行 {_today_trades} 笔交易"
                                task_entry["completed_at"] = datetime.now().isoformat()
                                timeline[phase]["tasks"].append(task_entry)
                                continue
                    _progress_time = None
                    if task_entry.get("started_at", ""):
                        try:
                            _progress_time = datetime.fromisoformat(task_entry["started_at"])
                        except:
                            pass
                    # v4.0: 先检查是否有报告文件(LLM生成的报告已归档)
                    _report_found = False
                    _logical_id = task_entry.get('logical_id', '')
                    _today_prefixes = {
                        'auto_verify': f'auto_verify_{today_str}',
                        'lppl_morning': f'lppl_daily_{today_str}',
                        'lppl_full': 'lppl_full_',
                    }
                    if _logical_id in _today_prefixes:
                        _rdir = os.path.join(PROJECT_ROOT, 'cache', 'reports')
                        _prefix = _today_prefixes[_logical_id]
                        _reports = [f for f in os.listdir(_rdir) if f.startswith(_prefix)] if os.path.isdir(_rdir) else []
                        if _reports:
                            _mt = max(os.path.getmtime(os.path.join(_rdir, f)) for f in _reports)
                            _mt_dt = datetime.fromtimestamp(_mt)
                            task_entry['status'] = 'completed'
                            task_entry['message'] = f'已完成 ({_mt_dt.strftime("%H:%M")})'
                            task_entry['completed_at'] = _mt_dt.isoformat()
                            _report_found = True

                    # v4.5.18 fix: dsl_backup/backup_report 检查输出文件，避免跳票误判
                    if not _report_found and _logical_id in ('dsl_backup', 'backup_report'):
                        _max_age = 86400  # 24h（与idle推断窗口一致）
                        if _logical_id == 'dsl_backup':
                            # 先检查 backup-git.log（Git同步专用输出）
                            _git_log = os.path.expanduser('~/.openclaw/logs/backup-git.log')
                            if os.path.exists(_git_log):
                                _git_mt_dt = datetime.fromtimestamp(os.path.getmtime(_git_log))
                                if (datetime.now() - _git_mt_dt).total_seconds() < _max_age \
                                   and _git_mt_dt.hour == 3 and _git_mt_dt.minute >= 30:
                                    task_entry['status'] = 'completed'
                                    task_entry['message'] = f'Git已同步 ({_git_mt_dt.strftime("%H:%M")})'
                                    task_entry['completed_at'] = _git_mt_dt.isoformat()
                                    _report_found = True
                            # 兜底: 检查 backup/ 目录的 tar.gz 文件（02:00/02:10 备份用）
                            if not _report_found:
                                _bk_dir = os.path.expanduser('~/.openclaw/workspace/backup')
                                if os.path.exists(_bk_dir):
                                    _bk_files = [f for f in os.listdir(_bk_dir) if f.endswith('.tar.gz') and f.startswith('dsl_backup_')]
                                    if _bk_files:
                                        _mt = max(os.path.getmtime(os.path.join(_bk_dir, f)) for f in _bk_files)
                                        _mt_dt = datetime.fromtimestamp(_mt)
                                        if (datetime.now() - _mt_dt).total_seconds() < _max_age:
                                            task_entry['status'] = 'completed'
                                            task_entry['message'] = f'已完成 ({_mt_dt.strftime("%H:%M")})'
                                            task_entry['completed_at'] = _mt_dt.isoformat()
                                            _report_found = True
                        elif _logical_id == 'backup_report':
                            for _log_dir in [os.path.join(PROJECT_ROOT, 'logs'), os.path.expanduser('~/.openclaw/workspace/backup')]:
                                if not os.path.exists(_log_dir):
                                    continue
                                _rp_files = [f for f in os.listdir(_log_dir) if 'backup' in f.lower() and f.endswith(('.json','.md','.txt','.tar.gz'))]
                                if not _rp_files:
                                    continue
                                _mt = max(os.path.getmtime(os.path.join(_log_dir, f)) for f in _rp_files)
                                _mt_dt = datetime.fromtimestamp(_mt)
                                if (datetime.now() - _mt_dt).total_seconds() < _max_age:
                                    task_entry['status'] = 'completed'
                                    task_entry['message'] = f'已完成 ({_mt_dt.strftime("%H:%M")})'
                                    task_entry['completed_at'] = _mt_dt.isoformat()
                                    _report_found = True
                                    break

                    # v4.6.3: 从task_entry的cron_state判断运行状态 (Gateway API透传)
                    _cron_state = task_entry.get('cron_state', {}) or {}
                    _cron_ran = False
                    _cron_errored = False
                    _cron_ran_today = False
                    try:
                        if _cron_state:
                            _lrs = _cron_state.get('lastRunStatus')
                            _lrt = _cron_state.get('lastRunAtMs', 0)
                            _lerr = _cron_state.get('lastError', '')
                            _duration_ms = _cron_state.get('lastDurationMs', 0)
                            if _lrt > 0 and (_lrt / 1000) > (_sched_today.timestamp() - 3600):
                                _cron_ran = True
                                _lrt_dt = datetime.fromtimestamp(_lrt / 1000)
                                if _lrt_dt.strftime('%Y%m%d') == today_str:
                                    _cron_ran_today = True
                                _lerr_lower = _lerr.lower()
                                _is_timeout_error = (
                                    'timeout' in _lerr_lower
                                    or 'timed out' in _lerr_lower
                                    or '超时' in _lerr
                                )
                                if _lrs == 'error' or _is_timeout_error:
                                    _cron_errored = True
                                    _lrs_detail = '超时' if _is_timeout_error else '失败'
                                    _duration_s = _duration_ms / 1000
                                    task_entry["wrapper_status"] = "timeout" if _lrs_detail == '超时' else "failed"
                                    task_entry["wrapper_error"] = _lerr
                                    task_entry["wrapper_duration_seconds"] = round(_duration_s, 1)
                                    if task_entry.get("status") == "completed" and task_entry.get("started_at"):
                                        _base_msg = task_entry.get("message") or "已完成"
                                        task_entry["message"] = f"{_base_msg} (cron wrapper{_lrs_detail}: {_duration_s:.0f}s)"
                                    else:
                                        task_entry["status"] = "timeout"
                                        task_entry["message"] = f"⏰ 计划{task_entry['time']}执行, 运行超时({_duration_s:.0f}s)"
                                        if _lerr:
                                            task_entry["message"] += f" | {_lerr[:80]}"
                    except Exception:
                        pass

                    # 没有progress 且无报告文件 → 跳票
                    if not _report_found and not _cron_errored and (_progress_time is None or _progress_time < _sched_today - timedelta(hours=1)):
                        # v4.6.x: 对于无progress文件的任务(如pool_warmup), 如果cron今日确实运行了则不标记为跳票
                        if _cron_ran_today:
                            _lrt_dt_final = datetime.fromtimestamp(_cron_state.get('lastRunAtMs', 0) / 1000)
                            task_entry["status"] = "completed"
                            task_entry["message"] = f"已完成 ({_lrt_dt_final.strftime('%H:%M')})"
                            task_entry["completed_at"] = _lrt_dt_final.isoformat()
                        else:
                            task_entry["status"] = "missed"
                            task_entry["message"] = f"⏰ 计划{task_entry['time']}执行, 未检测到执行记录"
                            if _progress_time:
                                task_entry["message"] += f" (最近记录: {_progress_time.strftime('%H:%M')})"
            except (ValueError, IndexError, TypeError):
                pass
        timeline[phase]["tasks"].append(task_entry)

    # 统计
    stats = {"total": 0, "completed": 0, "running": 0, "failed": 0, "timeout": 0, "idle": 0, "skipped": 0}
    for phase_data in timeline.values():
        for t in phase_data["tasks"]:
            stats["total"] += 1
            s = t.get("status", "idle")
            if s in stats:
                stats[s] += 1
    
    # 构建task_id→task_def映射
    task_def_map = {td["task_id"]: td for td in _get_pipeline_tasks()}

    # 为无进度文件的idle任务推断状态
    for phase_data in timeline.values():
        for t in phase_data["tasks"]:
            tid = t.get("task_id")
            if t.get("status") != "idle":
                continue
            now = datetime.now()
            today = now.date()
            weekday = today.weekday()  # 0=Mon,6=Sun
            
            # v4.5.9 fix: idle推断需同时匹配task_id(UUID)和logical_id
            logical_id = t.get("logical_id", tid)

            # 盘前交易预案: 检查planned_trades.json (必须在计划时间之后生成)
            if logical_id == "pre_market_plan" or tid == "pre_market_plan":
                pt_path = os.path.join(CACHE_DIR, "planned_trades.json")
                if os.path.exists(pt_path):
                    mt = datetime.fromtimestamp(os.path.getmtime(pt_path))
                    # 必须在计划时间(21:30)之后 + 24h内
                    task_hour = int(t.get("time", "21:30").split(":")[0])
                    task_time_today = now.replace(hour=task_hour, minute=0, second=0, microsecond=0)
                    if mt > task_time_today and (now - mt).total_seconds() < 86400:
                        t["status"] = "completed"; t["message"] = f"已生成预案 ({mt.strftime('%m-%d %H:%M')})"
                        t["completed_at"] = mt.isoformat(); stats["completed"] += 1; stats["idle"] -= 1; continue

            # v4.5.9: 交易执行(09:30) — 多重验证是否实际执行了交易
            if logical_id == "trade_execution_0930" or tid == "trade_execution_0930":
                executed = False
                # 1. 检查 paper_trading.db 中今日是否有新交易
                try:
                    import sqlite3
                    db_path = os.path.join(DATA_DIR, "paper_trading.db")
                    if os.path.exists(db_path):
                        conn = sqlite3.connect(db_path)
                        today_trades = conn.execute(
                            "SELECT COUNT(*) FROM trade_history WHERE timestamp LIKE ?",
                            (now.strftime("%Y-%m-%d") + "%",)
                        ).fetchone()[0]
                        conn.close()
                        if today_trades > 0:
                            t["status"] = "completed"; t["message"] = f"已执行 {today_trades} 笔交易"
                            t["completed_at"] = now.isoformat(); stats["completed"] += 1; stats["idle"] -= 1
                            executed = True
                except Exception:
                    pass
                # 2. 检查 execution_log.jsonl
                if not executed:
                    exec_log = os.path.join(DATA_DIR, "execution_log.jsonl")
                    if os.path.exists(exec_log):
                        try:
                            with open(exec_log) as f:
                                today_lines = [l for l in f if l.startswith(now.strftime("%Y-%m-%d"))]
                            if today_lines:
                                t["status"] = "completed"; t["message"] = f"已执行 {len(today_lines)} 笔交易"
                                t["completed_at"] = now.isoformat(); stats["completed"] += 1; stats["idle"] -= 1
                                executed = True
                        except Exception:
                            pass
                # 3. 如果以上都没有，检查 planned_trades.json 是否在今日生成但未执行
                if not executed:
                    pt_path = os.path.join(CACHE_DIR, "planned_trades.json")
                    if os.path.exists(pt_path):
                        mt = datetime.fromtimestamp(os.path.getmtime(pt_path))
                        if mt.strftime("%Y-%m-%d") == today_str and mt.hour >= 9:
                            t["status"] = "completed"; t["message"] = f"已生成预案 (待执行, {mt.strftime('%H:%M')})"
                            t["completed_at"] = mt.isoformat(); stats["completed"] += 1; stats["idle"] -= 1
                            executed = True
                if executed:
                    continue

            # 黑天鹅每日复盘: 检查最近分析文件 (必须在计划时间之后生成)
            if logical_id == "blackswan_review" or tid == "blackswan_review":
                bs_dir = os.path.join(os.path.dirname(DATA_DIR), "memory", "black-swan")
                if not os.path.exists(bs_dir): bs_dir = os.path.expanduser("~/.openclaw/workspace/memory/black-swan")
                if os.path.exists(bs_dir):
                    files = sorted([f for f in os.listdir(bs_dir) if f.startswith("analysis-") and f.endswith(".json")], reverse=True)
                    if files:
                        try:
                            mt = datetime.fromtimestamp(os.path.getmtime(os.path.join(bs_dir, files[0])))
                        except (FileNotFoundError, OSError):
                            continue
                        # 必须在计划时间之后 + 24h内
                        task_hour = int(t.get("time", "21:00").split(":")[0])
                        task_time_today = now.replace(hour=task_hour, minute=0, second=0, microsecond=0)
                        if mt > task_time_today and (now - mt).total_seconds() < 86400:
                            t["status"] = "completed"; t["message"] = f"已完成 ({mt.strftime('%m-%d %H:%M')})"
                            t["completed_at"] = mt.isoformat(); stats["completed"] += 1; stats["idle"] -= 1; continue

            # v4.5.12 fix: DSL系统备份 — 检查backup/目录最近文件(实际输出在~/openclaw/workspace/backup/)
            if logical_id == "dsl_backup" or tid == "dsl_backup":
                bk_dir = os.path.expanduser("~/.openclaw/workspace/backup")
                if os.path.exists(bk_dir):
                    files = [f for f in os.listdir(bk_dir) if f.endswith('.tar.gz') and f.startswith('dsl_backup_')]
                    if files:
                        mt = max(os.path.getmtime(os.path.join(bk_dir, f)) for f in files)
                        mt_dt = datetime.fromtimestamp(mt)
                        if (now - mt_dt).total_seconds() < 86400:
                            t["status"] = "completed"; t["message"] = f"已完成 ({mt_dt.strftime('%H:%M')})"
                            t["completed_at"] = mt_dt.isoformat(); stats["completed"] += 1; stats["idle"] -= 1; continue

            # v4.5.12 fix: 备份状态报告 — 检查logs/目录和backup/目录
            if logical_id == "backup_report" or tid == "backup_report":
                found = False
                for log_dir in [os.path.join(PROJECT_ROOT, "logs"), os.path.expanduser("~/.openclaw/workspace/backup")]:
                    if os.path.exists(log_dir):
                        files = [f for f in os.listdir(log_dir) if 'backup' in f.lower() and f.endswith(('.json','.md','.txt','.tar.gz'))]
                        if files:
                            mt = max(os.path.getmtime(os.path.join(log_dir, f)) for f in files)
                            mt_dt = datetime.fromtimestamp(mt)
                            if (now - mt_dt).total_seconds() < 86400:
                                t["status"] = "completed"; t["message"] = f"已完成 ({mt_dt.strftime('%H:%M')})"
                                t["completed_at"] = mt_dt.isoformat(); stats["completed"] += 1; stats["idle"] -= 1
                                found = True; break
                if found: continue

            # v4.6.x: 模型连接池预热 — 无输出文件, 直接从 jobs-state.json 检测
            if logical_id == "pool_warmup" or tid == "pool_warmup":
                try:
                    _pj = os.path.expanduser("~/.openclaw/cron/jobs-state.json")
                    if os.path.exists(_pj):
                        with open(_pj, encoding="utf-8") as _pf:
                            _ps = json.load(_pf)
                        _sj = _ps.get('jobs', {}) if isinstance(_ps.get('jobs'), dict) else {}
                        # 需要 name→id 映射
                        _dj = os.path.expanduser("~/.openclaw/cron/jobs.json")
                        if os.path.exists(_dj):
                            with open(_dj, encoding="utf-8") as _df:
                                _dd = json.load(_df)
                        _n2i = {}
                        for _cj in _dd.get('jobs', []):
                            if not _cj.get('enabled', True):
                                continue
                            _n2i[_cj.get('name', '')] = _cj.get('id', '')
                        _mid = _n2i.get('DSL模型连接池预热(08:50)', '')
                        if _mid and _mid in _sj:
                            _st = _sj[_mid].get('state', {})
                            _lrt = _st.get('lastRunAtMs', 0)
                            if _lrt > 0:
                                _lrt_dt = datetime.fromtimestamp(_lrt / 1000)
                                if _lrt_dt.strftime('%Y%m%d') == today_str:
                                    t['status'] = 'completed'
                                    t['message'] = f'已执行 ({_lrt_dt.strftime("%H:%M")})'
                                    t['completed_at'] = _lrt_dt.isoformat()
                                    stats['completed'] += 1; stats['idle'] -= 1
                                    _p_recheck = False; continue
                except Exception:
                    pass

            # v4.5.9: 自我反思 — 检查 adaptive_params.yaml feedback_history 是否有今日更新
            if logical_id == "self_reflection" or tid == "self_reflection":
                ap_path = os.path.join(CONFIG_DIR, "adaptive_params.yaml")
                if os.path.exists(ap_path):
                    try:
                        import yaml
                        with open(ap_path, "r", encoding="utf-8") as f:
                            ap = yaml.safe_load(f)
                        history = ap.get("feedback_history", [])
                        today_entries = [h for h in history
                                        if h.get("date","").startswith(today_str)]
                        if today_entries:
                            t["status"] = "completed"; t["message"] = f"已完成 ({len(today_entries)}条反思)"
                            t["completed_at"] = now.isoformat(); stats["completed"] += 1; stats["idle"] -= 1; continue
                    except Exception:
                        pass

            # v4.0: 黑天鹅自动验证 — 检查今日报告文件
            if logical_id == 'auto_verify':
                report_dir = os.path.join(PROJECT_ROOT, 'cache', 'reports')
                today_prefix = f'auto_verify_{today_str}'
                reports = [f for f in os.listdir(report_dir) if f.startswith(today_prefix)] if os.path.isdir(report_dir) else []
                if reports:
                    mt = max(os.path.getmtime(os.path.join(report_dir, f)) for f in reports)
                    mt_dt = datetime.fromtimestamp(mt)
                    t['status'] = 'completed'
                    t['message'] = f'已完成 ({mt_dt.strftime("%H:%M")})'
                    t['completed_at'] = mt_dt.isoformat()
                    stats['completed'] += 1; stats['idle'] -= 1; continue

            # v4.0: LPPL晨间检测 — 检查今日报告
            if logical_id == 'lppl_morning':
                report_dir = os.path.join(PROJECT_ROOT, 'cache', 'reports')
                today_prefix = f'lppl_daily_{today_str}'
                reports = [f for f in os.listdir(report_dir) if f.startswith(today_prefix)] if os.path.isdir(report_dir) else []
                if reports:
                    mt = max(os.path.getmtime(os.path.join(report_dir, f)) for f in reports)
                    mt_dt = datetime.fromtimestamp(mt)
                    t['status'] = 'completed'
                    t['message'] = f'已完成 ({mt_dt.strftime("%H:%M")})'
                    t['completed_at'] = mt_dt.isoformat()
                    stats['completed'] += 1; stats['idle'] -= 1; continue

            # v4.0: LPPL全量检测 — 检查最近报告
            if logical_id == 'lppl_full':
                report_dir = os.path.join(PROJECT_ROOT, 'cache', 'reports')
                today_prefix = 'lppl_full_'
                reports = [f for f in os.listdir(report_dir) if f.startswith(today_prefix)] if os.path.isdir(report_dir) else []
                if reports:
                    mt = max(os.path.getmtime(os.path.join(report_dir, f)) for f in reports)
                    mt_dt = datetime.fromtimestamp(mt)
                    if (datetime.now() - mt_dt).total_seconds() < 86400:
                        t['status'] = 'completed'
                        t['message'] = f'已完成 ({mt_dt.strftime("%m-%d %H:%M")})'
                        t['completed_at'] = mt_dt.isoformat()
                        stats['completed'] += 1; stats['idle'] -= 1; continue


            # 通用时间推断: 对已过执行时间的任务标记推测完成
            time_str = t.get("time", "")
            target_hour, target_min = None, None
            is_weekly = False
            
            if "周六" in time_str:
                is_weekly = True
                if weekday != 5: continue  # 不是周六跳过
                target_hour, target_min = _parse_time(time_str.replace("周六 ", ""))
            elif "周一" in time_str:
                is_weekly = True
                if weekday != 0: continue  # 不是周一跳过
                target_hour, target_min = _parse_time(time_str.replace("周一 ", ""))
            elif ":" in time_str:
                target_hour, target_min = _parse_time(time_str)
            
            if target_hour is not None:
                task_time = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
                is_future_today = task_time > now  # 今天还没到时间

                # 如果今天还没到时间，检查昨天的是否已完成
                if is_future_today:
                    yesterday_task = task_time - timedelta(days=1)
                    # 如果昨天这个时间已经过了(即昨天的任务应该已完成)
                    # → 保持idle等待今天的(昨天的状态由昨天的progress处理)
                    continue

                staleness = task_def_map.get(tid, {}).get("staleness_hours", 2) if tid else 2
                staleness_deadline = task_time + timedelta(hours=staleness)
                if now > staleness_deadline:
                    # 跳过周六日的日常任务
                    if weekday >= 5 and not is_weekly:
                        continue
                    # 早盘任务(<=12:00): 仅在当日15:00前可推断完成, 之后不再自动标记
                    is_morning_task = target_hour <= 12
                    afternoon_cutoff = task_time.replace(hour=17, minute=0, second=0)
                    if is_morning_task and now > afternoon_cutoff:
                        # 已过下午5点仍未执行 → 标记为跳过而非完成
                        t["status"] = "skipped"
                        t["message"] = f"跳过 (计划{time_str}, 已过执行窗口)"
                        stats["skipped"] += 1
                        stats["idle"] -= 1
                        continue
                    t["status"] = "completed"
                    t["message"] = f"推定完成 (计划{time_str}, 无今日执行记录⚠️)"
                    t["completed_at"] = task_time.isoformat()
                    stats["completed"] += 1
                    stats["idle"] -= 1

    # ── 标记当前执行 & 即将执行的任务 ──
    now = datetime.now()
    # 第一阶段：running中的任务优先标记
    all_tasks_flat = []
    for phase_key in phases_order:
        for t in timeline.get(phase_key, {}).get("tasks", []):
            all_tasks_flat.append((phase_key, t))
    
    current_count = 0
    for phase_key, t in all_tasks_flat:
        if t.get("status") == "running" and current_count < 2:
            t["is_current"] = True
            current_count += 1
    
    # 第二阶段：查找空闲任务中的"即将执行"
    now_hour = now.hour + now.minute / 60.0
    is_trading = _check_trading_day()
    next_found = False
    for phase_key, t in all_tasks_flat:
        if t.get("status") != "idle":
            continue
        if t.get("is_current"):
            continue
        # 非交易日：跳过今天不执行的日常交易任务
        if not is_trading and not _task_runs_today(t.get("cron_dow", "*")):
            continue
        try:
            task_h = int(t["time"].split(":")[0])
            task_m = int(t["time"].split(":")[1])
            task_hour = task_h + task_m / 60.0
        except (ValueError, KeyError):
            continue

        # 计算当前时间到计划时间的差值（小时）
        delta = task_hour - now_hour
        # 处理跨天
        if delta < -6:
            delta += 24

        # 在计划时间±30分钟内 → 标记为当前
        if -0.5 <= delta <= 0.5 and current_count < 2:
            t["is_current"] = True
            current_count += 1
        # 第一个未来的任务 → 标记为下一个
        elif delta > 0.5 and not next_found:
            t["is_next"] = True
            next_found = True

    # 第三阶段：如果没找到未来任务，标记今天会执行的最后一个idle为next
    if not next_found:
        for phase_key, t in reversed(all_tasks_flat):
            if t.get("status") != "idle" or t.get("is_current"):
                continue
            if not is_trading and not _task_runs_today(t.get("cron_dow", "*")):
                continue
            t["is_next"] = True
            break

    return {"timeline": timeline, "stats": stats, "phases_order": list(phases_order.keys())}


def _parse_time(t: str) -> tuple:
    """解析时间字符串HH:MM返回(hour, min), 失败返回(None,None)"""
    try:
        parts = t.strip().split(":")
        return int(parts[0]), int(parts[1])
    except: return None, None


def _fallback_position_ratio(severity: int) -> float:
    """纯severity→仓位公式(1-5分制), 供测试与fallback使用"""
    if severity >= 5: return 0.10   # 极度危险: 10%
    if severity >= 4: return 0.25   # 高危: 25%
    if severity >= 3: return 0.50   # 中高: 50%
    if severity >= 2: return 0.75   # 中等: 75%
    return 1.0                      # 低风险: 100%


def _compute_position_ratio(severity: int) -> float:
    """仓位比例 — 优先从adaptive_params.yaml读取, fallback到severity公式

    v4.5.7 S6: 统一使用adaptive_params.yaml的black_swan_position_ratio(由
    feedback_controller动态计算: max(0.15, 1.0 - max_weighted_impact*1.2))，
    而非dashboard独立硬编码公式，避免显示值与系统实际值脱节。
    仅在黑天鹅活跃(active=True + severity≥3)时读取config值，避免低风险时误读残留值。

    v4.6.9 P0: adaptive_params优先(与morning_decision执行层同源),
    black_swan_status.json仅作fallback — 修复status.json被lppl_to_dsl
    min()单向压缩残留(只降不升)导致Dashboard显示20% vs 执行层55%的分裂。
    """
    ratios = []
    # 真相源1: adaptive_params.yaml (feedback_controller 动态计算, 执行层同源)
    try:
        ap_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        if os.path.exists(ap_path):
            with open(ap_path, "r", encoding="utf-8") as f:
                ap = yaml.safe_load(f)
        if ap and "risk" in ap:
            risk = ap["risk"]
            bs_active = risk.get("black_swan_active", False)
            if bs_active and severity >= 3:
                ratio = risk.get("black_swan_position_ratio", None)
                if ratio is not None and isinstance(ratio, (int, float)):
                    return float(ratio)
    except Exception:
        pass
    # 真相源2: black_swan_status.json (仅当adaptive缺失/未激活时使用)
    try:
        status_path = os.path.join(DATA_DIR, "black_swan_status.json")
        status = safe_read_json(status_path) or {}
        ratio = status.get("position_ratio")
        if status.get("active") and severity >= 3 and isinstance(ratio, (int, float)):
            return float(ratio)
    except Exception:
        pass
    return _fallback_position_ratio(severity)


def _map_task_id(raw_id: str) -> str:
    """将进度文件中的task_id映射到标准_get_pipeline_tasks中的task_id"""
    mapping = {
        "dsl_backup": "dsl_backup",
        "batch_train": "batch_train",
        "backup_report": "backup_report",
        "batch_predict": "batch_predict",
        "batch_predict_0900": "batch_predict_0900",
        "batch_predict_1700": "batch_predict_1700",
        "self_reflection": "self_reflection",
        "feedback_controller": "self_reflection",  # feedback_controller 归属自我反思
        "pre_market_refresh": "pre_market_refresh",
        "pre_market_decision": "pre_market_decision",
        "intraday_monitor_1430": "intraday_monitor_1430",
        "intraday_monitor_1400": "intraday_monitor_1400",
        "intraday_monitor_1330": "intraday_monitor_1330",
        "intraday_monitor_1100": "intraday_monitor_1100",
        "intraday_monitor_1030": "intraday_monitor_1030",
        "intraday_monitor_0945": "intraday_monitor_0945",
        "intraday_monitor": "intraday_monitor_0945",  # 默认映射到0945
        "trade_execution_0930": "trade_execution_0930",
        "health_check": "health_check",
        "pre_market_plan": "pre_market_plan",
        "blackswan_review": "blackswan_review",
        "stop_loss_monitor": "stop_loss_monitor",
        "weekly_pool_refresh": "weekly_pool_refresh",
        "weekly_model_review": "weekly_model_review",
        "weekly_review": "weekly_review",
        "daily_sim": "closing_daily_report",
        "closing_daily": "closing_daily_report",
        "closing_daily_report": "closing_daily_report",
        "daily_report": "closing_daily_report",
        # v4.5.18: 新增黑天鹅/LPPL/auto_verify任务
        "lppl_morning": "lppl_morning",
        "lppl_full": "lppl_full",
        "pool_warmup": "pool_warmup",
        "lppl_full": "lppl_full",
        "auto_verify": "auto_verify",
        "_watchdog": "_watchdog",
    }
    # 模糊匹配
    for key, val in mapping.items():
        if key in raw_id:
            return val
    return raw_id


def get_task_reports(task_id: str) -> List[Dict[str, Any]]:
    """获取指定任务类型的历史执行报告"""
    # 如果传入的是截断的UUID，尝试映射到逻辑ID
    # 先查 pipeline 中的 logical_id
    resolved_id = task_id
    for td in _get_pipeline_tasks():
        if td["task_id"] == task_id and td.get("logical_id"):
            resolved_id = td["logical_id"]
            break
    
    # 用逻辑ID查找报告
    task_id = resolved_id
    # v4.5.23: 标准化带时段后缀的任务ID → 基础ID (stop_loss_monitor_0950 → stop_loss_monitor)
    _NORM_ID = {
        "stop_loss_monitor_0950": "stop_loss_monitor",
        "stop_loss_monitor_1035": "stop_loss_monitor",
        "stop_loss_monitor_1105": "stop_loss_monitor",
        "stop_loss_monitor_1335": "stop_loss_monitor",
        "stop_loss_monitor_1435": "stop_loss_monitor",
    }
    task_id = _NORM_ID.get(task_id, task_id)
    reports = []
    reports_dir = os.path.join(PROJECT_ROOT, "reports")
    logs_dir = os.path.join(PROJECT_ROOT, "logs", "reports")
    progress_dir = os.path.join(CACHE_DIR, "progress")

    # 报告类型与目录映射 (v4.5.17: 修复跨任务记录污染 + 新增LPPL/auto_verify)
    # 注意: 共享目录(cache/progress, cache/reports)会通过 _TASK_FILE_PREFIX 过滤
    report_dirs = {
        "batch_train": [os.path.join(reports_dir, "backtest"), os.path.join(reports_dir, "horizon_eval")],
        "batch_predict": [os.path.join(CACHE_DIR, "daily_predict.json"), os.path.join(reports_dir, "horizon_eval")],
        "batch_predict_0900": [os.path.join(CACHE_DIR, "daily_predict.json"), os.path.join(reports_dir, "horizon_eval")],
        "batch_predict_1700": [os.path.join(CACHE_DIR, "daily_predict.json"), os.path.join(reports_dir, "horizon_eval")],
        "backup_report": [os.path.expanduser("~/.openclaw/logs/backup-local.log"), os.path.expanduser("~/.openclaw/logs/backup-git.log"), os.path.expanduser("~/.openclaw/workspace/backup")],
        "self_reflection": [os.path.join(CONFIDENCE_DIR)],
        "pre_market_refresh": [os.path.join(PROJECT_ROOT, "cache", "pre_market")],
        "pre_market_decision": [os.path.join(PROJECT_ROOT, "reports", "pre_market")],
        "trade_execution_0930": [os.path.join(PROJECT_ROOT, "data", "execution_log.jsonl")],  # 单文件
        "blackswan_review": [os.path.expanduser("~/.openclaw/workspace/memory/black-swan")],
        "health_check": [os.path.join(CACHE_DIR, "progress")],
        "dsl_backup": [os.path.expanduser("~/.openclaw/workspace/backup"), os.path.expanduser("~/.openclaw/logs/backup-local.log"), os.path.expanduser("~/.openclaw/logs/backup-git.log"), os.path.join(CACHE_DIR, "progress")],
        "pre_market_plan": [os.path.join(CACHE_DIR, "planned_trades.json")],  # 单文件
        # v4.5.23 fix: intraday_monitor和stop_loss_monitor各时段均有独立progress文件
        "intraday_monitor_0945": [os.path.join(CACHE_DIR, "progress")],
        "intraday_monitor_1030": [os.path.join(CACHE_DIR, "progress")],
        "intraday_monitor_1100": [os.path.join(CACHE_DIR, "progress")],
        "intraday_monitor_1330": [os.path.join(CACHE_DIR, "progress")],
        "intraday_monitor_1430": [os.path.join(CACHE_DIR, "progress")],
        "stop_loss_monitor": [os.path.join(CACHE_DIR, "progress")],
        "closing_daily_report": [os.path.join(CACHE_DIR, "daily_predict.json")],
        "weekly_pool_refresh": [os.path.join(CONFIG_DIR)],
        "weekly_model_review": [os.path.join(MODELS_DIR)],
        "weekly_review": [os.path.join(PROJECT_ROOT, "reports")],
        # v4.5.17: 新增LPPL和auto_verify
        "lppl_morning": [os.path.join(CACHE_DIR, "reports")],
        "lppl_full": [os.path.join(CACHE_DIR, "reports")],
        "auto_verify": [os.path.join(CACHE_DIR, "reports")],
    }

    # 共享目录的文件名前缀映射 (避免跨任务记录污染)
    _TASK_FILE_PREFIX = {
        "lppl_morning": "lppl_daily",  # lppl_daily_*.json
    }
    _shared_dirs = {
        os.path.normpath(os.path.join(CACHE_DIR, "progress")),
        os.path.normpath(os.path.join(CACHE_DIR, "reports")),
    }

    # 统一日志目录: data/task_logs/{task_id}/
    task_log_dir = os.path.join(PROJECT_ROOT, "data", "task_logs", task_id)
    
    dirs = report_dirs.get(task_id, [])
    if not dirs:
        # 默认: 从 unified task_logs 目录读取
        if os.path.exists(task_log_dir):
            for fname in sorted(os.listdir(task_log_dir), reverse=True)[:30]:
                if not fname.endswith(".json"):
                    continue
                data = safe_read_json(os.path.join(task_log_dir, fname))
                if not data:
                    continue
                reports.append({
                    "id": fname.replace(".json", ""),
                    "type": "task_log",
                    "task_id": task_id,
                    "status": data.get("status", "unknown"),
                    "message": data.get("message", ""),
                    "started_at": data.get("started_at", ""),
                    "completed_at": data.get("completed_at", ""),
                    "raw": data.get("detail", {}),
                })
        if reports:
            return reports[:30]
        
        # 退而: 从 progress 文件记录 (v4.5.23 fix: 按mtime排序而非按字母取前20, 修复intraday/stop_loss等任务查不到报告的问题)
        if os.path.exists(progress_dir):
            for fname in sorted(os.listdir(progress_dir),
                                key=lambda f: os.path.getmtime(os.path.join(progress_dir, f)), reverse=True)[:50]:
                if not fname.endswith(".json"):
                    continue
                if task_id not in fname:
                    continue
                data = safe_read_json(os.path.join(progress_dir, fname))
                if not data:
                    continue
                reports.append({
                    "id": fname.replace(".json", ""),
                    "type": "progress",
                    "task_id": task_id,
                    "status": data.get("status", "unknown"),
                    "message": data.get("message", ""),
                    "started_at": data.get("started_at", ""),
                    "completed_at": data.get("completed_at", ""),
                    "raw": {k: v for k, v in data.items()
                            if k not in ["task_id", "status", "message", "started_at", "completed_at"]},
                })
        return reports[:30]

    # 扫描报告目录
    for d in dirs:
        if not os.path.exists(d):
            continue
        # 单文件路径: 直接读取
        if os.path.isfile(d):
            fpath = d
            fname = os.path.basename(d)
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            if fname.endswith(".json"):
                data = safe_read_json(fpath)
                if data:
                    summary = _extract_report_summary(task_id, data)
                    raw = {k: str(v)[:200] for k, v in data.items() if k not in ["metrics", "details"]}
                    reports.append({
                        "id": fname.replace(".json", ""), "type": "task_data", "task_id": task_id,
                        "status": "completed", "message": summary, "summary": summary,
                        "started_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "completed_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "raw": raw,
                    })
            elif fname.endswith(".jsonl"):
                # JSON Lines: 读取行数与最后一条
                try:
                    with open(fpath, "r") as f:
                        lines = [l.strip() for l in f if l.strip()]
                    total_lines = len(lines)
                    summary = f"交易日志: {total_lines}条记录"
                    raw = {"total_lines": total_lines, "last_entry": lines[-1][:200] if lines else ""}
                    reports.append({
                        "id": fname.replace(".jsonl", ""), "type": "task_data", "task_id": task_id,
                        "status": "completed", "message": summary, "summary": summary,
                        "started_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "completed_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "raw": raw,
                    })
                except Exception:
                    pass
            elif fname.endswith(".md"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read(500)
                    summary = content.split("\n")[0].lstrip("#").strip()[:120]
                    reports.append({
                        "id": fname.replace(".md", ""), "type": "task_data", "task_id": task_id,
                        "status": "completed", "message": summary, "summary": summary,
                        "started_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "completed_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "raw": {"preview": content[:200]},
                    })
                except Exception:
                    pass
            elif fname.endswith(".log"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        lines = [l.strip() for l in f if l.strip()]
                    # 取最后5行作为摘要
                    last5 = lines[-5:] if len(lines) >= 5 else lines
                    summary = " | ".join(l.split("]", 1)[-1].strip() for l in last5)[:200]
                    raw_lines = lines[-20:] if len(lines) >= 20 else lines
                    reports.append({
                        "id": fname.replace(".log", ""), "type": "backup_log", "task_id": task_id,
                        "status": "completed", "message": summary, "summary": summary,
                        "started_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "completed_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "raw": {"lines": raw_lines, "total_lines": len(lines)},
                    })
                except Exception:
                    pass
            continue
        # 目录路径: 遍历文件
        for fname in sorted(os.listdir(d), reverse=True):
            # v4.5.17: 共享目录按task_id过滤，防止跨任务记录污染
            if os.path.normpath(d) in _shared_dirs:
                _prefix = _TASK_FILE_PREFIX.get(task_id, task_id)
                if _prefix not in fname.lower():
                    continue
            if os.path.isdir(os.path.join(d, fname)):
                continue
            fpath = os.path.join(d, fname)
            try:
                stat = os.stat(fpath)
            except OSError:
                continue
            # 支持 .json 和 .md 文件
            if fname.endswith(".json"):
                data = safe_read_json(fpath)
                if not data:
                    continue
                summary = _extract_report_summary(task_id, data)
                if isinstance(data, dict):
                    raw = {k: str(v)[:200] for k, v in data.items() if k not in ["metrics", "details"]}
                else:
                    raw = {"total_items": len(data) if isinstance(data, list) else 0,
                           "preview": str(data[:3])[:200] if isinstance(data, list) else str(data)[:200]}
            elif fname.endswith(".md"):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read(500)
                    first_line = content.split("\n")[0].lstrip("#").strip()
                    summary = first_line[:120] or fname
                    raw = {"preview": content[:200].replace("\n", " ")}
                except Exception:
                    summary = fname
                    raw = {}
            elif fname.endswith(".tar.gz"):
                size_mb = stat.st_size / 1024 / 1024
                # 从文件名提取日期: dsl_backup_YYYYMMDD_HHMM.tar.gz
                date_part = fname.replace("dsl_backup_", "").replace(".tar.gz", "")
                summary = f"备份包 {date_part[:15]} ({size_mb:.0f}MB)"
                raw = {"filename": fname, "size_mb": round(size_mb, 1),
                       "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")}
            else:
                continue
            reports.append({
                "id": fname.replace(".json", "").replace(".md", ""),
                "type": d.split("/")[-1] if "/" in d else os.path.basename(d),
                "task_id": task_id,
                "status": "completed",
                "message": summary,
                "started_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "completed_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "summary": summary,
                "raw": raw,
            })
            if len(reports) >= 20:
                break

    # ── 源3: 进度文件 (universal fallback for ALL tasks) ──
    if os.path.exists(progress_dir):
        all_progress_files = sorted(os.listdir(progress_dir), reverse=True)
        # 精确匹配 + 模糊匹配
        for fname in all_progress_files[:50]:
            if not fname.endswith(".json"):
                continue
            # 先精确匹配文件名中的task_id，再用模糊匹配
            if task_id not in fname:
                # 模糊匹配：提取文件名中的核心task keyword
                fname_no_ext = fname.replace(".json", "")
                # 检查是否接近（如intraday_monitor匹配intraday_monitor_1100等）
                fname_parts = fname_no_ext.split("_")
                task_parts = task_id.split("_")
                if len(task_parts) >= 2:
                    # 检查前面的关键部分是否匹配
                    core_key = "_".join(task_parts[:2]) if len(task_parts) >= 3 else task_parts[0]
                    if core_key not in fname_no_ext:
                        continue
                else:
                    continue
            data = safe_read_json(os.path.join(progress_dir, fname))
            if not data:
                continue
            reports.append({
                "id": fname.replace(".json", ""),
                "type": "progress",
                "task_id": task_id,
                "status": data.get("status", "unknown"),
                "message": data.get("message", ""),
                "started_at": data.get("started_at", ""),
                "completed_at": data.get("completed_at", ""),
                "summary": data.get("message", ""),
                "raw": {k: str(v)[:200] for k, v in data.items()
                        if k not in ["task_id", "task_name", "status", "message", "started_at", "completed_at", "updated_at", "progress"]},
            })

    return reports[:30]


def _extract_report_summary(task_id: str, data: Dict) -> str:
    """从报告数据中提取摘要"""
    if not isinstance(data, dict):
        return str(data)[:120]
    if task_id == "batch_train":
        # 优先从 performance 字段读取
        perf = data.get("performance", data.get("summary", data.get("walk_forward", {})))
        if isinstance(perf, dict):
            ann = perf.get("annual_return_pct", perf.get("annual_return", "?"))
            sharpe = perf.get("sharpe_ratio", "?")
            win = perf.get("win_rate_pct", "")
            dd = perf.get("max_drawdown_pct", "")
            parts = [f"年化: {_fmt(ann)}", f"夏普: {_fmt(sharpe)}"]
            if win: parts.append(f"胜率: {_fmt(win)}%")
            if dd: parts.append(f"回撤: {_fmt(dd)}%")
            return ", ".join(parts)
        return str(perf)[:120]
    elif task_id in ("batch_predict", "batch_predict_0900", "batch_predict_1700"):
        preds = data.get("predictions", data.get("results", []))
        if isinstance(preds, dict):
            preds = list(preds.values())
        total = len(preds) if isinstance(preds, list) else 0
        high_conf = data.get("high_confidence", data.get("high_confidence_h5d", "?"))
        cross = data.get("cross_confirmed", "?")
        h20d_buy = data.get("h20d_buy", data.get("buy_signals", "?"))
        h20d_sell = data.get("h20d_sell", data.get("sell_signals", "?"))
        parts = [f"标的: {total}只"]
        if high_conf != "?": parts.append(f"高信度: {high_conf}")
        if cross != "?": parts.append(f"交叉确认: {cross}")
        if h20d_buy != "?": parts.append(f"h20d买{h20d_buy}/卖{h20d_sell}")
        return " | ".join(parts)
    elif task_id == "health_check":
        errors = data.get("total_errors", data.get("errors", 0))
        return f"错误: {errors}, 状态: {data.get('health', '?')}"
    elif task_id == "closing_daily_report":
        preds_or_stocks = data.get("predictions", data.get("stocks", []))
        if isinstance(preds_or_stocks, dict):
            preds_or_stocks = list(preds_or_stocks.values())
        if not isinstance(preds_or_stocks, list):
            preds_or_stocks = []
        stocks = len(preds_or_stocks)
        buys = len([p for p in preds_or_stocks if isinstance(p, dict) and p.get("signal") == "buy"])
        sells = len([p for p in preds_or_stocks if isinstance(p, dict) and p.get("signal") == "sell"])
        return f"信号: {buys}买/{sells}卖, 共{stocks}只"
    elif task_id == "blackswan_review":
        sev = data.get("scan_summary", {}).get("max_severity", data.get("severity", "?"))
        total = data.get("scan_summary", {}).get("total_events_detected", "?")
        return f"严重度: {sev}/10, 事件: {total}"
    else:
        # 通用: 取第一个非空字符串值
        for k in ["summary", "message", "result", "status"]:
            v = data.get(k, "")
            if v and isinstance(v, str):
                return v[:120]
        return f"{len(data)}字段"


def _fmt(val) -> str:
    """格式化数值"""
    if val is None or val == "?":
        return "?"
    try:
        f = float(val)
        if abs(f) > 100:
            return f"{f:.1f}"
        elif abs(f) > 1:
            return f"{f:.2f}"
        else:
            return f"{f:.4f}"
    except (ValueError, TypeError):
        return str(val)[:20]


# ── 全量数据聚合 ──

def get_full_dashboard() -> Dict[str, Any]:
    """获取Dashboard全量数据"""
    return {
        "status": get_system_status(),
        "pool": get_stock_pool(),
        "predictions": get_predictions(),
        "calibration": get_calibration(),
        "blackswan": get_blackswan(),
        "portfolio": get_portfolio(),
        "paperTrader": get_paper_trader_sync(),  # v4.6.9h.2: /api/full 含完整交易数据, 消除前端异步补丁
        "trading": get_trading_ledger(),
        "progress": get_progress(),
        "pipeline": get_pipeline_timeline(),
        "adaptive_params": get_adaptive_params(),
        "attribution": get_attribution(),  # v4.5.7: 绩效归因
        "timestamp": datetime.now().isoformat()
    }


def get_attribution() -> dict:
    """v4.5.7: 从adaptive_params读取绩效归因"""
    try:
        path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        import yaml
        with open(path) as f:
            params = yaml.safe_load(f) or {}
        attr = params.get("_performance_attribution", {})
        from core.risk_manager import RiskManager
        risk = RiskManager()
        return {
            "last_update": attr.get("last_update", ""),
            "stock_selection": attr.get("stock_selection", 0),
            "timing": attr.get("timing", 0),
            "sector_allocation": attr.get("sector_allocation", 0),
            "cost": attr.get("cost", 0),
            "total_pnl": attr.get("total", 0),
            "win_rate": attr.get("win_rate", 0),
            "avg_hold_days": attr.get("avg_hold_days", 0),
            "risk_enabled": risk.enabled,
        }
    except Exception:
        return {"last_update": "", "stock_selection": 0, "timing": 0}


# ── P2-2: 回测对比数据 ──
def get_backtest_comparison() -> list:
    """读取所有回测报告并返回性能对比时间序列"""
    import glob
    results = []
    bt_dir = os.path.join(PROJECT_ROOT, "reports", "backtest")
    pattern = os.path.join(bt_dir, "walkforward_*.json")
    files = sorted(glob.glob(pattern))
    for f in files[-30:]:  # 最多取最近30次
        try:
            data = safe_read_json(f)
            if not data or not data.get("performance"):
                continue
            perf = data["performance"]
            bt_time = data.get("backtest_time", "")
            results.append({
                "date": bt_time[:16] if bt_time else os.path.basename(f)[12:22],
                "total_return_pct": round(float(perf.get("total_return_pct", 0)), 1),
                "annual_return_pct": round(float(perf.get("annual_return_pct", 0)), 1),
                "max_drawdown_pct": round(float(perf.get("max_drawdown_pct", 0)), 1),
                "sharpe_ratio": round(float(perf.get("sharpe_ratio", 0)), 2),
                "win_rate_pct": round(float(perf.get("win_rate_pct", 0)), 1),
                "profit_factor": round(float(perf.get("profit_factor", 0)), 2),
                "trade_count": len(data.get("trades", [])),
            })
        except Exception:
            continue
    results.sort(key=lambda r: r["date"])
    return results[-20:]  # 最多返回20条


def generate_cron_status():
    """Run openclaw cron list and save to cron_status.json"""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            with open(os.path.join(DATA_DIR, "cron_status.json"), "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
    except Exception as e:
        pass
    return False


if __name__ == "__main__":
    import pprint
    data = get_full_dashboard()
    print("=== DSL Dashboard Data ===")
    print(f"Status: {json.dumps(data['status'], ensure_ascii=False, indent=2)[:500]}")
    print(f"Pool: {data['pool']['total']} stocks")
    print(f"Predictions: {data['predictions']['total']} signals")
    print(f"Calibration: {data['calibration']['summary']}")
    print(f"Portfolio: {data['portfolio']['total_positions']} positions")
    print(f"Trades: {data['trading']['total']} total")
