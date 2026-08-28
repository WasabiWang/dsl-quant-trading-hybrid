#!/usr/bin/env python3
"""
batch_train.py — DSL v4.5.1 分批模型训练（真实实现）
调度时间: 工作日 02:40
功能: 从 master_stock_pool.yaml 读取股票池，分批调用 train_predictor_v3 训练 LightGBM+XGBoost 模型
输出: models/{code}/lightgbm.pkl, xgboost.pkl, scaler.pkl
"""
import os, sys, json, time, gc, atexit, signal
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# v4.5.20: 加载 .env.local（统一密钥文件），确保 MAIRUI_LICENCE 等API密钥可用
try:
    from dotenv import load_dotenv
    _p = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _env_path = os.path.join(_p, ".env")
    _env_local = os.path.join(_p, ".env.local")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
    if os.path.exists(_env_local):
        load_dotenv(_env_local, override=True)
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import yaml
import threading

# v4.5.18 P1-5: BATCH_SIZE从6→12, MAX_WORKERS从3→4加速消费重训队列(16只排队中)
BATCH_SIZE = 12
MAX_WORKERS = 1  # v4.6.4: 从4→2→1缓解OOM
QUEUE_ONLY = "--queue-only" in sys.argv
SKIP_HEAVY_POST = "--skip-heavy-post" in sys.argv or QUEUE_ONLY
MAX_STOCKS = None
if "--max-stocks" in sys.argv:
    try:
        MAX_STOCKS = int(sys.argv[sys.argv.index("--max-stocks") + 1])
    except Exception:
        MAX_STOCKS = None
# v4.5.17 P0-1: 线程安全锁 — 保护 adaptive_params 模块级全局变量写入
_train_globals_lock = threading.Lock()

POOL_PATH = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
ADAPTIVE_PATH = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")


def get_stock_pool():
    """从 master_stock_pool.yaml 读取标的（去重）
    v4.6.9f P2-1b: degraded停训机制 — 精度<50%标记的标的跳过训练
    """
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    seen = set()
    stocks = []
    degraded = []
    for s in data.get("master_pool", []):
        symbol = s.get("symbol", "")
        if symbol and symbol not in seen:
            seen.add(symbol)
            if s.get("degraded", False):
                degraded.append({"symbol": symbol, "name": s.get("name", symbol),
                                 "tier": s.get("tier", "core"), "degraded": True})
            else:
                stocks.append({"symbol": symbol, "name": s.get("name", symbol),
                               "tier": s.get("tier", "core"), "degraded": False})
    if degraded:
        names = ", ".join(f'{d["symbol"]}({d["name"]})' for d in degraded)
        print(f"⏸️ degraded停训({len(degraded)}只): {names}")
    print(f"📋 股票池: {len(stocks)} 只标的（去重, 跳过{len(degraded)}只degraded）")
    return stocks


def load_adaptive_overrides():
    """从 adaptive_params.yaml 读取反馈驱动的训练参数覆盖
    
    闭环: 06:00 feedback_controller → 写入 adaptive_params.yaml → 本函数读取
    覆盖范围: 正则化参数/树深度/估计器数量/黑名单
    """
    overrides = {}
    if not os.path.exists(ADAPTIVE_PATH):
        print("ℹ️ adaptive_params.yaml 不存在，使用默认训练参数")
        return overrides
    try:
        with open(ADAPTIVE_PATH, "r", encoding="utf-8") as f:
            adaptive = yaml.safe_load(f)
        if not adaptive:
            return overrides
        
        # 模型参数覆盖
        model_cfg = adaptive.get("model", {})
        if model_cfg.get("n_estimators"):
            overrides["n_estimators"] = int(model_cfg["n_estimators"])
        if model_cfg.get("max_depth"):
            overrides["max_depth"] = int(model_cfg["max_depth"])
        if model_cfg.get("reg_alpha"):
            overrides["reg_alpha"] = float(model_cfg["reg_alpha"])
        if model_cfg.get("reg_lambda"):
            overrides["reg_lambda"] = float(model_cfg["reg_lambda"])
        if model_cfg.get("min_child_samples"):
            overrides["min_child_samples"] = int(model_cfg["min_child_samples"])
        if model_cfg.get("learning_rate"):
            overrides["learning_rate"] = float(model_cfg["learning_rate"])
        
        # 风险参数
        risk_cfg = adaptive.get("risk", {})
        if risk_cfg.get("black_swan_active"):
            overrides["black_swan_active"] = True
            overrides["position_cap"] = risk_cfg.get("black_swan_position_ratio", 0.2)
        
        # 交易参数
        trading_cfg = adaptive.get("trading", {})
        if trading_cfg.get("buy_threshold") is not None:
            overrides["buy_threshold"] = float(trading_cfg["buy_threshold"])
        if trading_cfg.get("sell_threshold") is not None:
            overrides["sell_threshold"] = float(trading_cfg["sell_threshold"])
        
        if overrides:
            print(f"🔄 已加载 {len(overrides)} 个自适应参数覆盖: {list(overrides.keys())}")
        else:
            print("ℹ️ adaptive_params.yaml 无模型参数覆盖")
    except Exception as e:
        print(f"⚠️ 读取 adaptive_params.yaml 失败: {e}")
    return overrides


def train_one(code: str, name: str, adaptive_overrides: dict = None, tier: str = "core") -> dict:
    """训练单只股票，支持 adaptive_params 覆盖 + v4.5.9分级特征"""
    _t0 = __import__("time").time()
    try:
        from scripts.train_predictor_v3 import train_single_stock
        import scripts.train_predictor_v3 as tp
        # 将自适应覆盖注入 train_predictor_v3 的全局参数
        if adaptive_overrides:
            if "n_estimators" in adaptive_overrides:
                tp.N_ESTIMATORS_DEFAULT = adaptive_overrides["n_estimators"]
            if "max_depth" in adaptive_overrides:
                tp.MAX_DEPTH_DEFAULT = adaptive_overrides["max_depth"]
            if "reg_alpha" in adaptive_overrides:
                tp.REG_ALPHA_DEFAULT = adaptive_overrides["reg_alpha"]
            if "reg_lambda" in adaptive_overrides:
                tp.REG_LAMBDA_DEFAULT = adaptive_overrides["reg_lambda"]
            if "min_child_samples" in adaptive_overrides:
                tp.MIN_CHILD_SAMPLES_DEFAULT = adaptive_overrides["min_child_samples"]
        # v4.5.5 S6: 当有adaptive_overrides时跳过Optuna，避免覆盖已调优的参数
        tp.SKIP_OPTUNA = bool(adaptive_overrides)
        result = train_single_stock(code, name, tier=tier)
        # v4.5.9: 审计日志
        ok = result and "error" not in result
        try:
            from common.audit import log
            log(source="cron:batch_train", action="model:train",
                target=code, detail={"accuracy": result.get("direction_accuracy", 0) if ok else 0},
                result="success" if ok else f"failed:{result.get('error', 'unknown')[:80]}")
        except Exception: pass
        if ok:
            _elapsed = round(__import__("time").time() - _t0, 1)
            return {"code": code, "status": "success", "elapsed": _elapsed, **result}
        _elapsed = round(__import__("time").time() - _t0, 1)
        return {"code": code, "status": "failed", "error": result.get("error", "unknown"), "elapsed": _elapsed}
    except Exception as e:
        try:
            from common.audit import log
            log(source="cron:batch_train", action="model:train", target=code,
                result=f"failed:{str(e)[:80]}")
        except Exception: pass
        return {"code": code, "status": "failed", "error": str(e)}


def load_retrain_priorities():
    """v4.5.3c: 从 adaptive_params.yaml 读取校准反馈的重训优先级
    
    闭环: feedback_controller.update_from_calibration()
          → calibration_feedback.analyze() → write_to_adaptive_params()
          → 本函数消费
    
    Returns:
        [{"symbol": str, "priority": str, "current_accuracy": float}, ...]
    """
    if not os.path.exists(ADAPTIVE_PATH):
        return []
    try:
        with open(ADAPTIVE_PATH, "r", encoding="utf-8") as f:
            adaptive = yaml.safe_load(f)
        calib = adaptive.get("calibration", {})
        plan = calib.get("retrain_plan", {})
        return plan.get("priority_stocks", [])
    except Exception as e:
        print(f"⚠️ 读取重训优先级失败: {e}")
        return []


def get_portfolio_positions() -> set:
    """v4.5.9: 读取纸交易持仓股票代码集合，用于最高优先级调度"""
    try:
        import sqlite3
        db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
        if not os.path.exists(db_path):
            return set()
        conn = sqlite3.connect(db_path)
        codes = {r[0] for r in conn.execute("SELECT stock_code FROM positions WHERE quantity > 0")}
        conn.close()
        return codes
    except Exception:
        return set()


def prioritize_stocks(stocks: list, priorities: list, portfolio_codes: set = None) -> list:
    """按重训优先级重新排序股票列表

    优先级 (v4.5.9 强化):
      0. PORTFOLIO — 持仓股票最高优先（风控要求）
      1. critical — 校准反馈紧急重训
      2. high — 校准反馈高优先
      3. medium — 中优先
      4. low — 低优先
      5. 无优先级按原序
    """
    if portfolio_codes is None:
        portfolio_codes = get_portfolio_positions()
    priority_map = {p["symbol"]: p for p in priorities} if priorities else {}
    priority_order = {"critical": 1, "high": 2, "medium": 3, "low": 4}

    def sort_key(s):
        sym = s["symbol"]
        # PORTFOLIO: 最高优先级 (0)
        if sym in portfolio_codes:
            return (0, 0)
        p = priority_map.get(sym, {})
        return (priority_order.get(p.get("priority", ""), 9),
                -p.get("current_accuracy", 0))

    sorted_stocks = sorted(stocks, key=sort_key)

    # 打印排序结果
    portfolio_first = [s for s in sorted_stocks if s["symbol"] in portfolio_codes]
    if portfolio_first:
        print(f"📦 持仓优先: {len(portfolio_first)}只排最前")
        for s in portfolio_first:
            print(f"    {s['symbol']} {s['name']:8s} [PORTFOLIO] 强制优先训练")
    reordered = [s for s in sorted_stocks
                 if s["symbol"] in priority_map and s["symbol"] not in portfolio_codes]
    if reordered:
        print(f"🎯 校准反馈: {len(reordered)}只优先重训")
        for s in sorted_stocks:
            if s["symbol"] in priority_map and s["symbol"] not in portfolio_codes:
                p = priority_map[s["symbol"]]
                print(f"    {s['symbol']} {s['name']:8s} [{p['priority']}] acc={p['current_accuracy']:.1%}")

    return sorted_stocks


def _sync_accuracy_to_calibration(results: list):
    """v4.5.5 S6: 训练后将新精度同步到prediction_calibration.json
    避免训练跑了但calibration未更新, 下周期仍显示急迫重训
    """
    try:
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        if not os.path.exists(cal_path):
            return
        with open(cal_path, "r") as f:
            cal = json.load(f)
        sa = cal.get("stock_accuracy", {})
        updated = 0
        for r in results:
            code = r.get("symbol", "")
            new_acc = r.get("direction_accuracy", 0)
            if not code:
                continue
            if code not in sa:
                sa[code] = {"name": r.get("name", code), "accuracies": [], "count": 0}
            s = sa[code]
            # v4.7.2 P2-3: 按training_hash去重 — 同一训练版本只记最后一次(与feedback_controller对齐),
            # 修掉尾部x3~x8连续重复值(模型收敛后每日训练精度相同→sparkline失真)
            _hash = r.get("model_hash", datetime.now().strftime("%Y%m%d"))
            if _hash != s.get("last_training_hash", ""):
                s["accuracies"].append(round(new_acc, 4))
                # P2: 保留最近50条，防止无界增长
                if len(s["accuracies"]) > 50:
                    s["accuracies"] = s["accuracies"][-50:]
            s["last_accuracy"] = round(new_acc, 4)
            s["mean_accuracy"] = round(sum(s["accuracies"]) / len(s["accuracies"]), 4) if s["accuracies"] else 0
            s["count"] = len(s["accuracies"])
            s["last_training_hash"] = _hash
            s["last_training_date"] = datetime.now().strftime("%Y-%m-%d")
            updated += 1
        cal["stock_accuracy"] = sa
        cal["last_updated"] = datetime.now().isoformat()
        with open(cal_path, "w") as f:
            json.dump(cal, f, indent=2, ensure_ascii=False)
        print(f"📊 训练后同步: {updated}只精度更新到prediction_calibration.json")
        
        # 同时更新adaptive_params.yaml的retrain_plan
        try:
            from core.calibration_feedback import CalibrationFeedback
            engine = CalibrationFeedback()
            engine.calibration = cal  # 用更新后的数据
            plan = engine.write_to_adaptive_params()
            print(f"📊 校准闭环: {plan['summary']}")
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ calibration同步跳过: {e}")


def _trigger_auto_tune_if_needed(results: list):
    """v4.5.5 S6: 训练后检查是否仍有低精度标的,自动触发参数调优"""
    try:
        with open(os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")) as f:
            ap = yaml.safe_load(f)
        auto_tune = ap.get("auto_tune", {})
        if not auto_tune.get("enabled", False):
            return
        
        # P0 FIX: 废除自动参数调优（原逻辑在测试集上挖掘最佳参数，属严重过拟合）
        # 低于45%的标的改为标记降级+报警，不再尝试任意参数组合
        still_low = [r for r in results if r.get("direction_accuracy", 1) < 0.45]
        if still_low:
            codes_still_low = [r["symbol"] for r in still_low if "symbol" in r]
            print(f"\n⚠️ {len(codes_still_low)}只训练后仍低于45%: {codes_still_low}")
            print(f"   📉 已标记降级（不再自动调参，防止测试集挖掘）")
            # 写入降级日志供morning_decision参考
            try:
                degrade_path = os.path.join(PROJECT_ROOT, "confidence_data", "degraded_models.json")
                import json as _json
                degraded = {}
                if os.path.exists(degrade_path):
                    with open(degrade_path) as _f:
                        degraded = _json.load(_f)
                for r in still_low:
                    code = r.get("symbol", "")
                    if code:
                        degraded[code] = {
                            "accuracy": r.get("direction_accuracy", 0),
                            "degraded_at": datetime.now().isoformat(),
                            "reason": "训练精度<45%，自动降级"
                        }
                with open(degrade_path, "w") as _f:
                    _json.dump(degraded, _f, indent=2, ensure_ascii=False)
            except Exception:
                pass
        
    except Exception as e:
        print(f"⚠️ 自动调优跳过: {e}")


def main():
    global _checkpoint_state

    # v4.5.20: 注册退出保护（atexit + signal）
    atexit.register(_emergency_checkpoint)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print("=" * 60)
    print("🚀 DSL v4.6.6 分批训练 (增量检查点 + 退出保护)")
    print("=" * 60)
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"每批: {BATCH_SIZE}只 | 并发: {MAX_WORKERS}")
    if QUEUE_ONLY:
        print("模式: 仅消费重训队列 (--queue-only)")
    if MAX_STOCKS:
        print(f"范围限制: 最多训练 {MAX_STOCKS} 只 (--max-stocks)")

    # v4.5.3d: 进度追踪
    tracker = None
    try:
        from common.progress_tracker import ProgressTracker
        tracker = ProgressTracker("batch_train", total_steps=1)
        _checkpoint_state["tracker"] = tracker
    except ImportError:
        pass

    stocks = get_stock_pool()
    adaptive_overrides = load_adaptive_overrides()
    
    # v4.5.9b: 清理retrain_queue中不在当前池的残留条目
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        _mgr = get_retrain_queue_manager()
        _reconciled = _mgr.reconcile_completed_from_training_status()
        _reset = _mgr.reset_stale_training(max_age_hours=6)
        if _reconciled:
            print(f"🔁 重训队列对账: {_reconciled}条已由training_status确认完成")
        if _reset:
            print(f"🛟 重训队列恢复: {_reset}条超时training已回到queued")
        pool_symbols = {s["symbol"] for s in stocks}
        queue = _mgr._load_lock()
        stale = [q for q in queue if q.get("status") == "queued" and q.get("symbol") not in pool_symbols]
        if stale:
            _cleaned = 0
            for item in queue:
                if item.get("status") == "queued" and item.get("symbol") not in pool_symbols:
                    item["status"] = "expired"
                    item["trained_at"] = datetime.now().isoformat()
                    item["reason"] = f"{item.get('reason','')} | 不在当前池"
                    _cleaned += 1
            if _cleaned:
                _mgr._save_lock(queue)
                print(f"🧹 重训队列清理: {_cleaned}条不在池的条目已过期")
    except Exception as e:
        print(f"⚠️ 队列清理跳过: {e}")
    
    # v4.5.3c: 加载校准反馈的重训优先级，优先训练退化标的
    priorities = load_retrain_priorities()
    # v4.5.9: 读取持仓，强制持仓股票最高训练优先级
    portfolio_codes = get_portfolio_positions()
    if portfolio_codes:
        print(f"📦 当前持仓: {len(portfolio_codes)}只 — 将排在训练队列最前面")
    stocks = prioritize_stocks(stocks, priorities, portfolio_codes)
    
    # v4.6.x P0-1: 消费retrain_queue.json — 标记出队
    _retrain_batch = []  # {symbol: str} 本次训练中来自队列的标的
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        _rmgr = get_retrain_queue_manager()
        _sync_low = _rmgr.sync_low_accuracy_from_calibration()
        if _sync_low.get("added", 0):
            print(f"📋 低精度闭环入队: {_sync_low['added']}只新加入 ({_sync_low.get('critical', 0)} critical)")
        _qsummary = _rmgr.get_summary()
        print(f"📋 重训队列: {_qsummary['queued']}排队/{_qsummary['training']}训练中/{_qsummary['completed']}已完成")
        queue_batch_size = MAX_STOCKS or (50 if not QUEUE_ONLY else BATCH_SIZE)
        _queue_items = _rmgr.dequeue(batch_size=queue_batch_size)
        if _queue_items:
            print(f"🔄 消费重训队列: {len(_queue_items)}只出队")
            _retrain_batch = [{"symbol": q["symbol"], "priority": q.get("priority", "medium")} for q in _queue_items]
            # 将这些出队标的强制排在股票池最前面
            _queue_symbols = {q["symbol"] for q in _retrain_batch}
            _queue_stocks = [s for s in stocks if s["symbol"] in _queue_symbols]
            _other_stocks = [s for s in stocks if s["symbol"] not in _queue_symbols]
            stocks = _queue_stocks if QUEUE_ONLY else (_queue_stocks + _other_stocks)
            if QUEUE_ONLY:
                print(f"   本次仅训练队列标的: {len(_queue_stocks)}只")
            else:
                print(f"   前排{len(_queue_stocks)}只来自队列 + 后排{len(_other_stocks)}只正常排序")
        elif QUEUE_ONLY:
            print("✅ 重训队列无待处理条目，跳过训练")
            return 0
    except Exception as e:
        print(f"⚠️ 重训队列消费跳过: {e}")

    if MAX_STOCKS:
        stocks = stocks[:MAX_STOCKS]
    
    all_results = []

    for i in range(0, len(stocks), BATCH_SIZE):
        batch = stocks[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(stocks) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n📦 第{batch_num}/{total_batches}批 ({len(batch)}只)")

        batch_results = []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(batch))) as ex:
            futures = {ex.submit(train_one, s["symbol"], s["name"], adaptive_overrides, s.get("tier", "core")): s for s in batch}
            for f in as_completed(futures):
                r = f.result()
                batch_results.append(r)
                if r["status"] == "success":
                    acc = r.get("direction_accuracy", 0)
                    print(f"  ✅ {r['code']:6s} 方向精度={acc:.1%}")
                else:
                    print(f"  ❌ {r['code']:6s} {r.get('error','?')[:50]}")

        all_results.extend(batch_results)
        success_count = sum(1 for r in batch_results if r["status"] == "success")

        # 成功率 < 80% 自动重试
        if len(batch) > 1 and success_count / len(batch) < 0.8:
            print(f"  ⚠️ 批次成功率 {success_count}/{len(batch)}，重试失败标的...")
            stock_by_code = {s["symbol"]: s for s in batch}
            retry_stocks = [stock_by_code[r["code"]] for r in batch_results
                            if r["status"] == "failed" and r.get("code") in stock_by_code]
            if retry_stocks:
                retry = []
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    rf = {ex.submit(train_one, s["symbol"], s["name"]): s for s in retry_stocks}
                    for f in as_completed(rf):
                        retry.append(f.result())
                all_results.extend(retry)

        # v4.5.20: 每批完成后增量保存检查点（防进程被kill丢失结果）
        _save_checkpoint(all_results, stocks, batch_num)
        if tracker:
            try:
                tracker.update(step=batch_num, total=total_batches,
                               message=f"第{batch_num}/{total_batches}批 ({len(batch)}只)")
            except Exception:
                pass

        gc.collect()
        time.sleep(0.5)

    # 汇总
    total_success = sum(1 for r in all_results if r["status"] == "success")
    _log_timing(all_results)  # P2-13: 训练耗时统计
    print(f"\n{'='*60}")
    print(f"📊 训练完成: {total_success}/{len(stocks)} 成功")
    accuracies = [r.get("direction_accuracy", 0) for r in all_results if r["status"] == "success"]
    if accuracies:
        print(f"   方向精度: 均值{sum(accuracies)/len(accuracies):.1%} | "
              f">50%: {sum(1 for a in accuracies if a > 0.5)}/{len(accuracies)}")

    # v4.5.20: 最终检查点（覆盖增量版本，写入完整数据）
    _save_checkpoint(all_results, stocks, None)

    # Post-processing: 以下步骤可能耗时，但检查点已保存，即使被kill也不会丢数据
    _post_process(all_results, accuracies, tracker, skip_heavy=SKIP_HEAVY_POST)

    # v4.5.3d: 进度追踪完成
    result_code = 0 if total_success / max(len(stocks), 1) >= 0.5 else 1
    if tracker:
        try:
            tracker.complete(f"训练完成: {total_success}/{len(stocks)} 成功",
                           total=len(stocks), success=total_success)
        except Exception:
            pass

    _checkpoint_state["finalized"] = True
    return result_code


def _post_process(all_results, accuracies, tracker, skip_heavy=False):
    """v4.5.20: 训练后处理（独立函数，失败不影响主流程）"""

    today_str = datetime.now().strftime("%Y-%m-%d")

    # v4.5.5 S6: 训练后同步精度到 prediction_calibration.json
    try:
        _sync_accuracy_to_calibration_once(all_results)
    except Exception as e:
        print(f"⚠️ calibration同步跳过: {e}")

    # v4.5.5 S6: 若仍有低精度标的,自动触发参数调优
    try:
        _trigger_auto_tune_if_needed(all_results)
    except Exception as e:
        print(f"⚠️ 自动调优跳过: {e}")

    # v4.5.7: 更新重训队列 — 标记已完成条目
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        mgr = get_retrain_queue_manager()
        completed_in_batch = 0
        failed_in_batch = 0
        for r in all_results:
            code = r.get("symbol", r.get("code", ""))
            if not code:
                continue
            if r["status"] == "success":
                if mgr.mark_completed(code, result=r):
                    completed_in_batch += 1
            elif r["status"] == "failed":
                if mgr.mark_failed(code, r.get("error", "unknown")):
                    failed_in_batch += 1
        if completed_in_batch or failed_in_batch:
            print(f"📋 重训队列: {completed_in_batch}只完成/{failed_in_batch}只失败")
            after = mgr.get_summary()
            print(f"   队列状态: {after['queued']}排队/{after['training']}训练中/{after['completed']}已完成/{after['failed']}失败")
    except Exception as e:
        print(f"⚠️ 重训队列更新跳过: {e}")

    if not skip_heavy:
        # v4.5.3c: 自动生成 h5d 增强预测报告（batch_predict 依赖此文件）
        print(f"\n{'='*60}")
        print(f"🔮 生成 h5d 增强预测报告...")
        try:
            import subprocess as sp
            # v4.5.21: PYTHONUNBUFFERED=1 防缓冲输出现象hang
            _enhanced_env = os.environ.copy()
            _enhanced_env["PYTHONUNBUFFERED"] = "1"
            res = sp.run(
                [sys.executable, os.path.join(PROJECT_ROOT, "scripts", "train_predictor_enhanced.py")],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800,
                env=_enhanced_env
            )
            if res.returncode == 0:
                print("✅ h5d 增强预测报告生成完成")
                for line in res.stdout.strip().split("\n"):
                    if line.strip():
                        print(f"   {line.strip()[:120]}")
            else:
                stderr_full = res.stderr.strip()
                print(f"⚠️ h5d 报告生成异常 (exit={res.returncode})")
                if stderr_full:
                    for _line in stderr_full.split("\n")[-10:]:
                        print(f"   ⚠️ {_line[:160]}")
        except sp.TimeoutExpired:
            print("⚠️ h5d 报告生成超时（30分钟）")
        except Exception as e:
            print(f"⚠️ h5d 报告生成失败: {e}")

        # v4.5.5 S6: 训练完v3模型后, 运行enhanced训练(1d/5d/20d)
        print("\n🔧 运行增强训练(多时间框架)...")
        try:
            from scripts.train_predictor_enhanced import main as enhanced_train
            enhanced_train()
        except Exception as e:
            print(f"  ⚠️ 增强训练跳过: {e}")
    else:
        print("\n⏭️ 跳过增强训练后处理 (--skip-heavy-post/--queue-only)")

    # v4.5.6b: 清理重训队列中已完成的条目
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        mgr = get_retrain_queue_manager()
        removed = mgr.clear_completed()
        if removed > 0:
            print(f"🧹 重训队列清理: {removed}条已完成条目已移除")
        resync = mgr.sync_low_accuracy_from_calibration()
        if resync.get("added", 0):
            print(
                f"📋 低精度闭环复核: {resync['added']}只重新入队 "
                f"({resync.get('critical', 0)} critical)"
            )
    except Exception as e:
        print(f"  ⚠️ 队列清理失败: {e}")

    if skip_heavy:
        return

    # v4.5.13: 飞书自报告
    try:
        from common.feishu_utils import send_markdown
        acc_mean = sum(accuracies)/len(accuracies) if accuracies else 0
        acc_gt50 = sum(1 for a in accuracies if a > 0.5) if accuracies else 0
        report = f"""**🧠 模型分批训练 | {datetime.now().strftime('%Y-%m-%d %H:%M')}**
**结果**: {sum(1 for r in all_results if r['status']=='success')}/{len(all_results)} 成功
**方向精度**: 均值{acc_mean:.1%}, >50%: {acc_gt50}/{len(accuracies)}
**版本**: {today_str}"""
        send_markdown(title=f"🧠 模型训练 | {datetime.now().strftime('%Y-%m-%d')}", content=report)
    except Exception:
        pass


# v4.5.20: 全局检查点数据，供 atexit/signal handler 使用
_checkpoint_state = {
    "all_results": [],
    "stocks": [],
    "tracker": None,
    "saved_batch": 0,
    "synced_accuracy_keys": set(),
    "finalized": False,
}


def _accuracy_sync_key(result: dict):
    code = result.get("symbol", result.get("code", ""))
    return (
        code,
        result.get("status", "unknown"),
        round(float(result.get("direction_accuracy", 0) or 0), 6),
        result.get("model_hash", ""),
    )


def _sync_accuracy_to_calibration_once(results: list):
    """Sync each in-process training result to calibration at most once."""
    valid_results = [
        r for r in results
        if r.get("direction_accuracy", 0) > 0
        and abs(r.get("direction_accuracy", 0) - 0.5) > 0.001
    ]
    if not valid_results:
        return
    synced = _checkpoint_state.setdefault("synced_accuracy_keys", set())
    new_results = []
    for r in valid_results:
        key = _accuracy_sync_key(r)
        if key in synced:
            continue
        synced.add(key)
        new_results.append(r)
    if new_results:
        _sync_accuracy_to_calibration(new_results)


def _save_checkpoint(all_results, stocks, batch_num=None):
    """v4.5.20: 增量保存训练结果，确保进程被kill时数据不丢失

    每批训练完成后调用，写入 train_meta.json + training_status.json。
    同时更新 atexit 全局状态，确保异常退出时也能保存。
    """
    global _checkpoint_state
    _checkpoint_state["all_results"] = list(all_results)
    _checkpoint_state["stocks"] = list(stocks)
    _checkpoint_state["saved_batch"] = batch_num or 0

    try:
        meta_path = os.path.join(PROJECT_ROOT, "cache", "train_meta.json")
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        with open(meta_path, "w") as f:
            json.dump({
                "time": datetime.now().isoformat(),
                "total": len(stocks),
                "success": sum(1 for r in all_results if r["status"] == "success"),
                "results": all_results,
                "checkpoint_batch": batch_num,
            }, f, ensure_ascii=False, indent=2)

        # 增量写入 training_status
        training_status_path = os.path.join(PROJECT_ROOT, "cache", "training_status.json")
        training_status = {}
        if os.path.exists(training_status_path):
            try:
                with open(training_status_path, "r") as _f:
                    training_status = json.load(_f)
            except Exception:
                training_status = {}
        now_dt = datetime.now()
        today_str = now_dt.strftime("%Y-%m-%d")
        train_time = now_dt.isoformat()
        for r in all_results:
            code = r.get("symbol", r.get("code", ""))
            if not code:
                continue
            training_status[code] = {
                "last_train_date": today_str,
                "last_train_time": train_time,
                "status": r.get("status", "unknown"),
                "direction_accuracy": r.get("direction_accuracy", 0),
                "model_fresh": r.get("status") == "success",
            }
        training_status["_last_batch_train_date"] = today_str
        training_status["_last_batch_train_time"] = train_time
        with open(training_status_path, "w") as f:
            json.dump(training_status, f, ensure_ascii=False, indent=2)

        if batch_num:
            print(f"💾 检查点已保存 (第{batch_num}批, {len(all_results)}条结果)", flush=True)

        # v4.5.21: 检查点保存后同步校准数据，防止进程被kill时校准数据丢失
        # 只同步有有效 accuracy 的结果（排除默认值 0.5 和 0）
        try:
            _sync_accuracy_to_calibration_once(all_results)
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ 检查点保存失败: {e}", flush=True)


def _emergency_checkpoint():
    """atexit/SIGTERM回调: 进程即将退出时尝试保存最后的检查点
    v4.6 fix: 无论 results 是否为空，都必须更新进度文件状态
    """
    global _checkpoint_state
    if _checkpoint_state.get("finalized"):
        return
    results = _checkpoint_state.get("all_results", [])
    stocks = _checkpoint_state.get("stocks", [])
    tracker = _checkpoint_state.get("tracker")
    
    # 🔧 v4.6: tracker 总是在 main() 中初始化，必须先标记完成
    if tracker and hasattr(tracker, 'complete'):
        try:
            if results:
                total_success = sum(1 for r in results if r.get("status") == "success")
                tracker.complete(
                    f"训练完成(自动补全): {total_success}/{len(stocks)} 成功",
                    total=len(stocks), success=total_success
                )
            else:
                # 进程在第一批训练之前退出，标记失败
                tracker.fail("训练中断: 进程提前退出(可能在模型加载阶段)")
        except Exception:
            pass
    
    if results and stocks:
        try:
            _save_checkpoint(results, stocks, _checkpoint_state.get("saved_batch", 0))
            # v4.5.21: 紧急退出时也同步校准数据
            try:
                _sync_accuracy_to_calibration_once(results)
            except Exception:
                pass
            print(f"\n🛟 紧急检查点已保存 ({len(results)}条结果)", flush=True)
        except Exception:
            pass


def _signal_handler(signum, _frame):
    """捕获 SIGTERM/SIGINT，保存检查点后退出"""
    print(f"\n⚠️ 收到终止信号 (signal={signum})，保存检查点...", flush=True)
    _emergency_checkpoint()
    sys.exit(128 + signum)


def _log_timing(results: list):
    """P2-13: 训练耗时统计"""
    timing_stats = {}
    for r in results:
        elapsed = r.get("elapsed", 0)
        if elapsed <= 0:
            continue
        tier = r.get("tier", "unknown")
        if tier not in timing_stats:
            timing_stats[tier] = {"count": 0, "total": 0.0, "min": float("inf"), "max": 0.0}
        s = timing_stats[tier]
        s["count"] += 1
        s["total"] += elapsed
        s["min"] = min(s["min"], elapsed)
        s["max"] = max(s["max"], elapsed)
    if timing_stats:
        print(f"\n⏱️ 训练耗时统计:")
        for tier in sorted(timing_stats):
            s = timing_stats[tier]
            avg = s["total"] / s["count"] if s["count"] else 0
            print(f"  [{tier}] 共{s['count']}只 | 平均{avg:.1f}s | 最快{s['min']:.1f}s | 最慢{s['max']:.1f}s")


def futures_key(r):
    return {"code": r["code"], "name": r.get("name", r["code"])}




def _verify_progress_completed():
    """v4.5.17: 进程退出后检查进度文件是否已标记完成，如未完成则自动补全
    
    解决: ProgressTracker.complete() 因进程中断/超时未执行，
    导致 Dashboard 显示"任务启动(推定完成)"的问题
    """
    _task_names = ["batch_train"]
    import json, os, glob
    from datetime import datetime
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(project_root, "cache", "progress")
    if not os.path.exists(cache_dir):
        return
    for fname in os.listdir(cache_dir):
        if not fname.endswith(".json"):
            continue
        if not any(fname.startswith(tn) for tn in _task_names):
            continue
        fpath = os.path.join(cache_dir, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            if data.get("status") in ("running", "started"):
                data["status"] = "completed"
                data["message"] = data.get("message", "") + " (退出后自动补全)"
                data["updated_at"] = datetime.now().isoformat()
                with open(fpath, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"  ✅ 进度已补全: {fname}")
        except Exception:
            pass


if __name__ == "__main__":
    _exit_code = main()
    _verify_progress_completed()
    sys.exit(_exit_code)
