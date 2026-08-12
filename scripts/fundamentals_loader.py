#!/usr/bin/env python3
"""
fundamentals_loader.py — 麦蕊API 基本面数据加载器
为 DSL 量化系统提供实时基本面特征。

功能:
- fetch_fundamentals_mairui(code) → dict: 对单只标的获取最新基本面
- 内置缓存, 避免重复API调用
- 缺失值统一处理为 0.0 (保持梯度可计算)

使用:
    from scripts.fundamentals_loader import fetch_fundamentals_mairui
    feats = fetch_fundamentals_mairui('600519')
"""
import os
import sys
import json
import numpy as np
from typing import Dict, Optional

# ── 缓存 (进程级, 避免同一进程重复拉取) ──
_fundamentals_cache: Dict[str, dict] = {}

# ── 持久化缓存路径 (跨进程, JSON文件) ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FILE = os.path.join(PROJECT_ROOT, "cache", "fundamentals_cache.json")


def _raw_to_float(v) -> float:
    """安全转换为float, 无效值返回nan"""
    try:
        if v is None or v == '-' or v == '' or v == '--' or v == 'N/A':
            return np.nan
        v_clean = str(v).replace(',', '')
        return float(v_clean)
    except (ValueError, TypeError):
        return np.nan


def _normalize_mairui_code(code: str) -> str:
    """将A股6位代码转为麦蕊格式 (带.SH或.SZ后缀)"""
    code = code.strip().split('.')[0]  # 去掉已有后缀
    if code.startswith(('6', '68', '688')):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


def fetch_fundamentals_mairui(code: str) -> dict:
    """
    通过麦蕊API获取股票最新基本面数据。
    返回 7 维特征字典, 缺失值设为 0.0。

    返回:
        {
            "roe": float,          # 净资产收益率 (%)
            "eps": float,          # 基本每股收益
            "bps": float,          # 每股净资产
            "cfps": float,         # 每股经营现金流
            "revenue_growth": float,  # 营业收入同比增长 (%)
            "gross_margin": float,    # 销售毛利率 (%)
            "capex_ratio": float,  # 资本支出/营业收入 比率
        }
    """
    # 1. 检查进程级缓存
    if code in _fundamentals_cache:
        return _fundamentals_cache[code]

    # 2. 检查文件级缓存
    _load_file_cache()
    if code in _fundamentals_cache:
        return _fundamentals_cache[code]

    # 3. 调用API
    mr_code = _normalize_mairui_code(code)
    result = {
        "roe": 0.0,
        "eps": 0.0,
        "bps": 0.0,
        "cfps": 0.0,
        "revenue_growth": 0.0,
        "gross_margin": 0.0,
        "capex_ratio": 0.0,
    }

    try:
        # ── financial/pershareindex ──
        from config.mairui_api_config import get_financial_indicators, get_income_statement, get_cashflow_statement

        pe_data = get_financial_indicators(mr_code, limit=1)
        if pe_data and isinstance(pe_data, list) and len(pe_data) > 0:
            latest = pe_data[0]

            roe = _raw_to_float(latest.get("jzcsyl"))
            eps = _raw_to_float(latest.get("jbmgsy"))
            bps = _raw_to_float(latest.get("mgjzc"))
            cfps = _raw_to_float(latest.get("mgjyhdxjl"))  # 每股经营现金流量
            gross_margin = _raw_to_float(latest.get("xsmlv"))  # 销售毛利率
            rev_growth = _raw_to_float(latest.get("zyyrsrzz"))  # 营业收入增长率
            # 每股资本公积作为capex的代理（可用作资本充足率）
            mgzbgjj = _raw_to_float(latest.get("mgzbgjj"))

            if not np.isnan(roe):
                result["roe"] = roe
            if not np.isnan(eps):
                result["eps"] = eps
            if not np.isnan(bps):
                result["bps"] = bps
            if not np.isnan(cfps):
                result["cfps"] = cfps
            if not np.isnan(gross_margin):
                result["gross_margin"] = gross_margin
            if not np.isnan(rev_growth):
                result["revenue_growth"] = rev_growth

        # ── financial/income 计算 capex_ratio ──
        # capex_ratio = (购建固定资产/无形资产支付现金) / 营业收入
        # 先从cashflow获取资本支出, 再从income获取营收
        try:
            cf_data = get_cashflow_statement(mr_code, limit=1)
            if cf_data and isinstance(cf_data, list) and len(cf_data) > 0:
                cf_latest = cf_data[0]
                # 购建固定资产、无形资产和其他长期资产支付的现金
                capex = _raw_to_float(cf_latest.get("gjgdzcwxzhqtqctzzfdxj"))
                if not np.isnan(capex) and capex > 0:
                    # 获取同期的营业收入
                    inc_data = get_income_statement(mr_code, limit=1)
                    if inc_data and isinstance(inc_data, list) and len(inc_data) > 0:
                        revenue = _raw_to_float(inc_data[0].get("yysr"))
                        if not np.isnan(revenue) and revenue > 0:
                            capex_ratio = capex / revenue
                            result["capex_ratio"] = round(float(capex_ratio), 6)
        except Exception:
            pass  # capex_ratio 保持为 0.0

        # 文件缓存持久化
        _fundamentals_cache[code] = result
        _save_file_cache()
        return result

    except Exception as e:
        print(f"  ⚠️ 基本面获取失败 {code}: {e}")
        _fundamentals_cache[code] = result
        return result


def fetch_fundamentals_batch(codes: list) -> dict:
    """批量获取基本面数据, 返回 {code: dict}"""
    results = {}
    for code in codes:
        results[code] = fetch_fundamentals_mairui(code)
    return results


def _load_file_cache():
    """从磁盘加载基本面缓存"""
    global _fundamentals_cache
    if _fundamentals_cache:  # 已加载
        return
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _fundamentals_cache.update(data)
    except Exception:
        pass


def _save_file_cache():
    """将基本面缓存写入磁盘"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_fundamentals_cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def clear_cache():
    """清空缓存（测试/调试用）"""
    global _fundamentals_cache
    _fundamentals_cache = {}
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)


if __name__ == "__main__":
    # 快速测试
    test_codes = ["600519", "000858", "002594", "300750", "688525", "999999"]
    for code in test_codes:
        feats = fetch_fundamentals_mairui(code)
        has_data = sum(1 for v in feats.values() if v != 0.0)
        print(f"{code}: {has_data}/7 有值 → roe={feats['roe']:.2f} eps={feats['eps']:.2f} bps={feats['bps']:.2f} cfps={feats['cfps']:.2f} growth={feats['revenue_growth']:.2%} margin={feats['gross_margin']:.2%} capex={feats['capex_ratio']:.4f}")
