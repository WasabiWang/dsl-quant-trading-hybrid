#!/usr/bin/env python3
"""
LPPL (Log-Periodic Power Law) 泡沫检测模型 v1.0
==============================================
基于 Didier Sornette (ETH Zurich) 的金融泡沫理论:
  ln p(t) = A + B*(tc - t)^m + C*(tc - t)^m * cos(ω*ln(tc - t) + φ)

参数含义:
  tc: 临界时间 (泡沫破裂点, 单位: 相对时间)
  m:  幂律指数 (0 < m < 1 表示超指数增长, 典型值 0.1-0.9)
  ω:  对数周期振荡频率 (典型值 5-15)
  A:  tc时刻的对数价格
  B:  幂律振幅 (B < 0 为上涨泡沫, B > 0 为反泡沫)
  C:  对数周期振幅 (应满足 |C| < |B|)
  φ:  相位 (典型值 0-2π)

使用方法:
  python3 lppl_model.py --symbol 科创50 --start 2024-01-01
  python3 lppl_model.py --symbol NVDA --start 2023-01-01
"""

import numpy as np
from scipy.optimize import differential_evolution, minimize
from scipy import stats
import warnings
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict
import json
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings('ignore')

# ============================================================================
# 核心 LPPL 模型
# ============================================================================

@dataclass
class LPPLParams:
    """LPPL 模型参数"""
    tc: float       # 临界时间
    m: float        # 幂律指数
    ω: float        # 对数周期频率
    A: float        # 对数价格基值
    B: float        # 幂律振幅
    C: float        # 对数周期振幅 (cos分量)
    phi: float      # 相位

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class BubbleDiagnostic:
    """泡沫诊断结果"""
    # 基础信息
    symbol: str
    analysis_date: str
    data_points: int
    date_range: Tuple[str, str]

    # LPPL拟合质量
    converged: bool
    r_squared: float
    rmse: float
    params: Optional[LPPLParams] = None

    # 泡沫判定
    bubble_detected: bool = False
    bubble_strength: float = 0.0       # 0-1, 泡沫强度
    crash_probability: float = 0.0     # 短期崩溃概率
    critical_date: Optional[str] = None # 预测临界日期
    days_to_critical: Optional[int] = None

    # 多个时间窗口的拟合
    window_results: List[Dict] = field(default_factory=list)

    # 价格回撤修正(prices passed but not stored; drawdown computed inline)
    current_price: float = 0.0            # 最新价格
    peak_90d_price: float = 0.0           # 90日高点
    drawdown_pct: float = 0.0             # 从90日高点的回撤幅度
    drawdown_relaxation: float = 0.0      # 回撤造成的崩溃概率下调比例

    # 诊断标签
    regime: str = "normal"    # normal / bubble_growth / bubble_mature / pre_crash / post_crash
    warnings: List[str] = field(default_factory=list)
    recommendation: str = ""


def lppl_log_price(t: np.ndarray, params: LPPLParams) -> np.ndarray:
    """
    计算 LPPL 模型的对数价格

    ln p(t) = A + B*(tc - t)^m * [1 + C*cos(ω*ln(tc - t) + φ)]
    """
    dt = params.tc - t
    if np.any(dt <= 0):
        return np.full_like(t, np.inf)

    power_term = params.B * (dt ** params.m)
    oscillation = 1 + params.C * np.cos(params.ω * np.log(dt) + params.phi)
    return params.A + power_term * oscillation


def lppl_components(t: np.ndarray, params: LPPLParams) -> Dict[str, np.ndarray]:
    """分解 LPPL 各组分用于诊断"""
    dt = np.clip(params.tc - t, 1e-10, None)
    power_term = params.B * (dt ** params.m)
    oscillation = params.C * np.cos(params.ω * np.log(dt) + params.phi)
    log_price = params.A + power_term * (1 + oscillation)
    price = np.exp(log_price)
    return {
        'dt': dt,
        'power_term': power_term,
        'oscillation': oscillation,
        'log_price': log_price,
        'price': price
    }


# ============================================================================
# LPPL 参数拟合 (使用差分进化 + 局部优化)
# ============================================================================

class LPPLFitError(Exception):
    """拟合异常"""
    pass


def fit_lppl(t_data: np.ndarray, log_price_data: np.ndarray,
             t_min: Optional[float] = None, t_max: Optional[float] = None,
             use_de: bool = True, verbose: bool = False) -> Tuple[LPPLParams, dict]:
    """
    用差分进化 + Nelder-Mead 拟合 LPPL 参数

    Args:
        t_data: 时间序列 (数值型, 如 days/weeks since epoch)
        log_price_data: 对数价格序列
        t_min/t_max: 时间范围约束 (可选)
        use_de: 是否先使用差分进化做全局搜索
        verbose: 是否打印优化细节

    Returns:
        (LPPLParams, fit_stats)
    """
    n = len(t_data)

    # 时间归一化到 [0, 1] 以避免数值问题
    t_min_actual = t_data[0]
    t_max_actual = t_data[-1]
    t_range = t_max_actual - t_min_actual

    # 设置参数边界 (基于文献建议)
    # tc: 最后时间点之后 [1%, 50%] 的数据长度范围
    # m: [0.01, 0.99]
    # ω: [1, 50]
    # A, B, C: 基于数据范围
    # phi: [0, 2π]

    log_mean = np.mean(log_price_data)
    log_std = np.std(log_price_data)

    if t_min is None:
        t_min = t_min_actual
    if t_max is None:
        t_max = t_max_actual

    tc_lower = t_max_actual + t_range * 0.005   # 未来0.5%
    tc_upper = t_max_actual + t_range * 0.50    # 未来50%

    bounds = [
        (tc_lower, tc_upper),                    # tc
        (0.01, 0.99),                            # m
        (1.0, 50.0),                             # ω
        (log_mean - 5 * log_std, log_mean + 5 * log_std),  # A
        (-5 * abs(log_std), 5 * abs(log_std)),   # B
        (0.0, 0.99),                             # C: 振荡振幅, 理论约束|C|<1(保证1+C*cos>0), |C|<|B|在cost中硬约束
        (0.0, 2 * np.pi),                        # phi
    ]

    def cost_function(params_vec):
        tc, m, omega, A, B, C, phi = params_vec

        # 约束检查
        if tc <= t_max_actual:
            return 1e10
        if m <= 0 or m >= 1:
            return 1e10

        p = LPPLParams(tc=tc, m=m, ω=omega, A=A, B=B, C=C, phi=phi)

        try:
            pred = lppl_log_price(t_data, p)
            if np.any(np.isnan(pred)) or np.any(np.isinf(pred)):
                return 1e10
            # RMSE
            err = np.sqrt(np.mean((pred - log_price_data) ** 2))
            # 硬约束: |C|必须 < |B| (否则振荡主导趋势, 无物理意义)
            if abs(C) >= abs(B) or abs(C) >= 1.0:
                return 1e10
            return err
        except Exception:
            return 1e10

    best_params = None
    best_cost = np.inf

    # Step 1: 差分进化 (全局搜索)
    if use_de:
        try:
            result_de = differential_evolution(
                cost_function,
                bounds,
                strategy='best1bin',
                maxiter=500,
                popsize=30,
                mutation=(0.5, 1.5),
                recombination=0.9,
                tol=1e-8,
                seed=42,
                polish=False,
            )
            if result_de.fun < best_cost:
                best_cost = result_de.fun
                best_params = result_de.x
            if verbose:
                print(f"  DE: cost={result_de.fun:.6f}, tc={result_de.x[0]:.1f}, m={result_de.x[1]:.3f}, ω={result_de.x[2]:.1f}")
        except Exception as e:
            if verbose:
                print(f"  DE failed: {e}")

    # Step 2: 局部优化 (Nelder-Mead + L-BFGS-B)
    if best_params is not None:
        init = best_params
    else:
        # 启发式初始值
        init = [
            t_max_actual + t_range * 0.15,   # tc: 15% ahead
            0.5,                              # m
            6.0,                              # ω
            log_price_data[-1],               # A
            -abs(log_std) * 0.5,             # B (负值 = 上涨泡沫)
            0.1,                              # C
            np.pi / 2,                        # phi
        ]

    # Nelder-Mead
    try:
        res_nm = minimize(
            cost_function, init, method='Nelder-Mead',
            options={'maxiter': 2000, 'xatol': 1e-8, 'fatol': 1e-8}
        )
        if res_nm.fun < best_cost:
            best_cost = res_nm.fun
            best_params = res_nm.x
    except Exception:
        pass

    if best_params is None:
        raise LPPLFitError("无法拟合LPPL模型")

    tc, m, omega, A, B, C, phi = best_params
    params = LPPLParams(tc=tc, m=m, ω=omega, A=A, B=B, C=C, phi=phi)

    # 计算拟合质量
    pred_log = lppl_log_price(t_data, params)
    ss_res = np.sum((log_price_data - pred_log) ** 2)
    ss_tot = np.sum((log_price_data - np.mean(log_price_data)) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    rmse = np.sqrt(ss_res / n)

    # 残差诊断 (Durbin-Watson)
    residuals = log_price_data - pred_log
    if len(residuals) > 2:
        dw = np.sum(np.diff(residuals) ** 2) / np.sum(residuals ** 2)
    else:
        dw = 2.0

    fit_stats = {
        'r_squared': r_squared,
        'rmse': rmse,
        'durbin_watson': dw,
        'cost': best_cost,
        'n_iterations': None  # Could pull from optimization results
    }

    return params, fit_stats


# ============================================================================
# 多窗口稳健性检验 (LPPL Confidence)
# ============================================================================

def multi_window_fit(t_data: np.ndarray, log_price_data: np.ndarray,
                     n_windows: int = 50, min_frac: float = 0.4,
                     verbose: bool = False) -> List[Dict]:
    """
    多时间窗口LPPL拟合 → 检验泡沫信号的稳健性

    策略: 随机抽取数据的不同子集 (起始点变化), 看LPPL参数是否稳定。
    如果不同窗口都检测到泡沫 → 信号更可靠
    """
    n_total = len(t_data)
    results = []

    for i in range(n_windows):
        # 随机选择起始点 (后60%范围内)
        start_idx = np.random.randint(0, int(n_total * (1 - min_frac)))
        end_idx = n_total

        t_sub = t_data[start_idx:end_idx]
        log_sub = log_price_data[start_idx:end_idx]

        if len(t_sub) < 20:
            continue

        try:
            params, stats = fit_lppl(t_sub, log_sub, use_de=(i < 5), verbose=False)

            # 判断是否检测到泡沫信号
            is_bubble = (
                params.B < 0 and           # 上涨泡沫
                0.01 < params.m < 0.99 and # 合理的幂律指数
                stats['r_squared'] > 0.5   # 拟合质量合格
            )

            results.append({
                'window_id': i,
                'start_idx': int(start_idx),
                'tc': params.tc,
                'm': params.m,
                'ω': params.ω,
                'B': params.B,
                'r_squared': stats['r_squared'],
                'is_bubble': is_bubble,
                'params': params.to_dict()
            })
        except Exception:
            continue

    return results


# ============================================================================
# 泡沫诊断主函数
# ============================================================================

def diagnose_bubble(prices: np.ndarray, dates: np.ndarray,
                    symbol: str = "UNKNOWN",
                    n_windows: int = 50,
                    min_window_frac: float = 0.4,
                    verbose: bool = True) -> BubbleDiagnostic:
    """
    对给定价格序列进行完整的泡沫诊断

    Args:
        prices: 价格序列
        dates: 日期序列 (float: days since epoch 或 ordinal)
        symbol: 标的标识
        n_windows: 多窗口检测次数
        min_window_frac: 最小窗口占比
        verbose: 是否打印进度

    Returns:
        BubbleDiagnostic 诊断结果
    """
    n = len(prices)
    if n < 50:
        return BubbleDiagnostic(
            symbol=symbol,
            analysis_date=datetime.now().isoformat()[:10],
            data_points=n,
            date_range=("N/A", "N/A"),
            converged=False,
            r_squared=0, rmse=0,
            warnings=["数据点不足 (需要≥50)"]
        )

    log_prices = np.log(prices)

    # 1. 预处理: 去除趋势 + 检测超指数增长
    # 时间轴: 用序号(交易日编号), tc是在此空间拟合
    # 但critical_date需要用实际日历天数进行映射
    t_norm = np.arange(n)
    # 计算实际日期间隔（处理周末/节假日缺口）
    try:
        parsed_dates = []
        for d in dates:
            if isinstance(d, (int, float)):
                parsed_dates.append(datetime.fromordinal(int(d)))
            else:
                parsed_dates.append(datetime.strptime(str(d)[:10], "%Y-%m-%d"))
        # 交易日历比例: 实际覆盖日历天数 / 数据点数
        cal_days = (parsed_dates[-1] - parsed_dates[0]).days
        cal_days = max(cal_days, 1)
        trade_to_cal_ratio = cal_days / (n - 1)  # 每个"交易单位"≈多少日历天
    except Exception:
        trade_to_cal_ratio = 365.0 / 252.0  # 默认用A股年化比例
    # 检测是否处于超指数增长阶段 (对数价格二阶导 > 0)
    log_returns = np.diff(log_prices)
    second_deriv = np.diff(log_returns)
    super_exponential_frac = np.mean(second_deriv > 0)

    # 2. 多窗口拟合
    window_results = multi_window_fit(
        t_norm, log_prices,
        n_windows=n_windows, min_frac=min_window_frac,
        verbose=verbose
    )

    # 3. 统计泡沫信号稳定性
    bubble_count = sum(1 for r in window_results if r['is_bubble'])
    bubble_ratio = bubble_count / len(window_results) if window_results else 0

    # 收集所有检测到泡沫的窗口参数
    bubble_params = [r for r in window_results if r['is_bubble']]

    # 4. 主拟合 (全数据)
    try:
        main_params, main_stats = fit_lppl(t_norm, log_prices, use_de=True, verbose=verbose)
        converged = main_stats['r_squared'] > 0.3
    except LPPLFitError:
        main_params = None
        main_stats = {'r_squared': 0, 'rmse': 0}
        converged = False

    diag = BubbleDiagnostic(
        symbol=symbol,
        analysis_date=datetime.now().isoformat()[:10],
        data_points=n,
        date_range=(str(dates[0])[:10], str(dates[-1])[:10]),
        converged=converged,
        r_squared=main_stats['r_squared'],
        rmse=main_stats['rmse'],
        params=main_params,
        window_results=bubble_params
    )

    # 5. 泡沫判定
    diag.bubble_strength = bubble_ratio

    if bubble_ratio >= 0.6 and main_params and main_params.B < 0:
        diag.bubble_detected = True
        diag.regime = "bubble_mature"

        # 计算预测临界日期
        if isinstance(dates[-1], (int, float)):
            # dates are ordinals
            last_date = datetime.fromordinal(int(dates[-1]))
        else:
            last_date = datetime.strptime(str(dates[-1])[:10], "%Y-%m-%d")

        # tc 是序号空间(t_norm)中的索引, 需要映射为实际日历天数
        tc_index = main_params.tc
        days_ahead_trade = tc_index - t_norm[-1]
        # 用交易日→日历日比例换算
        days_ahead_cal = days_ahead_trade * trade_to_cal_ratio
        if 0 < days_ahead_cal < len(dates) * 2.0:  # 放宽上限最多到2倍数据长度
            critical_date = last_date + timedelta(days=int(days_ahead_cal))
            diag.critical_date = critical_date.strftime("%Y-%m-%d")
            diag.days_to_critical = int(days_ahead_cal)

            # 短期崩溃概率: 基于拟合质量 × 信号稳定性 × 接近tc的程度
            # 用 Monte Carlo 风格的组合评分取代原来的线性映射
            # 因子1: 拟合质量(R²归一化)
            quality_factor = min(1.0, max(0.0, (diag.r_squared - 0.3) / 0.5))
            # 因子2: 信号稳定性(多窗口泡沫比例)
            stability_factor = diag.bubble_strength
            # 因子3: 接近tc的程度(越接近概率越高) — 用日历天而非序号
            time_proximity = 1.0 - min(1.0, days_ahead_cal / 252.0 * 2.0)  # 2年内线性递减
            # 综合: 三个因子加权
            crash_prob = 0.4 * quality_factor + 0.35 * stability_factor + 0.25 * time_proximity
            # 上限: 即使全部信号完美, crash_prob也不超过0.90 (保留不确定度)
            diag.crash_probability = min(0.90, max(0.05, crash_prob))

            # ===== Price Drawdown Relaxation =====
            # 当指数从高点显著回撤时，泡沫风险已部分释放，降低崩溃概率
            # 当指数接近高点时，泡沫压力积累，上调崩溃概率
            # 参见: 2026-07-29 James指出LPPL不响应22%跌幅的修正
            _dd_window = min(90, len(prices))
            _peak = float(np.max(prices[-_dd_window:]))
            _curr = float(prices[-1])
            _dd = _curr / _peak - 1.0  # 负值=回撤
            diag.current_price = _curr
            diag.peak_90d_price = _peak
            diag.drawdown_pct = _dd

            if _dd < -0.15:
                # 回撤>15%: 线性下调崩溃概率
                # -15%→下调0%, -40%→下调80%, 更低→cap 80%
                _relax = min(0.80, max(0.0, (abs(_dd) - 0.15) / 0.25 * 0.80))
                diag.drawdown_relaxation = _relax
                diag.crash_probability *= (1.0 - _relax)
                diag.crash_probability = max(0.05, diag.crash_probability)
                diag.warnings.append(
                    f"价格回撤{abs(_dd)*100:.0f}%: 崩溃概率下调{_relax*100:.0f}%")
            elif _dd > -0.03:
                # 接近高点(3%以内): 小幅上调(bubble building)
                _boost = 0.15
                diag.crash_probability = min(0.90, diag.crash_probability * (1.0 + _boost))
                diag.warnings.append(f"价格接近高点: 崩溃概率上调{_boost*100:.0f}%")
        else:
            diag.warnings.append(f"tc估计异常: days_ahead={days_ahead:.0f}")

    elif bubble_ratio >= 0.3:
        diag.regime = "bubble_growth"
        diag.warnings.append("泡沫信号弱，可能处于早期累积阶段")
    elif super_exponential_frac > 0.5:
        diag.regime = "bubble_growth"
        diag.warnings.append("检测到超指数增长但LPPL拟合不稳定")
    else:
        diag.regime = "normal"

    # 诊断标签
    if main_params:
        if main_params.m < 0.2:
            diag.warnings.append(f"m={main_params.m:.3f}偏低, 超指数增长极端, 泡沫信号强烈(注意假阳性)")
        if main_params.m > 0.8:
            diag.warnings.append(f"m={main_params.m:.3f}偏大，增长速度接近线性")

    # 建议
    if diag.bubble_detected and diag.crash_probability > 0.7:
        diag.recommendation = "🔴 高风险: 泡沫成熟期，建议大幅减仓或对冲"
    elif diag.bubble_detected:
        diag.recommendation = "🟠 中风险: 检测到泡沫信号，建议逐步降低仓位并设置止损"
    elif diag.bubble_strength > 0.3:
        diag.recommendation = "🟡 关注: 早期泡沫信号，建议密切监控"
    else:
        diag.recommendation = "🟢 正常: 未检测到明显泡沫信号"

    # 如果回撤修正后使推荐等级改变, 追加说明
    if diag.drawdown_pct < -0.15 and diag.bubble_detected and diag.crash_probability < 0.5:
        diag.recommendation += " (价格回撤修正)"

    return diag


# ============================================================================
# 批量检测多个标的
# ============================================================================

def batch_bubble_check(symbols_data: Dict[str, Tuple[np.ndarray, np.ndarray]],
                       verbose: bool = True) -> List[BubbleDiagnostic]:
    """
    批量泡沫检测

    Args:
        symbols_data: {symbol: (prices_array, dates_array)} 字典

    Returns:
        诊断结果列表 (按泡沫强度降序)
    """
    results = []
    for symbol, (prices, dates) in symbols_data.items():
        if verbose:
            print(f"🔍 检测 {symbol}...")
        try:
            diag = diagnose_bubble(prices, dates, symbol=symbol,
                                   n_windows=30, verbose=False)
            results.append(diag)
            if verbose:
                status = "🔴 BUBBLE" if diag.bubble_detected else "🟢 OK"
                print(f"  {status} | strength={diag.bubble_strength:.2f} "
                      f"| R²={diag.r_squared:.3f} "
                      f"| regime={diag.regime}")
        except Exception as e:
            if verbose:
                print(f"  ❌ {symbol} 检测失败: {e}")

    results.sort(key=lambda x: x.bubble_strength, reverse=True)
    return results


# ============================================================================
# CLI 入口
# ============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LPPL泡沫检测工具")
    parser.add_argument("--symbol", default="科创50", help="标的名称")
    parser.add_argument("--start", default="2024-01-01", help="数据起始日期")
    parser.add_argument("--end", default=None, help="数据截止日期")
    parser.add_argument("--windows", type=int, default=50, help="多窗口拟合次数")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()

    # 数据获取
    from akshare_config import get_price_data
    prices, dates = get_price_data(args.symbol, args.start, args.end)

    # 诊断
    diag = diagnose_bubble(
        np.array(prices), np.array(dates),
        symbol=args.symbol,
        n_windows=args.windows,
        verbose=True
    )

    if args.json:
        result = {
            'symbol': diag.symbol,
            'analysis_date': diag.analysis_date,
            'bubble_detected': diag.bubble_detected,
            'bubble_strength': diag.bubble_strength,
            'crash_probability': diag.crash_probability,
            'critical_date': diag.critical_date,
            'days_to_critical': diag.days_to_critical,
            'regime': diag.regime,
            'r_squared': diag.r_squared,
            'recommendation': diag.recommendation,
            'warnings': diag.warnings,
            'params': diag.params.to_dict() if diag.params else None,
            'drawdown_pct': diag.drawdown_pct,
            'drawdown_relaxation': diag.drawdown_relaxation,
            'current_price': diag.current_price,
            'peak_90d_price': diag.peak_90d_price,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  LPPL 泡沫诊断报告: {diag.symbol}")
        print(f"{'='*60}")
        print(f"  分析日期: {diag.analysis_date}")
        print(f"  数据区间: {diag.date_range[0]} → {diag.date_range[1]} ({diag.data_points}点)")
        print(f"  拟合质量: R²={diag.r_squared:.4f}, RMSE={diag.rmse:.4f}")
        print(f"  泡沫检测: {'🔴 是' if diag.bubble_detected else '🟢 否'}")
        print(f"  泡沫强度: {diag.bubble_strength:.2%}")
        print(f"  当前状态: {diag.regime}")
        print(f"  崩溃概率: {diag.crash_probability:.1%}")
        if diag.critical_date:
            print(f"  临界日期: {diag.critical_date} (距今{diag.days_to_critical}天)")
        if diag.params:
            print(f"\n  LPPL参数:")
            print(f"    tc={diag.params.tc:.1f}, m={diag.params.m:.3f}, ω={diag.params.ω:.1f}")
            print(f"    A={diag.params.A:.4f}, B={diag.params.B:.4f}, C={diag.params.C:.4f}")
        if diag.warnings:
            print(f"\n  ⚠️ 警告:")
            for w in diag.warnings:
                print(f"    - {w}")
        print(f"\n  📋 建议: {diag.recommendation}")
        if diag.drawdown_pct != 0:
            print(f"  📉 价格回撤: {diag.drawdown_pct*100:.0f}% (90日高点={diag.peak_90d_price:.0f})")
        if diag.drawdown_relaxation > 0:
            print(f"  🌀 回撤修正: 崩溃概率下调{diag.drawdown_relaxation*100:.0f}%")