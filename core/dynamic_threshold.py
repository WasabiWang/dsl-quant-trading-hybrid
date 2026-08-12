#!/usr/bin/env python3
"""
core/dynamic_threshold.py — DSL v4.5.9 动态置信阈值引擎

三层因子架构:
  L1 模型质量因子: 根据模型精度/R²/样本量计算折扣系数
  L2 市场状态因子: 根据沪深300趋势/波动率调整
  L3 信号强度因子: 根据个股ATR/布林带宽度调整

最终阈值 = max(盈亏平衡精度+安全边际, 模型精度 × L1折扣 × L2系数 × L3系数)
硬底线: 永远不低于 35% (盈亏平衡30.4% + 5%安全边际)

使用方式:
  from core.dynamic_threshold import DynamicThresholdEngine
  engine = DynamicThresholdEngine()
  threshold = engine.compute(code="301308", accuracy=0.709, r2=-0.062)
"""
import os, sys, json, math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "adaptive_params.yaml"
CACHE_DIR = PROJECT_ROOT / "cache"

# ═══════════════════ 常量 ═══════════════════
# 盈亏平衡精度 (基于P/L=2.29): 1/(1+2.29) = 30.4%
BREAKEVEN_ACCURACY = 0.304
SAFETY_MARGIN = 0.05  # 安全边际
HARD_FLOOR = BREAKEVEN_ACCURACY + SAFETY_MARGIN  # 35%

# 默认固定阈值 (当动态引擎未启用时的回退值)
# 跟踪当前模型精度 (Phase 3: 56.72%) - 2%缓冲 = 54%
DEFAULT_FIXED_THRESHOLD = 0.54

# ═══════════════════ L1: 模型质量折扣系数 ═══════════════════
# 精度越高→折扣越低→阈值越低→更容易出信号
# 精度越低→折扣越高→阈值越高→更难出信号
TIER_HIGH   = {"min_acc": 0.60, "discount": 0.85, "label": "高精度"}
TIER_MEDIUM = {"min_acc": 0.50, "discount": 0.90, "label": "中精度"}
TIER_LOW    = {"min_acc": 0.00, "discount": 1.00, "label": "低精度"}


def _get_l1_discount(accuracy: float, r2: float = -1, sample_count: int = 0) -> dict:
    """计算L1模型质量折扣系数

    Args:
        accuracy: 模型方向精度 (0-1)
        r2: 模型R² (可为负)
        sample_count: 训练样本量 (用于微调)

    Returns:
        {"discount": float, "tier": str, "adjusted_accuracy": float}
    """
    base_accuracy = accuracy

    # R²加分: 正R²说明回归有意义，降低折扣
    if r2 > 0.05:
        accuracy = min(1.0, accuracy + 0.02)  # R²>0.05加2%精度
    elif r2 > 0:
        accuracy = min(1.0, accuracy + 0.01)  # R²>0加1%精度

    # 小样本惩罚: <500样本加3%折扣
    sample_penalty = 0.03 if 0 < sample_count < 500 else 0.0

    # 分档
    if accuracy >= TIER_HIGH["min_acc"]:
        discount = TIER_HIGH["discount"]
        tier = TIER_HIGH["label"]
    elif accuracy >= TIER_MEDIUM["min_acc"]:
        discount = TIER_MEDIUM["discount"]
        tier = TIER_MEDIUM["label"]
    else:
        discount = TIER_LOW["discount"]
        tier = TIER_LOW["label"]

    discount = min(1.0, discount + sample_penalty)

    return {
        "discount": round(discount, 4),
        "tier": tier,
        "adjusted_accuracy": round(accuracy, 4),
    }


# ═══════════════════ L2: 市场状态系数 ═══════════════════
# 强趋势→降低阈值(更容易出信号)
# 震荡市→提高阈值(更难出信号，噪音大)
REGIME_STRONG_TREND = 0.90   # 阈值打9折
REGIME_NEUTRAL      = 1.00   # 不调整
REGIME_CHOPPY       = 1.10   # 阈值加10%
REGIME_CRISIS       = 0.80   # 危机模式阈值打8折(趋势极端)


def _compute_regime(index_df: pd.DataFrame = None) -> dict:
    """计算当前市场状态

    Args:
        index_df: 沪深300最近60+天OHLCV数据 (可选，缺省从缓存读取)

    Returns:
        {"regime": str, "factor": float, "trend_strength": float, "volatility": float}
    """
    # 尝试从缓存读取沪深300数据
    if index_df is None:
        index_cache = CACHE_DIR / "price_sh000300.json"
        if index_cache.exists():
            try:
                with open(index_cache) as f:
                    idx_data = json.load(f)
                change_pct = idx_data.get("change_pct", 0)
                # 用单日涨跌幅做粗略判断
                if abs(change_pct) > 2.0:
                    regime = "crisis" if change_pct < -2.0 else "strong_trend"
                    factor = REGIME_CRISIS if regime == "crisis" else REGIME_STRONG_TREND
                elif abs(change_pct) > 0.8:
                    regime = "strong_trend"
                    factor = REGIME_STRONG_TREND
                elif abs(change_pct) < 0.3:
                    regime = "choppy"
                    factor = REGIME_CHOPPY
                else:
                    regime = "neutral"
                    factor = REGIME_NEUTRAL
                return {
                    "regime": regime,
                    "factor": factor,
                    "trend_strength": round(change_pct, 4),
                    "volatility": 0,
                    "source": "cache"
                }
            except Exception:
                pass

    # 有DataFrame时做更精确的计算
    if index_df is not None and len(index_df) >= 20:
        close = index_df["close"].astype(float)
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()

        # 趋势强度: MA5偏离MA20的幅度
        trend_strength = float((ma5.iloc[-1] - ma20.iloc[-1]) / ma20.iloc[-1]) if ma20.iloc[-1] != 0 else 0

        # 波动率: 20日收益率标准差
        ret = close.pct_change()
        vol_20 = float(ret.rolling(20).std().iloc[-1]) if len(ret) >= 20 else 0

        # 判断状态
        if vol_20 > 0.025:  # 日波动率>2.5%
            regime = "crisis"
            factor = REGIME_CRISIS
        elif abs(trend_strength) > 0.02:  # MA5偏离MA20 >2%
            regime = "strong_trend"
            factor = REGIME_STRONG_TREND
        elif abs(trend_strength) < 0.005:  # MA5≈MA20
            regime = "choppy"
            factor = REGIME_CHOPPY
        else:
            regime = "neutral"
            factor = REGIME_NEUTRAL

        return {
            "regime": regime,
            "factor": factor,
            "trend_strength": round(trend_strength, 4),
            "volatility": round(vol_20, 6),
            "source": "dataframe"
        }

    # 无数据时返回中性
    return {"regime": "neutral", "factor": REGIME_NEUTRAL, "trend_strength": 0, "volatility": 0, "source": "default"}


# ═══════════════════ L3: 个股波动率系数 ═══════════════════
VOL_LOW    = 0.95  # 缩量→阈值打95折
VOL_NORMAL = 1.00
VOL_HIGH   = 1.05  # 放量→阈值加5%


def _compute_vol_factor(code: str, stock_df: pd.DataFrame = None) -> dict:
    """计算个股波动率调整系数

    Args:
        code: 股票代码
        stock_df: 个股OHLCV数据 (可选)

    Returns:
        {"factor": float, "vol_ratio": float, "label": str}
    """
    if stock_df is not None and len(stock_df) >= 20:
        try:
            close = stock_df["close"].astype(float)
            high = stock_df["high"].astype(float)
            low = stock_df["low"].astype(float)

            # ATR计算
            tr = pd.DataFrame({
                "hl": high - low,
                "hc": abs(high - close.shift(1)),
                "lc": abs(low - close.shift(1))
            }).max(axis=1)
            atr_5 = tr.rolling(5).mean().iloc[-1]
            atr_20 = tr.rolling(20).mean().iloc[-1]

            if atr_20 > 0:
                vol_ratio = float(atr_5 / atr_20)
            else:
                vol_ratio = 1.0

            if vol_ratio > 1.3:
                factor = VOL_HIGH
                label = "放量波动"
            elif vol_ratio < 0.8:
                factor = VOL_LOW
                label = "缩量收敛"
            else:
                factor = VOL_NORMAL
                label = "正常"

            return {"factor": factor, "vol_ratio": round(vol_ratio, 4), "label": label}
        except Exception:
            pass

    return {"factor": VOL_NORMAL, "vol_ratio": 1.0, "label": "默认"}


# ═══════════════════ 主引擎 ═══════════════════

class DynamicThresholdEngine:
    """动态置信阈值引擎

    使用:
        engine = DynamicThresholdEngine()
        result = engine.compute(code="301308", accuracy=0.709, r2=-0.062)
        print(result["threshold"])  # 输出: 0.54
    """

    def __init__(self, enabled: bool = None):
        """初始化引擎

        Args:
            enabled: 是否启用动态阈值。None=从配置读取，True/False=强制
        """
        self.enabled = enabled
        self._load_config()
        self._load_calibration_adjustments()

    def _load_config(self):
        """从adaptive_params.yaml读取动态阈值配置"""
        try:
            import yaml
            with open(CONFIG_PATH) as f:
                params = yaml.safe_load(f)
            dt_config = params.get("dynamic_threshold", {})
            if self.enabled is None:
                self.enabled = dt_config.get("enabled", False)
            self.config = dt_config
        except Exception:
            self.enabled = self.enabled or False
            self.config = {}

    def _load_calibration_adjustments(self):
        """v4.5.3c: 从adaptive_params.yaml加载校准反馈的阈值调整
        每个退化标的有独立的hard_floor_boost
        """
        self._calib_adjustments = {}
        try:
            import yaml
            with open(CONFIG_PATH) as f:
                params = yaml.safe_load(f)
            calib = params.get("calibration", {})
            plan = calib.get("retrain_plan", {})
            for s in plan.get("priority_stocks", []):
                symbol = s.get("symbol", "")
                acc = s.get("current_accuracy", 0.5)
                priority = s.get("priority", "")
                # 精度越低，hard_floor_boost 越高
                floor_boost = 0.0
                if acc < 0.40:
                    floor_boost = 0.15
                elif acc < 0.45:
                    floor_boost = 0.10
                elif acc < 0.50:
                    floor_boost = 0.05
                if priority == "critical" and floor_boost < 0.10:
                    floor_boost = 0.10
                if floor_boost > 0:
                    self._calib_adjustments[symbol] = {
                        "hard_floor_boost": round(floor_boost, 2),
                        "accuracy": round(acc, 4),
                        "priority": priority,
                    }
        except Exception:
            pass

    def compute(self, code: str, accuracy: float, r2: float = -1,
                sample_count: int = 0, stock_df: pd.DataFrame = None,
                index_df: pd.DataFrame = None) -> dict:
        """计算某标的的动态置信阈值

        Args:
            code: 股票代码
            accuracy: 模型方向精度
            r2: 模型R²
            sample_count: 训练样本量
            stock_df: 个股OHLCV (用于L3)
            index_df: 沪深300 OHLCV (用于L2)

        Returns:
            {
                "threshold": float,        # 最终阈值
                "is_dynamic": bool,        # 是否为动态计算
                "l1": {...},               # L1详情
                "l2": {...},               # L2详情
                "l3": {...},               # L3详情
                "hard_floor": float,       # 硬底线
                "raw_product": float,      # 原始乘积(应用floor前)
            }
        """
        if not self.enabled:
            return {
                "threshold": DEFAULT_FIXED_THRESHOLD,
                "is_dynamic": False,
                "l1": {"discount": 1.0, "tier": "disabled", "adjusted_accuracy": accuracy},
                "l2": {"regime": "disabled", "factor": 1.0},
                "l3": {"factor": 1.0, "vol_ratio": 1.0, "label": "disabled"},
                "hard_floor": HARD_FLOOR,
                "raw_product": accuracy,
            }

        # L1: 模型质量
        l1 = _get_l1_discount(accuracy, r2, sample_count)

        # L2: 市场状态
        l2 = _compute_regime(index_df)

        # L3: 个股波动率
        l3 = _compute_vol_factor(code, stock_df)

        # 组合计算
        base_threshold = l1["adjusted_accuracy"] * l1["discount"]
        raw_product = base_threshold * l2["factor"] * l3["factor"]

        # v4.5.3c: 应用校准反馈硬底线提升
        effective_floor = HARD_FLOOR
        calib_adj = self._calib_adjustments.get(code, {})
        floor_boost = calib_adj.get("hard_floor_boost", 0.0)
        if floor_boost > 0:
            effective_floor = min(HARD_FLOOR + floor_boost, 0.60)  # cap at 60%
        
        # 应用硬底线
        threshold = max(effective_floor, raw_product)

        # 上限不超过90%
        threshold = min(0.90, threshold)

        return {
            "threshold": round(threshold, 4),
            "is_dynamic": True,
            "l1": l1,
            "l2": l2,
            "l3": l3,
            "hard_floor": round(effective_floor, 4),
            "hard_floor_base": round(HARD_FLOOR, 4),
            "hard_floor_boost": round(floor_boost, 2),
            "raw_product": round(raw_product, 4),
            "calibration_priority": calib_adj.get("priority", ""),
        }

    def compute_batch(self, models_info: list, index_df: pd.DataFrame = None) -> list:
        """批量计算所有标的的动态阈值

        Args:
            models_info: [{"code": str, "accuracy": float, "r2": float, ...}]
            index_df: 沪深300数据

        Returns:
            [{"code": str, "threshold": float, "is_dynamic": bool, ...}]
        """
        # L2 只算一次（所有标的市场状态相同）
        l2 = _compute_regime(index_df) if self.enabled else {"regime": "disabled", "factor": 1.0}

        results = []
        for m in models_info:
            code = m.get("code", "")
            accuracy = m.get("accuracy", m.get("dir_accuracy", 0.5))
            r2 = m.get("r2", -1)
            sample_count = m.get("sample_count", 0)

            if not self.enabled:
                result = {
                    "code": code,
                    "threshold": DEFAULT_FIXED_THRESHOLD,
                    "is_dynamic": False,
                }
            else:
                l1 = _get_l1_discount(accuracy, r2, sample_count)
                base_threshold = l1["adjusted_accuracy"] * l1["discount"]
                raw_product = base_threshold * l2["factor"]  # L3需要个股数据，批量模式暂不计算
                threshold = max(HARD_FLOOR, min(0.90, raw_product))

                result = {
                    "code": code,
                    "threshold": round(threshold, 4),
                    "is_dynamic": True,
                    "l1": l1,
                    "l2": l2,
                    "raw_product": round(raw_product, 4),
                }

            results.append(result)

        return results


# ═══════════════════ 集成点 ═══════════════════

def integrate_with_pool_predictor(predictor_result: dict, engine: DynamicThresholdEngine = None) -> dict:
    """将动态阈值集成到PoolPredictor的输出中

    用法: 在 pool_predictor.predict() 返回结果后调用
    predictor_result = predictor.predict(code, df, name)
    result = integrate_with_pool_predictor(predictor_result)

    会添加/更新:
      - dynamic_threshold: 该标的的动态阈值
      - confidence_level: 基于动态阈值重新判断 high/low
    """
    if engine is None:
        engine = DynamicThresholdEngine()

    code = predictor_result.get("code", "")
    accuracy = predictor_result.get("accuracy", 0.5)
    r2 = predictor_result.get("r2", -1)
    confidence = predictor_result.get("confidence", 0)
    signal = predictor_result.get("signal", "hold")

    dt_result = engine.compute(code=code, accuracy=accuracy, r2=r2)
    dynamic_threshold = dt_result["threshold"]

    # 用动态阈值重新判断置信级别
    is_high = confidence >= dynamic_threshold and signal != "hold"

    predictor_result["dynamic_threshold"] = dynamic_threshold
    predictor_result["dynamic_threshold_detail"] = dt_result
    predictor_result["confidence_level"] = "high" if is_high else "low"

    return predictor_result


# ═══════════════════ CLI入口 ═══════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DSL动态阈值引擎")
    parser.add_argument("--mode", choices=["status", "test", "batch"], default="test",
                       help="运行模式: status=查看配置, test=单标的测试, batch=批量计算")
    parser.add_argument("--code", default="301308", help="股票代码(test模式)")
    parser.add_argument("--accuracy", type=float, default=0.709, help="模型精度(test模式)")
    parser.add_argument("--r2", type=float, default=-0.062, help="模型R²(test模式)")
    parser.add_argument("--enable", action="store_true", help="强制启用动态阈值")
    args = parser.parse_args()

    if args.mode == "status":
        engine = DynamicThresholdEngine(enabled=args.enable or None)
        print(f"动态阈值引擎状态: {'✅ 已启用' if engine.enabled else '❌ 未启用(使用固定阈值0.54)'}")
        print(f"硬底线: {HARD_FLOOR:.1%}")
        print(f"盈亏平衡精度: {BREAKEVEN_ACCURACY:.1%} (基于P/L=2.29)")
        print(f"\nL1折扣表:")
        for tier in [TIER_HIGH, TIER_MEDIUM, TIER_LOW]:
            print(f"  {tier['label']}: 精度>={tier['min_acc']:.0%} → 折扣={tier['discount']}")
        print(f"\nL2市场系数:")
        print(f"  强趋势: ×{REGIME_STRONG_TREND} | 中性: ×{REGIME_NEUTRAL} | 震荡: ×{REGIME_CHOPPY} | 危机: ×{REGIME_CRISIS}")
        print(f"\nL3波动率系数:")
        print(f"  缩量: ×{VOL_LOW} | 正常: ×{VOL_NORMAL} | 放量: ×{VOL_HIGH}")

    elif args.mode == "test":
        engine = DynamicThresholdEngine(enabled=True)
        result = engine.compute(code=args.code, accuracy=args.accuracy, r2=args.r2)
        print(f"标的: {args.code}")
        print(f"模型精度: {args.accuracy:.1%}, R²: {args.r2}")
        print(f"\nL1 模型质量: 折扣={result['l1']['discount']}, 档位={result['l1']['tier']}, 调整后精度={result['l1']['adjusted_accuracy']:.1%}")
        print(f"L2 市场状态: {result['l2']['regime']}, 系数={result['l2']['factor']}")
        print(f"L3 波动率: 系数={result['l3']['factor']}, 标签={result['l3']['label']}")
        print(f"\n原始乘积: {result['raw_product']:.1%}")
        print(f"硬底线: {result['hard_floor']:.1%}")
        print(f"最终阈值: {result['threshold']:.1%}")

    elif args.mode == "batch":
        # 从模型池加载所有模型，批量计算
        sys.path.insert(0, str(PROJECT_ROOT))
        import joblib, glob

        engine = DynamicThresholdEngine(enabled=True)
        model_dir = PROJECT_ROOT / "models" / "pool"
        files = sorted(glob.glob(str(model_dir / "*_lgb_*.pkl")))

        models_info = []
        for f in files:
            try:
                d = joblib.load(f)
                code = os.path.basename(f).split("_")[0]
                name = "_".join(os.path.basename(f).split("_")[1:-2])
                models_info.append({
                    "code": code, "name": name,
                    "accuracy": d.get("dir_accuracy", 0),
                    "r2": d.get("r2", -1),
                })
            except Exception:
                pass

        results = engine.compute_batch(models_info)
        print(f"{'代码':8s} {'名称':10s} {'精度':6s} {'R²':8s} {'动态阈值':8s} {'原始乘积':8s} {'L1折扣':6s} {'市场系数':6s}")
        print("-" * 80)
        for r in sorted(results, key=lambda x: x.get("threshold", 0)):
            code = r["code"]
            mi = next((m for m in models_info if m["code"] == code), {})
            name = mi.get("name", "?")
            acc = mi.get("accuracy", 0)
            r2 = mi.get("r2", -1)
            l1_discount = r.get("l1", {}).get("discount", "?")
            l2_factor = r.get("l2", {}).get("factor", "?")
            print(f"{code:8s} {name:10s} {acc:5.1%} {r2:7.3f} {r['threshold']:7.1%} {r.get('raw_product',0):7.1%} {l1_discount} {l2_factor}")
