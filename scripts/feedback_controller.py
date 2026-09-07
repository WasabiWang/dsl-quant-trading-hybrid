#!/usr/bin/env python3
"""
DSL v4.5.1 反馈控制器 - 所有反馈源的统一入口

读取:
  1. 黑天鹅事件数据 (memory/black-swan/)
  2. 回测绩效 (reports/backtest/walkforward_*.json)
  3. 自我反思教训 (MEMORY.md / LESSONS.md)
  4. 模型训练报告 (reports/predictor/training_*.json)
  5. 每日预测数据 (cache/daily_predict.json)

输出:
  - config/adaptive_params.yaml (动态参数)
  - data/risk_override.json (风险覆盖)
  - data/feedback_log.jsonl (反馈日志)
  - confidence_data/confidence_calibration.json (置信度校准)
  - confidence_data/prediction_calibration.json (预测校准)
  - confidence_data/event_history.json (事件历史)

自动化程度: 所有DSL cron任务可随时调用 update_from_*() 函数
"""
import os, sys, json, math, yaml
from datetime import datetime, timedelta

# 强制代理绕过（akshare/东方财富/麦蕊API等）
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

ADAPTIVE_PARAMS = PROJECT_ROOT / "config" / "adaptive_params.yaml"
RISK_OVERRIDE = PROJECT_ROOT / "data" / "risk_override.json"
FEEDBACK_LOG = PROJECT_ROOT / "data" / "feedback_log.jsonl"
CALIBRATION_DIR = PROJECT_ROOT / "confidence_data"
DAILY_PREDICT = PROJECT_ROOT / "cache" / "daily_predict.json"


def load_adaptive_params() -> dict:
    from common.file_lock import locked_yaml_read
    return locked_yaml_read(ADAPTIVE_PARAMS)


def save_adaptive_params(params: dict):
    """保存adaptive_params.yaml，合并已有字段防止覆盖丢失"""
    params["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    from common.file_lock import locked_yaml_write, locked_yaml_read
    # Read→Merge→Write: 保留调用方未提供的已有字段（如risk块）
    existing = locked_yaml_read(ADAPTIVE_PARAMS, default={})
    # 递归合并，保留已有但新params中没有的key
    def deep_merge(base: dict, overlay: dict) -> dict:
        merged = dict(base)
        for k, v in overlay.items():
            if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
                merged[k] = deep_merge(merged[k], v)
            else:
                merged[k] = v
        return merged
    merged = deep_merge(existing, params)
    locked_yaml_write(ADAPTIVE_PARAMS, merged)


def log_feedback(source: str, action: str, details: dict):
    """记录一条反馈到日志"""
    entry = {"ts": datetime.now().isoformat(), "source": source, "action": action, "details": details}
    with open(FEEDBACK_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ═══════════════ 黑天鹅→风控 ═══════════════
def update_from_black_swan():
    """从黑天鹅事件数据更新风险参数"""
    bs_dir = Path.home() / ".openclaw" / "workspace" / "memory" / "black-swan"
    if not bs_dir.exists():
        return {"status": "no_data"}

    # 找最新分析文件
    files = sorted(bs_dir.glob("analysis-*.json"), reverse=True)
    if not files:
        return {"status": "no_analysis"}

    with open(files[0]) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            return {"status": "parse_error"}

    # 检查分析文件的时效性(超过7天作废)
    file_age = datetime.now() - datetime.fromtimestamp(files[0].stat().st_mtime)
    if file_age > timedelta(days=7):
        # P1-FIX: 过期数据发送告警,不再静默跳过
        try:
            from monitoring.feishu_alert import send_alert, AlertLevel, AlertType
            send_alert(
                level=AlertLevel.WARNING,
                alert_type=AlertType.SYSTEM,
                title="黑天鹅数据过期",
                message=f"黑天鹅分析数据已过期{file_age.days}天(阈值7天),风险监测可能失效!",
                module="feedback_controller",
                metric="data_freshness",
                value=file_age.days,
                threshold=7
            )
        except ImportError:
            print(f"⚠️ 黑天鹅数据已过期{file_age.days}天,但告警模块不可用")
        return {"status": "stale_data", "age_days": file_age.days}

    # 提取严重等级
    severity = 0
    if isinstance(data, dict):
        # 遍历查找最高严重等级
        def find_severity(obj, max_sev=0):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in ("severity", "严重等级", "severity_level"):
                        try:
                            max_sev = max(max_sev, int(v))
                        except (ValueError, TypeError):
                            pass
                    max_sev = find_severity(v, max_sev)
            elif isinstance(obj, list):
                for item in obj:
                    max_sev = find_severity(item, max_sev)
            return max_sev
        severity = find_severity(data)

    if severity == 0:
        return {"status": "no_threat", "severity": 0}

    params = load_adaptive_params()
    # v4.7.3.2: 防御性初始化 — calibration_notes缺失时params["risk"]不存在
    # → KeyError 'risk' 导致 update_all 崩溃, 校准闭环(daily_records/兑现回验)自2026-08-18冻结
    if not isinstance(params, dict):
        params = {}
    params.setdefault("risk", {})

    # === P1-1: 提取 calibration_notes ===
    calibration_notes = None
    if isinstance(data, dict):
        pv = data.get("prediction_verification", {})
        calibration_notes = pv.get("calibration_notes", None)
        if calibration_notes:
            if "risk" not in params:
                params["risk"] = {}
            params["risk"]["black_swan_calibration_notes"] = str(calibration_notes)[:500]

    # === P1-2: category-weighted dynamic position_ratio ===
    CATEGORY_WEIGHTS = {
        "geopolitical": 1.0, "geopolitical_military": 1.0,
        "financial": 0.9, "financial_system": 0.9,
        "policy": 0.9, "policy_sudden_change": 0.9,
        "tech": 0.7, "tech_breakthrough_accident": 0.7,
        "health": 0.6, "public_health": 0.6,
        "natural": 0.5, "natural_disasters": 0.5,
        "social": 0.5, "social_unrest": 0.5,
    }
    events = data.get("events_severity_ge_3", data.get("events", [])) if isinstance(data, dict) else []
    max_weighted_impact = 0.0
    event_details = []
    for ev in (events if isinstance(events, list) else []):
        ev_sev = int(ev.get("severity", 0) or 0)
        if ev_sev < 3:
            continue
        category = str(ev.get("category", "")).lower()
        confidence = float(ev.get("confidence", 0.5) or 0.5)
        cat_weight = CATEGORY_WEIGHTS.get(category, 0.8)
        sev_norm = ev_sev / 5.0
        weighted = sev_norm * cat_weight * confidence
        max_weighted_impact = max(max_weighted_impact, weighted)
        event_details.append({
            "id": ev.get("event_id", ev.get("id", "?")), "category": category,
            "severity": ev_sev, "confidence": confidence,
            "weighted": round(weighted, 3),
        })

    # position_ratio: base=1.0, 根据加权影响递减, 下限0.30
    # v4.5.13: 系数从1.2降至0.8, 下限从0.15升至0.30 — 避免severity=5时仓位被过度压至16%
    # 当有加权事件时使用加权计算，否则用severity直接映射作为fallback
    if max_weighted_impact > 0:
        if severity >= 4:
            position_ratio = max(0.30, 1.0 - max_weighted_impact * 0.8)
            params["risk"]["black_swan_active"] = True
            params["risk"]["black_swan_position_ratio"] = round(position_ratio, 2)
            params["risk"]["volatility_multiplier"] = max(0.3, 0.5 + position_ratio * 0.3)
        elif severity >= 3:
            position_ratio = max(0.40, 1.0 - max_weighted_impact * 0.6)
            params["risk"]["black_swan_active"] = True
            params["risk"]["black_swan_position_ratio"] = round(position_ratio, 2)
            params["risk"]["volatility_multiplier"] = max(0.4, 0.5 + position_ratio * 0.4)
        else:
            params["risk"]["black_swan_active"] = False
    elif severity >= 4:
        # 无加权事件但severity高 → 保守fallback
        position_ratio = 0.35
        params["risk"]["black_swan_active"] = True
        params["risk"]["black_swan_position_ratio"] = position_ratio
        params["risk"]["volatility_multiplier"] = 0.5
    elif severity >= 3:
        position_ratio = 0.50
        params["risk"]["black_swan_active"] = True
        params["risk"]["black_swan_position_ratio"] = position_ratio
        params["risk"]["volatility_multiplier"] = 0.6
    elif severity >= 2:
        params["risk"]["volatility_multiplier"] = 0.85
    else:
        params["risk"]["black_swan_active"] = False
    params["risk"]["black_swan_weighted_impact"] = round(max_weighted_impact, 3)

    # === P2-2: 提取商品趋势特征 (供 h20d/morning_decision 消费) ===
    # v4.5.5 S6(commodity修复): 分析JSON中商品价格在market_prices而非commodities键
    # v4.5.9b: 从akshare获取商品涨跌幅(之前hardcode=0)
    market_prices = data.get("market_prices", {}) if isinstance(data, dict) else {}
    risk_matrix = data.get("risk_matrix", {}) if isinstance(data, dict) else {}
    if not isinstance(risk_matrix, dict):
        risk_matrix = {}
    hedging = risk_matrix.get("hedging_assets", {})
    if not isinstance(hedging, dict):
        hedging = {}
    bs_features = {}
    
    # 获取商品涨跌幅
    _commodity_change = {}
    try:
        import akshare as ak
        for name, symbol in [("gold", "GC"), ("oil", "CL"), ("silver", "SI")]:
            try:
                df = ak.futures_foreign_hist(symbol=symbol)
                if len(df) >= 2:
                    cur = float(df.iloc[-1]["close"])
                    prev = float(df.iloc[-2]["close"])
                    if prev > 0:
                        _commodity_change[name] = round((cur - prev) / prev * 100, 2)
            except Exception:
                pass
    except ImportError:
        pass
    
    for name, asset_key in [("gold", "COMEX黄金"), ("oil", "WTI原油"), ("silver", "COMEX白银")]:
        price = market_prices.get(asset_key, 0)
        if isinstance(price, (int, float)):
            bs_features[f"{name}_price"] = round(float(price), 2)
        else:
            bs_features[f"{name}_price"] = 0.0
        # 从 hedging_assets 提取target区间
        asset_hedging = hedging.get(name, {})
        if isinstance(asset_hedging, str):
            bs_features[f"{name}_outlook"] = asset_hedging[:200]
        # v4.5.9b: 用akshare计算涨跌幅, fallback=0
        bs_features[f"{name}_change_pct"] = _commodity_change.get(name, 0.0)
    if bs_features:
        params["risk"]["black_swan_commodity"] = bs_features
    # 综合风险评级
    overall = risk_matrix.get("overall_assessment", "") if isinstance(data.get("risk_matrix", {}), dict) else ""
    params["risk"]["black_swan_overall"] = str(overall)[:100]

    save_adaptive_params(params)
    log_feedback("black_swan", f"severity_{severity}", {
        "position_ratio": params["risk"]["black_swan_position_ratio"],
        "weighted_impact": round(max_weighted_impact, 3),
        "calibration_notes": (calibration_notes or "")[:100],
        "events": event_details,
    })

    return {"status": "updated", "severity": severity}


# ═══════════════ 回测绩效→参数自适应 ═══════════════
def update_from_backtest():
    """从回测结果自适应调整交易参数"""
    bt_dir = PROJECT_ROOT / "reports" / "backtest"
    if not bt_dir.exists():
        return {"status": "no_data"}

    files = sorted(bt_dir.glob("walkforward_*.json"), reverse=True)
    if not files:
        return {"status": "no_backtest"}

    with open(files[0]) as f:
        bt = json.load(f)
    perf = bt.get("performance", {})
    
    # P1-1: 幂等保护 — 如果回测文件没变且距上次更新<30天，跳过
    bt_file_hash = str(files[0].stat().st_mtime)
    params = load_adaptive_params()
    # v4.7.3.2: 防御性初始化 — adaptive_params无trading键时同样会KeyError
    if not isinstance(params, dict):
        params = {}
    _tr = params.setdefault("trading", {})
    _tr.setdefault("position_size", 0.13)
    _tr.setdefault("max_positions", 6)
    _tr.setdefault("buy_threshold", 0.035)
    last_bt_hash = params.get("_last_backtest_hash", "")
    last_bt_date = params.get("_last_backtest_date", "")
    today = datetime.now().strftime("%Y-%m-%d")
    # 超过30天强制更新一次
    force_update = (last_bt_date and last_bt_date < (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
    if bt_file_hash == last_bt_hash and not force_update:
        return {"status": "skipped", "reason": "backtest_file_unchanged"}

    # 根据回测结果微调
    sharpe = perf.get("sharpe_ratio", 0)
    win_rate = perf.get("win_rate_pct", 0)
    profit_factor = perf.get("profit_factor", 0)
    max_dd = perf.get("max_drawdown_pct", 0)

    # 夏普高 → 可以提高仓位; 回撤大 → 降低仓位
    if sharpe >= 1.5 and abs(max_dd) < 15:
        params["trading"]["position_size"] = min(0.3, params["trading"]["position_size"] + 0.05)
        params["trading"]["max_positions"] = min(8, params["trading"]["max_positions"] + 1)
        action = "aggressive_position"
    elif sharpe < 0.5 or abs(max_dd) > 25:
        params["trading"]["position_size"] = max(0.1, params["trading"]["position_size"] - 0.05)
        params["trading"]["max_positions"] = max(3, params["trading"]["max_positions"] - 1)
        action = "defensive_position"
    else:
        action = "hold"

    # 盈亏比 < 1.5 → 收紧阈值
    if profit_factor < 1.5:
        params["trading"]["buy_threshold"] = min(0.05, params["trading"]["buy_threshold"] + 0.01)
        action = "tighten_thresholds"
    elif profit_factor > 3:
        params["trading"]["buy_threshold"] = max(0.01, params["trading"]["buy_threshold"] - 0.005)

    # P1-1: 记录回测文件指纹+日期，防止重复叠加, 每月至少更新一次
    params["_last_backtest_hash"] = bt_file_hash
    params["_last_backtest_date"] = datetime.now().strftime("%Y-%m-%d")
    save_adaptive_params(params)
    log_feedback("backtest", action, perf)

    return {"status": "updated", "action": action, "perf": perf}


# ═══════════════ 自我反思→模型参数 ═══════════════
def _extract_todays_lessons() -> list[str]:
    """P0-1: 从 LESSONS.md 和 MEMORY.md 提取今日教训，结构化存储"""
    lessons = []
    today = datetime.now().strftime("%Y-%m-%d")
    # 读 LESSONS.md
    lessons_path = Path.home() / ".openclaw" / "workspace" / "LESSONS.md"
    if lessons_path.exists():
        content = lessons_path.read_text()
        # 查找今天的教训区块
        today_header = f"## 📅 {today}"
        if today_header in content:
            idx = content.index(today_header)
            block = content[idx:]
            # 截取到下一个日期标题或文件尾
            next_date = None
            import re
            for m in re.finditer(r"## 📅 \d{4}-\d{2}-\d{2}", block):
                if m.start() > 0:
                    next_date = m.start()
                    break
            if next_date:
                block = block[:next_date]
            # 提取每个教训标题
            for line in block.split('\n'):
                line = line.strip()
                if line.startswith('### 教训'):
                    lessons.append(line)
    # 读 MEMORY.md 教训
    memory_path = Path.home() / ".openclaw" / "workspace" / "MEMORY.md"
    if memory_path.exists():
        mem = memory_path.read_text()
        # 提取教训总结编号列表
        import re
        for m in re.finditer(r"(\d+)\.\s+(.+)", mem):
            lesson_text = m.group(2).strip()
            if len(lesson_text) > 10 and len(lesson_text) < 200:
                lessons.append(lesson_text[:100])
                if len(lessons) >= 10:
                    break
    return lessons


def get_recent_lessons(max_lessons: int = 5) -> list[str]:
    """P1-2: 供外部调用，获取最近的系统教训用于上下文注入"""
    lessons = _extract_todays_lessons()
    unique = []
    seen = set()
    for l in lessons:
        key = l[:50]
        if key not in seen:
            seen.add(key)
            unique.append(l)
    return unique[-max_lessons:]


def update_from_reflection(lesson: str = None):
    """从自我反思更新系统参数
    P0-1: 自动从 LESSONS.md 和 MEMORY.md 提取今日教训
    """
    params = load_adaptive_params()
    
    # 提取实际教训（不再记录空壳）
    extracted = _extract_todays_lessons()
    actual_lesson = lesson or (extracted[0] if extracted else None)
    
    # 防重复：如果最近一条已经是同样的教训，跳过
    recent = params.get("feedback_history", [])[-3:] if params.get("feedback_history") else []
    if actual_lesson and any(actual_lesson[:30] in r.get("lesson", "") for r in recent):
        return {"status": "skipped", "reason": "duplicate_lesson"}
    
    lesson_text = actual_lesson or "📝 未发现新教训"
    if "feedback_history" not in params:
        params["feedback_history"] = []
    params["feedback_history"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "self_reflection",
        "lesson": lesson_text[:200],
    })
    # 只保留最近30条
    params["feedback_history"] = params["feedback_history"][-30:]

    save_adaptive_params(params)
    log_feedback("self_reflection", "lesson_recorded", {
        "lesson": lesson_text[:100],
        "lessons_extracted": len(extracted),
    })
    return {"status": "recorded", "lesson": lesson_text[:80], "total_extracted": len(extracted)}


# ═══════════════ 动态阈值→灰度切换 ═══════════════
def update_from_threshold(action: str = "status", **kwargs):
    """动态阈值灰度切换管理
    
    Args:
        action: 
            status - 查看当前动态阈值状态
            enable - 启用动态阈值
            disable - 禁用动态阈值(回退到固定0.58)
            test - 用历史数据测试动态阈值效果
    """
    params = load_adaptive_params()
    dt_config = params.get("dynamic_threshold", {})
    
    if action == "status":
        enabled = dt_config.get("enabled", False)
        return {
            "status": "enabled" if enabled else "disabled",
            "current_threshold": "dynamic" if enabled else 0.58,
            "hard_floor": dt_config.get("hard_floor", 0.35),
            "config": dt_config,
        }
    
    elif action == "enable":
        dt_config["enabled"] = True
        params["dynamic_threshold"] = dt_config
        save_adaptive_params(params)
        log_feedback("dynamic_threshold", "enabled", {"previous": False})
        return {"status": "enabled", "note": "动态阈值已启用，每个标的使用独立阈值"}
    
    elif action == "disable":
        dt_config["enabled"] = False
        params["dynamic_threshold"] = dt_config
        save_adaptive_params(params)
        log_feedback("dynamic_threshold", "disabled", {"previous": True})
        return {"status": "disabled", "fallback_threshold": 0.58}
    
    elif action == "test":
        # 用历史数据跑一遍动态阈值 vs 固定阈值对比
        try:
            from core.dynamic_threshold import DynamicThresholdEngine
            import joblib, glob
            
            model_dir = PROJECT_ROOT / "models" / "pool"
            files = sorted(glob.glob(str(model_dir / "*_lgb_*.pkl")))
            
            models_info = []
            for f in files:
                try:
                    d = joblib.load(f)
                    code = os.path.basename(f).split("_")[0]
                    models_info.append({
                        "code": code,
                        "accuracy": d.get("dir_accuracy", 0),
                        "r2": d.get("r2", -1),
                    })
                except Exception:
                    pass
            
            engine = DynamicThresholdEngine(enabled=True)
            results = engine.compute_batch(models_info)
            
            # 统计
            fixed_pass = sum(1 for m in models_info if m["accuracy"] >= 0.58)
            dynamic_pass = sum(1 for r in results if r.get("is_dynamic"))
            
            return {
                "status": "test_complete",
                "fixed_058_signals": fixed_pass,
                "dynamic_signals": dynamic_pass,
                "details": results[:10],  # 前10个标的
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}
    
    else:
        return {"status": "unknown_action", "action": action}


# ═══════════════ 校准系统 → 置信度校准 ═══════════════
def _recompute_overall_stats(pred_data: dict) -> dict:
    """v4.7.4(P2): overall_stats实时重算 — 修复current_stock_count/stocks_above_50等
    自2026-05-09冻结的陈旧字段。

    每标的精度主判据链: 兑现精度(非hold, 样本≥5) → h20d OOS → 训练均值
    """
    import yaml as _yaml
    stock_acc = pred_data.get("stock_accuracy", {}) or {}

    # 主池标的数 (config/master_stock_pool.yaml)
    current_count = 0
    try:
        _pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        with open(_pool_path, "r", encoding="utf-8") as _f:
            _pool = _yaml.safe_load(_f) or {}
        current_count = len(_pool.get("master_pool", []) or [])
    except Exception:
        current_count = len(stock_acc)

    # 每标的精度链 + 全局兑现统计
    above_50 = above_60 = below_45 = 0
    metrics = []
    ex_hold_c = ex_hold_t = 0
    # 全局兑现统计从 daily_records 直接计算(权威源, 覆盖含已移出股票池的历史标的)
    for _dr in pred_data.get("daily_records", []):
        for _s in _dr.get("stocks", []):
            if not _s.get("realized_checked", False) or _s.get("signal", "hold") == "hold":
                continue
            ex_hold_t += 1
            _rc = _s.get("realized_correct")
            if _rc is True or _rc == 1:
                ex_hold_c += 1
    for _sym, sa in stock_acc.items():
        if not isinstance(sa, dict):
            continue
        _rt = sa.get("realized_total", 0) or 0
        _rc = sa.get("realized_correct", 0) or 0
        # 主判据链
        if _rt >= 5:
            m = _rc / _rt
        else:
            _h20 = sa.get("h20d_accuracy", 0) or 0
            m = _h20 if _h20 > 0 else (sa.get("mean_accuracy", 0) or 0)
        if m <= 0:
            continue
        metrics.append(m)
        if m >= 0.50:
            above_50 += 1
        if m >= 0.60:
            above_60 += 1
        if m < 0.45:
            below_45 += 1

    h20d_mean = 0.0
    _h20_meta = pred_data.get("h20d_eval_meta", {}) or {}
    if _h20_meta.get("mean_accuracy"):
        try:
            h20d_mean = round(float(_h20_meta["mean_accuracy"]), 4)
        except (TypeError, ValueError):
            pass

    return {
        "current_stock_count": current_count,
        "stocks_above_50": above_50,
        "stocks_above_60": above_60,
        "stocks_below_45": below_45,
        "realized_correct_ex_hold": ex_hold_c,
        "realized_total_ex_hold": ex_hold_t,
        "realized_accuracy_ex_hold": round(ex_hold_c / ex_hold_t, 4) if ex_hold_t > 0 else None,
        "h20d_mean_accuracy": h20d_mean,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def update_from_calibration():
    """
    从每日预测结果 + 黑天鹅事件 校准预测精度
    将预测精度数据持久化到 confidence_data/ 目录
    
    读取:
      - cache/daily_predict.json (股票方向精度)
      - memory/black-swan/analysis-*.json (黑天鹅事件)
    
    输出:
      - confidence_data/prediction_calibration.json
      - confidence_data/event_history.json
      - confidence_data/confidence_calibration.json
    """
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    
    pred_file = CALIBRATION_DIR / "prediction_calibration.json"
    event_file = CALIBRATION_DIR / "event_history.json"
    conf_file = CALIBRATION_DIR / "confidence_calibration.json"
    
    # === 初始化/读取 校准数据文件 ===
    def _load_or_init(filepath, defaults):
        from common.file_lock import locked_json_read, locked_json_write
        data = locked_json_read(filepath)
        if data:
            return data
        data = {**defaults, "created_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat()}
        locked_json_write(filepath, data)
        return data
    
    pred_data = _load_or_init(pred_file, {
        "version": "1.0",
        "overall_stats": {"total_predictions": 0, "correct_predictions": 0,
                           "accuracy_rate": 0.5, "avg_confidence": 0.5,
                           "calibration_error": 0.0},
        "stock_accuracy": {},
        "daily_records": []
    })
    event_data = _load_or_init(event_file, {"events": []})
    conf_data = _load_or_init(conf_file, {
        "version": "1.0",
        "category_adjustments": {},
        "event_history": [],
        "prediction_quality": {
            "h5d_mean_accuracy": 0.5,
            "h20d_mean_accuracy": 0.5,
            "high_confidence_count": 0,
            "total_predictions": 0
        }
    })
    
    # === 1. 股票方向精度记录 ===
    stock_accuracies = {}
    acc_count = 0
    mean_acc = 0.0
    high_conf_count = 0
    predictions = []
    if DAILY_PREDICT.exists():
        try:
            with open(DAILY_PREDICT) as f:
                predict_data = json.load(f)
            
            predictions = predict_data.get("predictions", [])
            predict_date = predict_data.get("predict_date", datetime.now().strftime("%Y-%m-%d"))
            
            # 构建当日记录
            daily_record = {
                "date": predict_date,
                "version": predict_data.get("version", "?"),
                "total_stocks": len(predictions),
                "high_confidence_h5d": predict_data.get("high_confidence_h5d", 0),
                "high_confidence_pool": predict_data.get("high_confidence_pool", 0),
                "stocks": []
            }
            
            total_acc = 0.0
            acc_count = 0
            high_conf_count = 0
            
            for s in predictions:
                symbol = s.get("symbol", "")
                name = s.get("name", "")
                acc = s.get("direction_accuracy", s.get("accuracy", 0))
                if isinstance(acc, str) and acc == "?":
                    acc = 0.0
                conf = s.get("confidence", 0)
                if isinstance(conf, str) and conf == "?":
                    conf = 0.0
                signal = s.get("signal", "hold")
                
                # 当日记录
                predicted_return = s.get("predicted_return", 0)
                if isinstance(predicted_return, str):
                    predicted_return = 0
                horizon = s.get("horizon", "5d")
                rec = {"symbol": symbol, "name": name,
                       "direction_accuracy": float(acc),
                       "confidence": float(conf), "signal": signal,
                       "predicted_return": round(float(predicted_return), 4),
                       "horizon": horizon,
                       "training_hash": s.get("training_hash", ""),
                       "source": s.get("source", "")}
                
                # v4.5.3c: 预留兑现精度字段
                rec["realized_return"] = None
                rec["realized_correct"] = None
                rec["realized_checked"] = False
                
                # 附加 h20d 精度
                h20d = s.get("h20d", {})
                if h20d:
                    rec["h20d_accuracy"] = h20d.get("direction_accuracy", 0)
                    rec["h20d_predicted_return"] = round(float(h20d.get("predicted_return", 0)), 4)
                daily_record["stocks"].append(rec)
                
                # 统计
                if float(acc) > 0:
                    total_acc += float(acc)
                    acc_count += 1
                if float(acc) >= 0.58:
                    high_conf_count += 1
            
            # 更新总体统计
            stats = pred_data["overall_stats"]
            stats["total_predictions"] += len(predictions)
            mean_acc = total_acc / max(acc_count, 1)
            stats["accuracy_rate"] = round(mean_acc, 4)
            stats["last_date"] = predict_date
            
            # 防止重复记录同一天
            existing_dates = [r["date"] for r in pred_data["daily_records"]]
            if predict_date not in existing_dates:
                pred_data["daily_records"].append(daily_record)
            
            # 更新每只标的的历史精度
            for s in predictions:
                symbol = s.get("symbol", "")
                acc_val = float(s.get("direction_accuracy", s.get("accuracy", 0)))
                if symbol and acc_val > 0:
                    if symbol not in pred_data["stock_accuracy"]:
                        pred_data["stock_accuracy"][symbol] = {
                            "name": s.get("name", ""),
                            "accuracies": [],
                            "mean_accuracy": 0.0,
                            "last_accuracy": 0.0
                        }
                    sa = pred_data["stock_accuracy"][symbol]
                    # v4.5.5 S4: 基于training_hash去重 — 同一训练版本只记最后一次
                    training_hash = s.get("training_hash", "")
                    prev_hash = sa.get("last_training_hash", "")
                    if training_hash and training_hash != prev_hash:
                        # 新的训练版本 → 追加精度（唯一记录）
                        sa["accuracies"].append(acc_val)
                        sa["last_training_hash"] = training_hash
                        sa["last_training_date"] = predict_date
                    elif not training_hash:
                        # 向后兼容: 无training_hash时用旧去重逻辑
                        if not sa["accuracies"] or abs(sa["accuracies"][-1] - acc_val) > 0.0001:
                            sa["accuracies"].append(acc_val)
                    sa["accuracies"] = sa.get("accuracies", [])[-50:]
                    # 同一版本下不重复追加
                    sa["mean_accuracy"] = round(
                        sum(sa["accuracies"]) / len(sa["accuracies"]), 4)
                    sa["last_accuracy"] = acc_val
            
            # v4.6.x: 平滑信号压制 + h5d-h20d冲突降级（对齐batch_predict.compute_h5d_signal）
            suppressed_count = 0
            conflict_count = 0
            for rec in daily_record.get("stocks", []):
                acc = float(rec.get("direction_accuracy", 0))
                sig = rec.get("signal", "hold").lower()
                pred_ret = float(rec.get("predicted_return", 0))

                # sigmoid联合分数: 精度×收益幅度
                w_acc = 1.0 / (1.0 + math.exp(-(acc - 0.40) / 0.06))
                mag_score = 1.0 / (1.0 + math.exp(-abs(pred_ret) / 0.01))
                signal_score = w_acc * mag_score

                if signal_score < 0.25 and sig in ("buy", "sell"):
                    rec["signal"] = "hold"
                    rec["confidence"] = round(acc, 4)  # 置信度=精度, 不乘0.3
                    rec["signal_suppressed"] = True
                    rec["suppress_reason"] = f"signal_score={signal_score:.3f}<0.25 (acc={acc:.1%}, ret={pred_ret:+.3f})"
                    suppressed_count += 1

                # h5d-h20d方向冲突检测 (v4.6.x: 只记录冲突不强制hold, 权重已在batch_predict中处理)
                h20d_acc = rec.get("h20d_accuracy", 0)
                h20d_ret = rec.get("h20d_predicted_return", 0)
                h5d_ret = rec.get("predicted_return", 0)
                if h20d_acc and h20d_ret:
                    h5d_dir = "buy" if h5d_ret >= 0.005 else ("sell" if h5d_ret <= -0.005 else "hold")
                    h20d_dir = "buy" if h20d_ret >= 0.005 else ("sell" if h20d_ret <= -0.005 else "hold")
                    if h5d_dir != h20d_dir and h5d_dir != "hold" and h20d_dir != "hold":
                        rec["cross_conflict"] = True
                        rec["conflict_profiles"] = f"h5d={h5d_dir}({h5d_ret:.3f})/h20d={h20d_dir}({h20d_ret:.3f})"
                        rec["confidence"] = round(rec.get("confidence", 0) * 0.5, 4)
                        conflict_count += 1
            
            # 🔧 Fix 6: 从daily_records同步correct_predictions到overall_stats
            # v4.7.4(P0-2): 只统计非hold信号, hold(realized_correct=None)剔除分母
            correct_total = 0
            for dr in pred_data.get("daily_records", []):
                for sr in dr.get("stocks", []):
                    rc = sr.get("realized_correct")
                    if (rc is True or rc == 1) and sr.get("signal", "hold") != "hold":
                        correct_total += 1
            stats["correct_predictions"] = correct_total

            # v4.7.4(P2): overall_stats 实时全量重算 — 修复陈旧字段
            # (current_stock_count=29/stocks_above_50=21 等自05-09冻结)
            _ov_rt = _recompute_overall_stats(pred_data)
            for _k, _v in _ov_rt.items():
                stats[_k] = _v
            
            # 更新置信度校准的 prediction_quality
            conf_data["prediction_quality"] = {
                "h5d_mean_accuracy": round(mean_acc, 4),
                "high_confidence_count": high_conf_count,
                "total_predictions": len(predictions),
                "last_updated": predict_date
            }
            
            _n_preds = stats["total_predictions"]
            stats["avg_confidence"] = round(
                (stats.get("avg_confidence", 0.5) * (_n_preds - len(predictions))
                 + mean_acc * len(predictions)) / max(_n_preds, 1), 4)
            stats["calibration_error"] = round(
                abs(stats["avg_confidence"] - stats["accuracy_rate"]), 4)
            
            pred_data["last_updated"] = datetime.now().isoformat()
            from common.file_lock import locked_json_write
            locked_json_write(pred_file, pred_data)
            
        except Exception as e:
            print(f"⚠️ 校准: 读取预测数据失败: {e}")
    
    # === 2. 记录黑天鹅事件到 event_history ===
    bs_dir = Path.home() / ".openclaw" / "workspace" / "memory" / "black-swan"
    if bs_dir.exists():
        files = sorted(bs_dir.glob("analysis-*.json"), reverse=True)
        if files:
            try:
                with open(files[0]) as f:
                    bs_data = json.load(f)
                # 记录事件
                event_entry = {
                    "id": f"bs_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    "timestamp": datetime.now().isoformat(),
                    "source": "black_swan_analysis",
                    "data": bs_data
                }
                event_data["events"].append(event_entry)
                event_data["last_updated"] = datetime.now().isoformat()
                # 只保留最近100条
                event_data["events"] = event_data["events"][-100:]
                from common.file_lock import locked_json_write
                locked_json_write(event_file, event_data)
            except Exception as e:
                print(f"⚠️ 校准: 记录事件失败: {e}")
    
    # === 3. 保存置信度校准 ===
    conf_data["last_updated"] = datetime.now().isoformat()
    from common.file_lock import locked_json_write
    locked_json_write(conf_file, conf_data)
    
    log_feedback("calibration", "prediction_accuracy_recorded", {
        "total_predictions": len(predictions) if DAILY_PREDICT.exists() else 0,
        "mean_accuracy": round(mean_acc, 4) if acc_count > 0 else 0,
        "high_confidence": high_conf_count
    })
    
    # === 4. v4.5.3c: 校准反馈闭环 — 分析精度趋势并写入 adaptive_params ===
    try:
        from core.calibration_feedback import get_calibration_feedback
        cf = get_calibration_feedback()
        plan = cf.write_to_adaptive_params()
        if plan["total"] > 0:
            print(f"📊 校准反馈: {plan['summary']}")
            print(f"   写入 adaptive_params.yaml → batch_train 将在下次训练时消费")
    except Exception as e:
        print(f"⚠️ 校准反馈: 分析失败: {e}")
    
    # === 5. v4.5.3c: 兑现精度回溯验证 ===
    try:
        from core.calibration_feedback import get_calibration_feedback
        cf = get_calibration_feedback()
        result = cf.check_realized_accuracy(max_days=7)
        if result["total"] > 0:
            print(f"🎯 兑现精度回溯: {result['checked']}条验证, "
                  f"正确{result['correct']}/{result['total']} (精度{result['accuracy']:.1%})")
    except Exception as e:
        print(f"⚠️ 兑现精度验证: {e}")
    
    # === 6. v4.5.7: daily_record完整性检查 ===
    try:
        from core.calibration_feedback import verify_daily_record_integrity
        integrity = verify_daily_record_integrity()
        if integrity["valid"]:
            print(f"✅ {integrity['message']}")
        else:
            print(f"🛠️ {integrity['message']}")
    except Exception as e:
        print(f"⚠️ daily_record完整性检查跳过: {e}")
    
    return {"status": "updated",
            "predictions_recorded": len(predictions) if DAILY_PREDICT.exists() else 0,
            "mean_accuracy": round(total_acc / max(acc_count, 1), 4) if acc_count > 0 else 0,
            "events_recorded": len(event_data["events"])}


# ═══════════════ 美股风险→泡沫评分 ═══════════════
def update_from_us_risk():
    """
    评估美股泡沫风险等级(0-100分)，写入 adaptive_params.risk.us_bubble
    数据源: akshare 标普500/纳指/美债利差
    执行时机: 06:00 自我反思回调, 利用美股收盘后(04:00 BJT)的完整数据
    """
    score = 0
    details = {}
    
    try:
        import akshare as ak
        import pandas as pd
        from common.akshare_utils import safe_index_us_stock_sina
        
        # === 1. 标普500 (30%) — 日内振幅代理VIX恐慌 ===
        try:
            sp500 = ak.index_us_stock_sina(symbol='.INX')
            if len(sp500) >= 20:
                last = sp500.iloc[-1]
                sp_close = float(last['close'])
                sp_high = float(last['high'])
                sp_low = float(last['low'])
                
                # 日内振幅% → VIX代理
                intraday_vol = (sp_high - sp_low) / sp_close
                if intraday_vol > 0.025:  # 2.5%+振幅 → 恐慌
                    score += 30
                    details['sp_intraday_vol'] = round(intraday_vol*100, 2)
                elif intraday_vol > 0.015:
                    score += 18
                    details['sp_intraday_vol'] = round(intraday_vol*100, 2)
                elif intraday_vol > 0.008:
                    score += 9
                    details['sp_intraday_vol'] = round(intraday_vol*100, 2)
                else:
                    score += 3
                    details['sp_intraday_vol'] = round(intraday_vol*100, 2)
                
                details['sp_close'] = round(sp_close, 2)
                details['sp_change'] = round(float(last.get('open', sp_close)) and (sp_close - float(last['open'])) / float(last['open']) * 100, 2)
                
                # 距20日高点的回撤 → 趋势恶化信号
                high_20 = sp500['high'].tail(20).max()
                drawdown_20 = (sp_close - high_20) / high_20
                details['sp_drawdown_20'] = float(round(drawdown_20 * 100, 2))
        except Exception as e:
            details['sp_error'] = str(e)[:80]
        
        # === 2. 纳斯达克 (15%) — 科技股情绪 ===
        # v4.5.3c fix: 从 index_global_spot_em()(东方财富, 连接重置) 切换到
        #   index_us_stock_sina()(新浪, 同SP500, 无Rate Limit)
        try:
            nasdaq = ak.index_us_stock_sina(symbol='.IXIC')
            if len(nasdaq) >= 2:
                last_close = float(nasdaq.iloc[-1]['close'])
                prev_close = float(nasdaq.iloc[-2]['close'])
                nasdaq_change = round((last_close / prev_close - 1) * 100, 2)
                details['nasdaq_change'] = nasdaq_change
                details['nasdaq_price'] = last_close
                if nasdaq_change < -3:
                    score += 15
                elif nasdaq_change < -1.5:
                    score += 10
                elif nasdaq_change < -0.5:
                    score += 5
                elif nasdaq_change > 2:
                    score += 8  # 过度亢奋也是风险
        except Exception as e:
            details['nasdaq_error'] = str(e)[:80]
        
        # === 3. 美债利差 (20%) — 衰退预警 ===
        try:
            bond = ak.bond_zh_us_rate()
            if len(bond) > 0:
                # 找最近的 10Y 和 2Y 收益率
                last_bond = bond.iloc[-1]
                y10 = float(last_bond.get('美国国债收益率10年', 0) or 0)
                y2 = float(last_bond.get('美国国债收益率2年', 0) or 0)
                if y10 > 0 and y2 > 0:
                    spread = y10 - y2
                    details['us_10y'] = round(y10, 2)
                    details['us_2y'] = round(y2, 2)
                    details['us_spread'] = round(spread, 2)
                    if spread < -0.5:  # 深度倒挂
                        score += 20
                    elif spread < -0.2:
                        score += 15
                    elif spread < 0:
                        score += 10
                    elif spread < 0.5:
                        score += 5
        except Exception as e:
            details['bond_error'] = str(e)[:80]
        
        # === 4. 黑天鹅叠加 (15%) ===
        params = load_adaptive_params()
        bs_active = params.get("risk", {}).get("black_swan_active", False)
        bs_severity = 0
        # 从 black_swan 读取当前 severity
        if bs_active:
            bs_ratio = params.get("risk", {}).get("black_swan_position_ratio", 1.0)
            # position_ratio越低说明风险越高
            if bs_ratio <= 0.2:
                score += 15
                bs_severity = 5
            elif bs_ratio <= 0.35:
                score += 10
                bs_severity = 3
            elif bs_ratio <= 0.6:
                score += 5
                bs_severity = 2
        details['bs_severity'] = bs_severity
        
        # === 5. 标普资金面 (20%) — 量价背离 ===
        try:
            sp500 = safe_index_us_stock_sina(symbol='.INX')
            if len(sp500) >= 10:
                last_vol = float(sp500.iloc[-1]['volume'])
                avg_vol_5 = sp500['volume'].tail(6).head(5).astype(float).mean()  # 前几天均值
                vol_ratio = last_vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                details['sp_vol_ratio'] = round(vol_ratio, 2)
                if vol_ratio > 2.5:  # 放量暴跌
                    score += 20
                elif vol_ratio > 1.5:
                    score += 12
                elif vol_ratio < 0.5:  # 缩量上涨→动能衰竭
                    last_change = sp500.iloc[-1]['close'] - sp500.iloc[-1]['open']
                    if last_change > 0:
                        score += 10
        except Exception as e:
            details['sp_vol_error'] = str(e)[:80]
        
    except Exception as e:
        return {"status": "error", "message": str(e)[:100]}
    
    # 上限 100
    score = min(100, score)
    details['total_score'] = score
    
    # 风险等级
    if score >= 60:
        level = "🔴 HIGH"
    elif score >= 35:
        level = "🟡 MEDIUM"
    else:
        level = "🟢 LOW"
    details['level'] = level
    
    # 写入 adaptive_params — 确保所有值是原生Python类型
    def _clean(v):
        if hasattr(v, 'item'):
            return float(v.item()) if 'float' in str(type(v)) else int(v.item()) if 'int' in str(type(v)) else v
        return v
    
    params = load_adaptive_params()
    if "risk" not in params:
        params["risk"] = {}
    params["risk"]["us_bubble"] = {k: _clean(v) for k, v in details.items()}
    save_adaptive_params(params)
    
    log_feedback("us_risk", f"score_{score}", details)
    
    return {"status": "updated", "score": score, "level": level, "details": details}


# ═══════════════ 全量更新 ═══════════════
def update_all():
    """一键更新所有反馈源"""
    results = {}
    results["black_swan"] = update_from_black_swan()
    results["backtest"] = update_from_backtest()
    results["reflection"] = update_from_reflection()
    results["calibration"] = update_from_calibration()
    results["us_risk"] = update_from_us_risk()
    results["attribution"] = update_attribution()  # v4.5.7: 绩效归因
    return results


# ═══════════════ v4.5.7: 绩效归因 ═══════════════
def update_attribution() -> dict:
    """绩效归因分析: 选股α + 择时 + 行业权重 + 成本
    
    v4.5.6b: 添加SQLite DB直读fallback，修复simulation_portfolio.json中symbol/pnl为None的问题
    """
    from core.risk_manager import performance_attribution
    
    try:
        trades = []
        
        # 第1级: simulation_portfolio.json
        ledger_path = os.path.join(PROJECT_ROOT, "data", "simulation_portfolio.json")
        if os.path.exists(ledger_path):
            with open(ledger_path) as f:
                sim = json.load(f)
            trades = sim.get("trade_history", [])
        
        # 检查数据完整性: 有symbol=None → fallback到DB
        has_null_symbol = any(t.get("symbol") is None for t in trades) if trades else True
        
        # 第2级: 直接从paper_trading.db读取
        if has_null_symbol or not trades or len(trades) < 3:
            import sqlite3
            db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                db_trades = conn.execute(
                    "SELECT * FROM trade_history ORDER BY id"
                ).fetchall()
                conn.close()
                
                if db_trades:
                    # 构建交易列表 + 按标的匹配买卖计算PnL
                    trades = []
                    buy_prices = {}  # symbol → [(price, quantity)]
                    for t in db_trades:
                        d = dict(t)
                        code = d.get("stock_code", "?")
                        action = d.get("action", "?").upper()
                        price = float(d.get("price", 0) or 0)
                        qty = int(d.get("quantity", 0) or 0)
                        amt = float(d.get("amount", 0) or 0)
                        trade = {
                            "symbol": code,
                            "action": action,
                            "price": price,
                            "quantity": qty,
                            "amount": amt,
                            "timestamp": d.get("timestamp", ""),
                            "pnl": 0.0,
                            "hold": 0,
                        }
                        if action == "BUY":
                            buy_prices.setdefault(code, []).append((price, qty))
                            trade["pnl"] = None  # BUY无PnL
                        elif action == "SELL" and code in buy_prices and buy_prices[code]:
                            # FIFO匹配: 用最早的买入价计算PnL
                            buy_price, buy_qty = buy_prices[code].pop(0)
                            matched_qty = min(qty, buy_qty)
                            trade["pnl"] = round((price - buy_price) * matched_qty, 2)
                            if buy_qty > qty:
                                buy_prices[code].insert(0, (buy_price, buy_qty - qty))
                        elif action == "SELL":
                            trade["pnl"] = None
                        trades.append(trade)
                    print(f"  📊 已从DB读取 {len(trades)} 条交易记录")
        
        if not trades or len(trades) < 2:
            return {"status": "skipped", "reason": "交易数据不足"}
        
        # v4.5.9b: 读取当前持仓(open_positions)融合未实现盈亏
        open_positions = []
        db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
        if os.path.exists(db_path):
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM positions").fetchall():
                d = dict(row)
                if d.get("quantity", 0) > 0:
                    open_positions.append({
                        "symbol": d.get("stock_code", "?"),
                        "quantity": d.get("quantity", 0),
                        "avg_cost": d.get("avg_cost", 0),
                        "current_price": d.get("current_price", 0),
                    })
            conn.close()
        if open_positions:
            print(f"  📊 融合未实现盈亏: {len(open_positions)}只持仓")
        
        attribution = performance_attribution(trades, open_positions=open_positions)
        
        # 保存到adaptive_params
        params = load_adaptive_params()
        params["_performance_attribution"] = {
            "last_update": datetime.now().isoformat(),
            **attribution
        }
        save_adaptive_params(params)
        
        print(
            f"📈 绩效归因: 选股α={attribution.get('stock_selection',0):.2%} "
            f"择时={attribution.get('timing',0):.2%} "
            f"胜率={attribution.get('win_rate',0):.1%} "
            f"盈亏比={attribution.get('profit_factor',0):.2f}\n"
            f"   已实现={attribution.get('realized_pnl',0):+.0f} "
            f"未实现={attribution.get('unrealized_pnl',0):+.0f} "
            f"合计={attribution.get('total',0):+.0f}"
        )
        
        return {"status": "updated", "attribution": attribution}
    except Exception as e:
        print(f"⚠️ 绩效归因失败: {e}")
        return {"status": "error", "error": str(e)}


# ═══════════════ 读取当前参数 ═══════════════
def get_current_params() -> dict:
    """供其他脚本读取当前自适应参数"""
    return load_adaptive_params()


if __name__ == "__main__":
    # v4.5.3d: 进度追踪
    try:
        from common.progress_tracker import ProgressTracker
        tracker = ProgressTracker("feedback_controller", total_steps=1)
    except ImportError:
        tracker = None
    
    results = update_all()
    
    if tracker:
        tracker.complete("校准更新完成", predictions_recorded=results.get("predictions_recorded", 0))
    
    print(json.dumps(results, indent=2, ensure_ascii=False))
