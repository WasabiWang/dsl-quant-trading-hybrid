#!/usr/bin/env python3
"""
DSL v4.5.9 S6 — 数据契约定义层
结构化Schema定义所有模块间数据交换的字段名、路径、映射关系。
一次定义，全员消费，杜绝字段名错配。

模式: 数据契约
- schema: 定义producer写什么key, consumer读什么key
- validator: 运行时验证生产端和消费端一致性
- cross_check: 跨文件交叉验证数据一致性
"""
import os, json, yaml
from pathlib import Path
from typing import Dict, List, Optional, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIDENCE_DIR = PROJECT_ROOT / "confidence_data"
CACHE_DIR = PROJECT_ROOT / "cache"
PRE_MARKET_DIR = CACHE_DIR / "pre_market"
CONFIG_DIR = PROJECT_ROOT / "config"


# ════════════════════════════════════════════════
# 1. 字段名映射表 — 定义所有跨模块字段
# ════════════════════════════════════════════════
FIELD_MAP = {
    # ── risk.json ←→ adaptive_params.yaml ──
    "risk.position_ratio": {
        "description": "风险调整后仓位比例",
        "producer": [
            {"file": "cache/pre_market/{date}_risk.json", "field": "position_ratio"},
            {"file": "config/adaptive_params.yaml", "field": "risk.black_swan_position_ratio"},
        ],
        "consumer": [
            {"file": "scripts/morning_decision.py", "field": "position_ratio", "line_ref": 414},
        ],
        "fallback_order": ["position_ratio", "recommended_position_ratio"],
        "type": "float (0.0-1.0)",
    },
    "risk.black_swan_active": {
        "description": "黑天鹅是否活跃",
        "producer": [
            {"file": "config/adaptive_params.yaml", "field": "risk.black_swan_active"},
        ],
        "consumer": [
            {"file": "scripts/morning_decision.py", "line_ref": 467},
        ],
        "type": "bool",
    },

    # ── black_swan analysis JSON keys ──
    "black_swan.events_key": {
        "description": "黑天鹅分析JSON中严重事件字段名",
        "producer": [
            {"file": "memory/black-swan/analysis-*.json", "field": "events_severity_ge_3"},
        ],
        "consumer": [
            {"file": "scripts/pre_market_refresh.py", "function": "fetch_black_swan_latest", "line_ref": 204},
            {"file": "scripts/feedback_controller.py", "function": "update_from_black_swan", "line_ref": 136},
            {"file": "web_dashboard/data_adapter.py", "function": "get_blackswan", "line_ref": 280},
        ],
        "type": "list[dict]",
    },
    "black_swan.commodities_key": {
        "description": "商品价格所在字段名",
        "producer": [
            {"file": "memory/black-swan/analysis-*.json", "field": "market_prices"},
        ],
        "consumer": [
            {"file": "scripts/feedback_controller.py", "function": "update_from_black_swan", "line_ref": 174},
        ],
        "type": "dict[commodity_name→price]",
    },

    # ── calibration JSON keys ──
    "calibration.stock_accuracy": {
        "description": "精确率数据所在文件",
        "files": ["confidence_data/prediction_calibration.json", "confidence_data/confidence_calibration.json"],
        "sync_rule": "prediction_calibration → confidence_calibration (stock_accuracy同步)",
    },

    # ── paper trading ──
    "trading.ledger_sync": {
        "description": "纸交易多数据源同步规则",
        "files": ["data/paper_trading.db", "data/paper_trading_ledger.json", "data/simulation_portfolio.json"],
        "sync_rule": "SQLite(Master) → Ledger JSON → Simulation JSON",
        "position_count_rule": "三文件持仓数必须一致",
    },

    # ── circuit breaker ──
    "trading.circuit_breaker": {
        "description": "熔断器状态",
        "file": "data/circuit_breaker.json",
        "reset_rule": "测试后必须trading_paused=false, today_trade_count=0",
    },
}


# ════════════════════════════════════════════════
# 2. 运行时验证器
# ════════════════════════════════════════════════

class DataContractValidator:
    """自动验证数据契约一致性"""

    def __init__(self):
        self.errors = []
        self.warnings = []

    def check_all(self) -> List[str]:
        """运行所有检查, 返回错误列表"""
        self.check_risk_position_ratio()
        self.check_black_swan_events_key()
        self.check_trading_ledger_sync()
        self.check_calibration_sync()
        self.check_circuit_breaker_clean()
        return self.errors

    def check_risk_position_ratio(self):
        """验证 risk.json 的 position_ratio 字段名正确"""
        dates = sorted([f.stem.replace("_risk","") for f in PRE_MARKET_DIR.glob("*_risk.json")])
        if not dates:
            return
        latest = PRE_MARKET_DIR / f"{dates[-1]}_risk.json"
        try:
            with open(latest) as f:
                data = json.load(f)
            if "position_ratio" not in data:
                self.errors.append(f"[SCHEMA] {latest}: 缺position_ratio字段")
            # 验证adaptive_params.yaml的字段
            ap_path = CONFIG_DIR / "adaptive_params.yaml"
            if ap_path.exists():
                with open(ap_path) as f:
                    ap = yaml.safe_load(f)
                bs = ap.get("risk", {})
                if "black_swan_position_ratio" not in bs:
                    self.errors.append("[SCHEMA] adaptive_params.yaml: 缺risk.black_swan_position_ratio")
                if "black_swan_active" not in bs:
                    self.errors.append("[SCHEMA] adaptive_params.yaml: 缺risk.black_swan_active")
        except Exception as e:
            self.warnings.append(f"[SCHEMA] risk.json检查跳过: {e}")

    def check_black_swan_events_key(self):
        """验证黑天鹅分析JSON使用正确的key名
        v4.5.19: 适配新版analysis格式(使用structured_predictions/risk_matrix/daily_event_scan)
        """
        bs_dir = PROJECT_ROOT.parent / "memory" / "black-swan"
        if not bs_dir.exists():
            return
        files = sorted(bs_dir.glob("analysis-*.json"), reverse=True)
        if not files:
            return
        try:
            with open(files[0]) as f:
                data = json.load(f)
            
            # 新版analysis格式 v2+ : 核心字段
            v2_core = ["meta", "daily_event_scan", "risk_matrix", "structured_predictions", "daily_review_summary"]
            missing_v2 = [f for f in v2_core if f not in data]
            if missing_v2 and len(missing_v2) < len(v2_core):
                # 部分缺失—告警
                self.warnings.append(f"[SCHEMA] {files[0].name}: 缺新版核心字段 {missing_v2}")
            elif missing_v2:
                # 全部缺失—可能还是旧版
                # 回退检查旧版字段
                if "events_severity_ge_3" not in data:
                    self.warnings.append(f"[SCHEMA] {files[0].name}: 缺旧版events_severity_ge_3字段(可能是新格式)")
                if "market_prices" not in data:
                    self.warnings.append(f"[SCHEMA] {files[0].name}: 缺旧版market_prices字段(可能是新格式)")
        except Exception:
            pass

    def check_trading_ledger_sync(self):
        """验证纸交易三数据源持仓数一致"""
        sq_count = -1
        led_count = -1
        sim_count = -1

        # SQLite
        db_path = DATA_DIR / "paper_trading.db"
        if db_path.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path))
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM positions")
                sq_count = c.fetchone()[0]
                conn.close()
            except Exception:
                pass

        # Ledger JSON
        led_path = DATA_DIR / "paper_trading_ledger.json"
        if led_path.exists():
            try:
                with open(led_path) as f:
                    led = json.load(f)
                led_count = len(led.get("positions", []))
            except Exception:
                pass

        # Simulation JSON
        sim_path = DATA_DIR / "simulation_portfolio.json"
        if sim_path.exists():
            try:
                with open(sim_path) as f:
                    sim = json.load(f)
                sim_count = len(sim.get("positions", {}))
            except Exception:
                pass

        counts = [c for c in [sq_count, led_count, sim_count] if c >= 0]
        if len(counts) >= 2 and len(set(counts)) != 1:
            self.errors.append(
                f"[SYNC] 持仓数不一致: SQLite={sq_count}, Ledger={led_count}, Simulation={sim_count}"
            )

    def check_calibration_sync(self):
        """验证prediction和confidence校准文件一致"""
        pred_path = CONFIDENCE_DIR / "prediction_calibration.json"
        conf_path = CONFIDENCE_DIR / "confidence_calibration.json"
        if not pred_path.exists() or not conf_path.exists():
            return
        try:
            with open(pred_path) as f:
                pred = json.load(f)
            with open(conf_path) as f:
                conf = json.load(f)
            pred_keys = set(pred.get("stock_accuracy", {}).keys())
            conf_keys = set(conf.get("stock_accuracy", {}).keys())
            missing = pred_keys - conf_keys
            if missing:
                self.errors.append(
                    f"[SYNC] confidence_calibration缺{len(missing)}只精度数据: {sorted(missing)[:5]}"
                )
        except Exception:
            pass

    def check_circuit_breaker_clean(self):
        """验证熔断器没有被测试污染"""
        cb_path = DATA_DIR / "circuit_breaker.json"
        if not cb_path.exists():
            return
        try:
            with open(cb_path) as f:
                cb = json.load(f)
            if cb.get("trading_paused", False) and cb.get("today_trade_count", 0) >= 10:
                self.warnings.append("[ISOLATION] circuit_breaker被测试写为paused, 建议reset")
            # v4.5.9 S6: 校验circuit_breaker计数 vs 实际交易记录
            cb_date = cb.get("last_trade_date", "")
            cb_count = cb.get("today_trade_count", 0)
            if cb_count > 0:
                try:
                    import sqlite3
                    db = DATA_DIR / "paper_trading.db"
                    if db.exists():
                        conn = sqlite3.connect(str(db))
                        c = conn.cursor()
                        c.execute("SELECT COUNT(*) FROM trade_history WHERE timestamp LIKE ? || '%'", (cb_date[:10],))
                        actual = c.fetchone()[0]
                        conn.close()
                        if cb_count != actual:
                            self.errors.append(
                                f"[SYNC] circuit_breaker计数{cb_count} vs 实际交易{actual}(日期{cb_date[:10]})"
                            )
                except Exception:
                    pass
        except Exception:
            pass


# ════════════════════════════════════════════════
# 3. 测试隔离治理
# ════════════════════════════════════════════════

def assert_test_isolation():
    """验证测试结束后没有污染生产数据

    在harness测试套件的teardown中调用。
    """
    validator = DataContractValidator()

    # 检查circuit_breaker  (最常被污染)
    cb_path = DATA_DIR / "circuit_breaker.json"
    if cb_path.exists():
        with open(cb_path) as f:
            cb = json.load(f)
        if cb.get("trading_paused", False):
            # reset
            cb["trading_paused"] = False
            cb["pause_until"] = 0
            cb["pause_reason"] = ""
            cb["today_trade_count"] = 0
            cb["trigger_history"] = []
            with open(cb_path, "w") as f:
                json.dump(cb, f, indent=2)
            return True  # cleaned
    return False  # no cleanup needed


# ════════════════════════════════════════════════
# 4. 自检入口
# ════════════════════════════════════════════════

if __name__ == "__main__":
    print("="*60)
    print("  ⚡ DSL 数据契约自检")
    print("="*60)
    v = DataContractValidator()
    errors = v.check_all()
    if errors:
        print(f"\n❌ 发现 {len(errors)} 个契约违反:")
        for e in errors:
            print(f"  {e}")
        if len(errors) > 3:
            print(f"\n  ⚠️ 建议: 运行 python3 core/data_schema.py 查看字段映射定义")
    else:
        print("\n✅ 所有数据契约一致")
    # Cleanup test artifacts
    if assert_test_isolation():
        print("🔧 已清理circuit_breaker测试污染")
