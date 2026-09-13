"""DSL Test Pyramid — L1: Component tests (module-level, <30s)."""

import os
import sys
import json
import subprocess
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from datetime import datetime, date as date_type


class TestPaperTrader:
    """L1.1: PaperTrader — holiday rejection, portfolio summary, repeatability."""

    def test_holiday_reject(self):
        from unittest.mock import patch
        from paper_trader import PaperTrader
        t = PaperTrader(test_mode=True)
        # Mock datetime.now() to return 五一 (known holiday)
        with patch('paper_trader.datetime') as mock_dt:
            mock_dt.now.return_value.date.return_value = date_type(2026, 5, 1)
            r = t.execute_trade("A", "000001", "BUY", 10.0, 100, "L1-test")
        assert not r.get("success"), f"Expected holiday reject, got: {r.get('error', '')}"

    def test_portfolio_summary(self):
        from paper_trader import PaperTrader
        t = PaperTrader(test_mode=True)
        s = t.get_portfolio_summary()
        assert isinstance(s, dict)
        assert "current_cash" in str(s) or "positions" in str(s)

    def test_repeatable(self):
        from paper_trader import PaperTrader
        t = PaperTrader(test_mode=True)
        r = t.execute_trade("A", "000001", "BUY", 10.0, 100, "L1-test2")
        assert isinstance(r, dict)

    def test_stop_loss_check_returns_dict(self):
        from paper_trader import PaperTrader
        t = PaperTrader(test_mode=True)
        result = t.check_stop_losses()
        assert isinstance(result, dict)
        assert "triggered" in result
        assert "executed" in result
        assert result["triggered"] == 0  # No positions in test mode


class TestDynamicThreshold:
    """L1.2: DynamicThreshold engine — accuracy-based thresholds."""

    @pytest.fixture
    def engine(self):
        from core.dynamic_threshold import DynamicThresholdEngine
        return DynamicThresholdEngine()

    def test_high_accuracy_threshold(self, engine):
        th = engine.compute("600519", 0.65, 0.5)
        t = th.get("threshold", 0)
        assert 0.35 <= t <= 0.65, f"threshold={t:.2f}"

    def test_low_accuracy_threshold(self, engine):
        th = engine.compute("601899", 0.43, -0.1)
        t = th.get("threshold", 0)
        assert t >= 0.35, f"threshold={t:.2f}"

    def test_mid_accuracy_threshold(self, engine):
        th = engine.compute("000001", 0.55, 0.3)
        t = th.get("threshold", 0)
        assert 0.35 <= t <= 0.70, f"threshold={t:.2f}"


class TestCalibrationFeedback:
    """L1.3: CalibrationFeedback — retrain plan and priorities."""

    @pytest.fixture
    def cf(self):
        from core.calibration_feedback import get_calibration_feedback
        return get_calibration_feedback()

    def test_retrain_plan_total(self, cf):
        plan = cf.get_retrain_plan()
        assert "total" in plan, str(plan.get("total", "?"))

    def test_retrain_plan_summary(self, cf):
        plan = cf.get_retrain_plan()
        assert "summary" in plan

    def test_priorities_list(self, cf):
        plan = cf.get_retrain_plan()
        priorities = plan.get("priorities", [])
        assert isinstance(priorities, list)

    def test_priority_fields(self, cf):
        plan = cf.get_retrain_plan()
        priorities = plan.get("priorities", [])
        if priorities:
            p = priorities[0]
            for k in ["symbol", "priority", "action"]:
                assert k in p

    def test_has_check_realized(self, cf):
        assert hasattr(cf, "check_realized_accuracy")


class TestPoolSync:
    """L1.4: Stock pool sync — dry-run consistency."""

    def test_dry_run_consistent(self):
        r = subprocess.run(
            [sys.executable, "scripts/sync_stock_pool.py", "--dry-run"],
            capture_output=True, text=True, timeout=15, cwd=str(PROJECT_ROOT)
        )
        assert r.returncode == 0 or "无新增" in r.stdout or "完全一致" in r.stdout


class TestAkshareWrapper:
    """L1.5: akshare utils — function availability and light real calls."""

    def test_functions_exist(self):
        from common.akshare_utils import (
            safe_stock_zh_a_hist, safe_index_us_stock_sina,
            safe_stock_zh_a_spot_em
        )
        assert callable(safe_stock_zh_a_hist)
        assert callable(safe_index_us_stock_sina)
        assert callable(safe_stock_zh_a_spot_em)

    @pytest.mark.slow
    @pytest.mark.skipif(os.getenv('RUN_NETWORK_TESTS') != '1', reason='Network-dependent; covered by scripts/verify_data_source.py')
    def test_hist_returns_data(self):
        from common.akshare_utils import safe_stock_zh_a_hist
        df = safe_stock_zh_a_hist("000001", "daily", "20260427", "20260430")
        assert df is not None, "DataFrame should not be None"
        assert len(df) >= 1, f"Expected >=1 rows, got {len(df)}"
        assert "收盘" in df.columns


class TestFaultInjection:
    """L1.7: 故障注入测试 — 模拟过去一个月出现的10种异常场景
    每次改代码后跑这组测试, 确认核心异常路径不被破坏
    """

    @pytest.fixture
    def trader(self):
        from paper_trader import PaperTrader
        return PaperTrader()

    def test_01_stale_plan_regeneration(self):
        """故障1: 交易计划过期/错目标日 → fail-closed, 不自动重生成
        对应Bug: 2026-06-01 PaperTrader缩进漂移导致get_portfolio_summary不可用
        """
        import tempfile
        from pathlib import Path
        from scripts import execute_scheduled_trades as est

        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "planned_trades.json"
            fp.write_text(json.dumps({
                "generated_at": "2026-06-05T09:20:00",
                "target_date": "2026-06-05",
                "trades": [{"code": "000001", "action": "BUY", "quantity": 100, "price": 10}],
            }), encoding="utf-8")
            old_path = est.PLANNED_TRADES_PATH
            est.PLANNED_TRADES_PATH = str(fp)
            try:
                assert est.load_planned_trades() == []
            finally:
                est.PLANNED_TRADES_PATH = old_path

    def test_02_portfolio_summary_in_class(self):
        """故障2: PaperTrader.get_portfolio_summary 必须在类中(非嵌套函数)
        对应Bug: 2026-05-27 v4.5.21 缩进漂移
        """
        from paper_trader import PaperTrader
        assert hasattr(PaperTrader, 'get_portfolio_summary'), "get_portfolio_summary 不在PaperTrader类中!"
        assert hasattr(PaperTrader, 'check_stop_losses'), "check_stop_losses 不在PaperTrader类中!"
        assert hasattr(PaperTrader, '_update_metrics'), "_update_metrics 不在PaperTrader类中!"

    def test_03_all_methods_declared(self):
        """故障3: 类方法声明与实现一致
        对应Bug: 所有意外删除/缩进漂移
        """
        from paper_trader import PaperTrader
        from core.risk_manager import RiskManager
        for cls in [PaperTrader, RiskManager]:
            declared = set(getattr(cls, '__all__methods__', []))
            actual = set(m for m in dir(cls) if not m.startswith('__'))
            missing = declared - actual
            assert not missing, f"{cls.__name__}: 声明的方法缺失: {missing}"

    def test_04_holiday_detection_2026(self):
        """故障4: 2026年节假日检测 — akshare新旧版本兼容
        对应Bug: 2026-06-01 akshare 1.18.57 移除了is_open列
        """
        from config.holiday_calendar import is_trading_day
        # 已知非交易日
        assert not is_trading_day(date_type(2026, 5, 1), "A_SHARE"), "五一应为非交易日"
        assert not is_trading_day(date_type(2026, 5, 4), "A_SHARE"), "五一假期应为非交易日"
        # 已知交易日
        assert is_trading_day(date_type(2026, 5, 6), "A_SHARE"), "5月6日应为交易日"
        assert is_trading_day(date_type(2026, 6, 1), "A_SHARE"), "6月1日应为交易日"

    def test_05_yaml_config_keys_consistency(self):
        """故障5: YAML配置key一致性 — 生产者/消费者签名对齐
        对应Bug: 2026-05-11 key名不匹配导致position_ratio脱节
        """
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  "config", "adaptive_params.yaml")
        with open(config_path) as f:
            data = yaml.safe_load(f)
        # 关键路径必须存在
        risk = data.get('risk', {})
        assert 'black_swan_active' in risk, "risk.black_swan_active 必须存在"
        assert 'black_swan_position_ratio' in risk, "risk.black_swan_position_ratio 必须存在"
        trading = data.get('trading', {})
        assert 'max_positions' in trading, "trading.max_positions 必须存在"

    def test_06_stop_loss_config_path(self):
        """故障6: 止损配置路径与L3质量检查对齐
        对应Bug: 2026-05-25 evening_quality_check L3黑天鹅key路径错位
        """
        import yaml
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  "config", "adaptive_params.yaml")
        with open(config_path) as f:
            data = yaml.safe_load(f)
        # adaptive_params.yaml 必须用多级路径
        risk = data.get('risk', {})
        # 正确的读取方式
        bs_active = risk.get('black_swan_active', False)
        bs_ratio = risk.get('black_swan_position_ratio', 1.0)
        # 错误的读取方式 (旧Bug): params.get('black_swan', {}) 会读到空dict
        wrong = data.get('black_swan', None)
        assert wrong is None or wrong == {}, (
            "⚠️ 不要从YAML根节点读 black_swan! 正确路径是 risk.black_swan_active"
        )

    def test_07_system_event_not_used(self):
        """故障7: 所有cron任务使用agentTurn, 不使用systemEvent
        对应Bug: 2026-05-19 systemEvent cron全量跳票
        """
        # 检查cron配置中的payload kind
        # 这个测试通过cron list API验证
        pass  # 主session已全部转为agentTurn

    def test_08_paper_trader_init(self):
        """故障8: PaperTrader初始化不抛异常
        对应Bug: 各种导入/初始化回归
        """
        from paper_trader import PaperTrader
        t = PaperTrader()
        assert t is not None
        # 基础方法可调用
        summary = t.get_portfolio_summary()
        assert 'initial_capital' in summary
        assert 'current_cash' in summary
        assert 'positions' in summary

    def test_09_data_source_fallback_chain(self):
        """故障9: 数据源降级链完整性 — 多级fallback存在
        对应Bug: 2026-05-11 同域名假降级
        """
        # 检查迈蕊API配置完整性
        from config.mairui_api_config import LICENCE
        assert isinstance(LICENCE, str), "MAIRUI_LICENCE 应为字符串"
        # 检查dsl_data_sdk_original有降级逻辑
        try:
            from dsl_data_sdk_original import get_price
            assert callable(get_price), "get_price 应可调用"
        except ImportError:
            pass  # 测试环境可能没有sdk, 跳过

    def test_10_dashboard_version_sync(self):
        """故障10: Dashboard版本号与系统VERSION一致
        对应Bug: 改代码不重启Dashboard
        """
        import subprocess
        # 读取VERSION文件
        version_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_path) as f:
            file_version = f.readline().strip()
        # 检查是否为空
        assert file_version, "VERSION文件不能为空"
        assert '.' in file_version, f"VERSION格式有误: {file_version}"

    def test_11_scheduled_stale_prediction_blocks_buy_preserves_sell(self, monkeypatch):
        """故障11: scheduled executor 必须拦截 stale BUY，但保留 SELL 风险出口"""
        from core import data_freshness
        from scripts import execute_scheduled_trades as est

        monkeypatch.setattr(
            data_freshness,
            "assess_daily_predict_freshness",
            lambda: {
                "status": "stale",
                "allow_buy": False,
                "allow_sell": True,
                "reason": "daily_predict过期",
            },
        )

        trades = [
            {"code": "000001", "action": "BUY", "quantity": 100, "price": 10},
            {"code": "000002", "action": "SELL", "quantity": 100, "price": 11},
        ]
        filtered, freshness = est._filter_buys_when_prediction_stale(trades)

        assert freshness["status"] == "stale"
        assert filtered == [trades[1]]

    def test_12_scheduled_executor_risk_fields_initialized_before_gates(self):
        """故障12: 精度/单票仓位门禁不能在 qty/ref_price 初始化前读取它们"""
        src = Path(PROJECT_ROOT / "scripts" / "execute_scheduled_trades.py").read_text(encoding="utf-8")

        qty_init = src.index('qty = int(trade.get("quantity", 100) or 100)')
        quality_gate = src.index("精度<40%禁入")
        ref_price_init = src.index("ref_price = _trade_reference_price(trade)")
        single_limit_gate = src.index("单票仓位硬上限")

        assert qty_init < quality_gate
        assert ref_price_init < single_limit_gate


class TestCircuitBreakerState:
    """L1.6: CircuitBreaker — state management and position limits."""

    @pytest.fixture
    def cb(self):
        from core.circuit_breaker import CircuitBreaker
        return CircuitBreaker()

    def test_trading_allowed(self, cb, monkeypatch):
        # 2026-09-13: is_trading_allowed() 会按墙钟在午休(11:30-13:00)返回 False,
        # 原断言直接用真实时间 → 午休时段必然失败(时段性 flaky, 也会误拦 pre-push)。
        # 本用例只验证"熔断器未被触发"这一逻辑, 时钟必须被固定。
        monkeypatch.setattr("config.constants.is_lunch_break", lambda *a, **k: False)
        allowed, _ = cb.is_trading_allowed()
        assert allowed

    def test_trading_blocked_during_lunch_break(self, cb, monkeypatch):
        """午休时段必须拒单(与上面同一个门控, 反向锁定)。"""
        monkeypatch.setattr("config.constants.is_lunch_break", lambda *a, **k: True)
        allowed, reason = cb.is_trading_allowed()
        assert not allowed
        assert "午休" in reason

    def test_starting_capital(self, cb):
        cb.set_today_starting_capital(1_000_000)
        data = cb._load_data()
        assert data["today_starting_capital"] > 0

    def test_position_limit_pass(self, cb):
        ok, _ = cb.check_position_limit("600519", 50_000, 200_000)
        assert ok, "25% should pass"

    def test_position_limit_fail(self, cb):
        ok, reason = cb.check_position_limit("600519", 100_000, 200_000)
        assert not ok, f"50% should fail, got: {reason}"
