#!/usr/bin/env python3
"""
Dashboard contract audit.

Validates every Dashboard tab/module against backend data contracts.  The
default adapter mode imports web_dashboard.data_adapter directly and does not
require a running server.  Browser smoke is optional and read-only.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

STATIC_INDEX = PROJECT_ROOT / "web_dashboard" / "static" / "index.html"
DATA_DIR = PROJECT_ROOT / "data"

TAB_CONTRACTS = {
    "overview": {
        "name": "总览",
        "paths": [
            "status.health", "status.version",
            "pool.total", "pool.stocks", "pool.tiers",
            "calibration.summary", "calibration.stocks",
            "blackswan.active", "blackswan.severity", "blackswan.position_ratio",
            "portfolio.total_positions", "portfolio.cash", "portfolio.positions",
            "attribution",
        ],
    },
    "predictions": {
        "name": "预测/筛选",
        "paths": [
            "predictions.total", "predictions.predictions",
            "pool.stocks", "portfolio.positions",
        ],
    },
    "models": {
        "name": "模型",
        "paths": [
            "calibration.summary.total", "calibration.summary.retrain_urgent",
            "calibration.summary.retrain_planned", "calibration.summary.normal",
            "calibration.stocks", "predictions.predictions",
        ],
    },
    "progress": {
        "name": "进度/管道",
        "paths": ["pipeline.timeline", "pipeline.stats", "pipeline.phases_order"],
    },
    "blackswan": {
        "name": "黑天鹅",
        "paths": [
            "blackswan.active", "blackswan.severity", "blackswan.events",
            "blackswan.market_prices", "blackswan.top_threats",
            "blackswan.position_ratio", "blackswan.lppl",
        ],
    },
    "paper-trader": {
        "name": "模拟交易",
        "paths": ["paperTrader.summary", "paperTrader.positions", "paperTrader.history"],
    },
}

FETCH_CONTRACT_PREFIXES = {
    "/api/full": "read",
    "/api/paper-trader": "read",
    "/api/pipeline": "read",
    "/api/task-reports/": "read",
    "/api/retrain-status": "read",
    "/api/accuracy-trend": "read",
    "/api/cron/trigger/": "mutation",
    "/api/retrain": "mutation",
    "/api/retrain-now": "mutation",
}

VALID_TASK_STATUSES = {
    "idle", "running", "completed", "stale", "failed", "missed", "skipped", "timeout"
}


class DashboardContractAudit:
    def __init__(self, browser_url: str = ""):
        self.browser_url = browser_url
        self.issues = []
        self.warnings = []
        self.passes = []
        self.details = {}

    def run_all(self) -> dict:
        self.check_static_frontend_contract()
        full = self.load_full_dashboard()
        self.check_full_dashboard_contract(full)
        self.check_prediction_module(full)
        self.check_model_module(full)
        self.check_pipeline_module(full)
        self.check_paper_trader_consistency(full)
        self.check_auxiliary_endpoints()
        if self.browser_url or os.getenv("RUN_DASHBOARD_BROWSER_AUDIT") == "1":
            self.check_browser_smoke(self.browser_url or "http://127.0.0.1:8888")
        return self.summary()

    @staticmethod
    def _read_index() -> str:
        return STATIC_INDEX.read_text(encoding="utf-8")

    @staticmethod
    def _path_exists(data, dotted: str) -> bool:
        cur = data
        for part in dotted.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False
        return True

    @staticmethod
    def _canonical_fetch(path: str) -> str:
        parsed = urlparse(path)
        base = parsed.path or path.split("?", 1)[0]
        return base

    @staticmethod
    def _matches_prefix(path: str) -> tuple[bool, str]:
        for prefix, mode in FETCH_CONTRACT_PREFIXES.items():
            if path == prefix or path.startswith(prefix):
                return True, mode
        return False, ""

    def check_static_frontend_contract(self):
        """Validate every declared Dashboard tab and fetch endpoint has a contract."""
        html = self._read_index()
        tabs = sorted({
            tab for tab in re.findall(r'data-tab="([^"]+)"', html)
            if re.fullmatch(r"[a-z0-9-]+", tab)
        })
        missing = [tab for tab in tabs if tab not in TAB_CONTRACTS]
        stale_contracts = [tab for tab in TAB_CONTRACTS if tab not in tabs]
        if missing:
            self.issues.append(f"Dashboard tab缺少合约: {missing}")
        elif stale_contracts:
            self.warnings.append(f"Dashboard合约中存在前端未声明tab: {stale_contracts}")
        else:
            self.passes.append(f"Dashboard tab合约覆盖: {len(tabs)}个 ✅")

        fetches = sorted(set(
            self._canonical_fetch(path)
            for path in re.findall(r"fetch\(['\"]([^'\"]+)", html)
        ))
        uncovered = []
        mutations = []
        reads = []
        for path in fetches:
            ok, mode = self._matches_prefix(path)
            if not ok:
                uncovered.append(path)
            elif mode == "mutation":
                mutations.append(path)
            else:
                reads.append(path)
        if uncovered:
            self.issues.append(f"前端fetch未纳入Dashboard合约: {uncovered}")
        else:
            self.passes.append(f"前端fetch合约覆盖: read={len(reads)} mutation={len(mutations)} ✅")
        self.details["tabs"] = tabs
        self.details["fetches"] = fetches
        self.details["mutation_fetches"] = mutations

    def load_full_dashboard(self) -> dict:
        from web_dashboard.data_adapter import get_full_dashboard
        data = get_full_dashboard()
        if not isinstance(data, dict):
            self.issues.append("/api/full adapter返回非dict")
            return {}
        self.passes.append("/api/full adapter可读 ✅")
        return data

    def check_full_dashboard_contract(self, full: dict):
        for tab, contract in TAB_CONTRACTS.items():
            if tab == "paper-trader":
                continue
            missing = [path for path in contract["paths"] if not self._path_exists(full, path)]
            if missing:
                self.issues.append(f"{contract['name']}模块缺后端字段: {missing}")
            else:
                self.passes.append(f"{contract['name']}模块字段完整 ✅")

    def check_prediction_module(self, full: dict):
        pred = full.get("predictions", {})
        rows = pred.get("predictions", [])
        if not isinstance(rows, list):
            self.issues.append("predictions.predictions不是list")
            return
        total = pred.get("total")
        if total != len(rows):
            self.issues.append(f"预测模块total不一致: total={total}, rows={len(rows)}")
        else:
            self.passes.append(f"预测模块total一致: {total}行 ✅")
        required = {"symbol", "name", "signal", "confidence", "predicted_return",
                    "direction_accuracy", "source"}
        missing_rows = [
            r.get("symbol", "?")
            for r in rows
            if not required.issubset(set(r.keys()))
        ]
        if missing_rows:
            self.issues.append(f"预测行缺关键字段: {missing_rows[:8]}")
        else:
            self.passes.append("预测行关键字段完整 ✅")

        active = [
            r for r in rows
            if r.get("source") != "suspended" and r.get("confidence_level") != "suspended"
        ]
        if active:
            fallback = sum(1 for r in active if r.get("source") == "pool_fallback")
            default_acc = sum(1 for r in active if r.get("direction_accuracy") == 0.5)
            if fallback / len(active) > 0.30 or default_acc / len(active) > 0.10:
                self.issues.append(
                    f"预测页语义污染: pool_fallback={fallback}/{len(active)}, "
                    f"待训练哨兵={default_acc}/{len(active)}"
                )
            else:
                self.passes.append("预测页source/default语义健康 ✅")

    def check_model_module(self, full: dict):
        cal = full.get("calibration", {})
        summary = cal.get("summary", {})
        stocks = cal.get("stocks", [])
        if not isinstance(stocks, list):
            self.issues.append("calibration.stocks不是list")
            return
        if summary.get("total") != len(stocks):
            self.issues.append(f"模型模块summary.total不一致: {summary.get('total')} vs {len(stocks)}")
        else:
            self.passes.append(f"模型模块summary.total一致: {len(stocks)}只 ✅")
        bad = [
            s.get("symbol", "?")
            for s in stocks
            if "accuracy" not in s or "calibration_status" not in s
        ]
        if bad:
            self.issues.append(f"模型行缺accuracy/calibration_status: {bad[:8]}")
        else:
            self.passes.append("模型行关键字段完整 ✅")

    def check_pipeline_module(self, full: dict):
        pipeline = full.get("pipeline", {})
        timeline = pipeline.get("timeline", {})
        phases = pipeline.get("phases_order", [])
        stats = pipeline.get("stats", {})
        if not isinstance(timeline, dict) or not isinstance(phases, list):
            self.issues.append("pipeline.timeline/phases_order结构异常")
            return
        tasks = []
        for phase in timeline.values():
            tasks.extend(phase.get("tasks", []) if isinstance(phase, dict) else [])
        unknown_status = [
            t.get("task_id", t.get("logical_id", "?"))
            for t in tasks
            if t.get("status") not in VALID_TASK_STATUSES
        ]
        if unknown_status:
            self.issues.append(f"Pipeline未知状态: {unknown_status[:8]}")
        elif stats.get("total") != len(tasks):
            self.warnings.append(f"Pipeline stats.total不一致: {stats.get('total')} vs {len(tasks)}")
        else:
            self.passes.append(f"Pipeline stats/timeline一致: {len(tasks)}任务 ✅")

    def _read_paper_trader_snapshot(self):
        db_path = DATA_DIR / "paper_trading.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM positions").fetchall()
            positions = [
                {
                    "symbol": r["stock_code"],
                    "quantity": r["quantity"],
                    "market_value": round(r["quantity"] * r["current_price"], 2),
                }
                for r in rows
            ]
            cash_row = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
            cash = float(cash_row[0]) if cash_row else 0.0
            total_value = round(cash + sum(p["market_value"] for p in positions), 2)
            return {
                "summary": {
                    "cash": round(cash, 2),
                    "market_value": round(sum(p["market_value"] for p in positions), 2),
                    "total_value": total_value,
                    "position_count": len(positions),
                },
                "positions": positions,
            }
        finally:
            conn.close()

    def check_paper_trader_consistency(self, full: dict):
        contract = TAB_CONTRACTS["paper-trader"]
        snapshot = self._read_paper_trader_snapshot()
        if snapshot is None:
            self.warnings.append("paper_trading.db不存在，跳过模拟交易DB一致性")
            return
        full_port = full.get("portfolio", {})
        missing = [path for path in contract["paths"] if path.startswith("paperTrader.")]
        # paperTrader is loaded by the frontend as a second API; adapter mode compares
        # /api/full.portfolio against the same SQLite source instead.
        self.details["paper_trader_adapter_note"] = f"frontend secondary fields: {missing}"
        full_codes = sorted(str(p.get("symbol", "")) for p in full_port.get("positions", []))
        db_codes = sorted(str(p.get("symbol", "")) for p in snapshot.get("positions", []))
        total_diff = abs(float(full_port.get("total_value", 0) or 0) - snapshot["summary"]["total_value"])
        if full_codes != db_codes:
            self.issues.append(f"/api/full.portfolio与paper_trading.db持仓代码不一致: {full_codes} vs {db_codes}")
        elif total_diff > 1.0:
            self.issues.append(f"/api/full.portfolio总资产与paper_trading.db差异过大: {total_diff:.2f}")
        else:
            self.passes.append("/api/full.portfolio与paper_trading.db一致 ✅")

    def check_auxiliary_endpoints(self):
        from web_dashboard.data_adapter import (
            get_accuracy_trend,
            get_pipeline_timeline,
            get_stock_pool,
            get_predictions,
            get_calibration,
            get_task_reports,
        )
        trend = get_accuracy_trend()
        if not isinstance(trend.get("daily_records", []), list):
            self.issues.append("/api/accuracy-trend daily_records结构异常")
        else:
            self.passes.append("/api/accuracy-trend结构完整 ✅")

        pool = get_stock_pool()
        predictions = get_predictions()
        calibration = get_calibration()
        if not isinstance(pool.get("stocks", []), list) or not isinstance(predictions.get("predictions", []), list):
            self.issues.append("/api/screener依赖的pool/predictions结构异常")
        elif not isinstance(calibration.get("stocks", []), list):
            self.issues.append("/api/screener依赖的calibration结构异常")
        else:
            self.passes.append("/api/screener数据源结构完整 ✅")

        pipeline = get_pipeline_timeline()
        first_task = ""
        for phase in (pipeline.get("timeline") or {}).values():
            tasks = phase.get("tasks", []) if isinstance(phase, dict) else []
            if tasks:
                first_task = tasks[0].get("logical_id") or tasks[0].get("task_id") or ""
                break
        if first_task:
            reports = get_task_reports(first_task)
            if not isinstance(reports, list):
                self.issues.append("/api/task-reports返回非list")
            else:
                self.passes.append("/api/task-reports结构完整 ✅")
        else:
            self.warnings.append("Pipeline无任务，跳过task-reports结构检查")

    def check_browser_smoke(self, browser_url: str):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self.warnings.append(f"Playwright不可用，跳过浏览器smoke: {e}")
            return

        console_errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.goto(browser_url, wait_until="networkidle", timeout=15000)
                if "/login" in page.url:
                    self.warnings.append("Dashboard跳转登录页，浏览器smoke未验证tab内容")
                    browser.close()
                    return
                for tab in sorted(TAB_CONTRACTS):
                    page.locator(f'.nav-item[data-tab="{tab}"]').click(timeout=5000)
                    page.wait_for_timeout(300)
                    active = page.locator(f"#section-{tab}.active").count()
                    if active != 1:
                        self.issues.append(f"浏览器smoke: tab未激活 {tab}")
                loading_count = page.locator(".section.active .loading").count()
                if console_errors:
                    self.issues.append(f"浏览器console错误: {console_errors[:5]}")
                elif loading_count:
                    self.warnings.append("浏览器smoke: 当前tab仍有loading元素")
                else:
                    self.passes.append("浏览器逐tab smoke通过 ✅")
                browser.close()
        except Exception as e:
            self.issues.append(f"浏览器smoke失败: {str(e)[:200]}")

    def summary(self) -> dict:
        status = "passed"
        reason = ""
        if self.issues:
            status = "failed"
            reason = "; ".join(self.issues[:3])
        elif self.warnings:
            status = "warning"
            reason = "; ".join(self.warnings[:3])
        return {
            "audit_time": datetime.now().isoformat(),
            "status": status,
            "status_reason": reason,
            "issues": self.issues,
            "warnings": self.warnings,
            "passes": self.passes,
            "details": self.details,
            "issue_count": len(self.issues),
            "warning_count": len(self.warnings),
            "pass_count": len(self.passes),
            "health_score": max(0, 100 - len(self.issues) * 15 - len(self.warnings) * 5),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None, help="JSON报告路径")
    parser.add_argument("--browser-url", default="", help="可选：运行只读Playwright逐tab smoke")
    args = parser.parse_args()

    audit = DashboardContractAudit(browser_url=args.browser_url)
    report = audit.run_all()

    print("=" * 60)
    print("🖥️ Dashboard 合约审查")
    print(f"   ❌ {report['issue_count']} 问题")
    for item in report["issues"]:
        print(f"      - {item}")
    print(f"   ⚠️ {report['warning_count']} 警告")
    for item in report["warnings"]:
        print(f"      - {item}")
    print(f"   ✅ {report['pass_count']} 通过")
    print(f"   健康度评分: {report['health_score']}/100")
    print("=" * 60)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"📁 报告: {args.output}")

    if report["issue_count"] > 0:
        sys.exit(1)
    if report["warning_count"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
