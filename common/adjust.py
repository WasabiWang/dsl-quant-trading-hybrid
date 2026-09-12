#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
common/adjust.py — 复权口径统一入口 (FX-2 · 阶段1时点一致性修复)

背景（阶段1.1审计 FX-2）:
- 评估路径 K线经 `dsl_data_sdk_original.get_kline` **硬编码 adjust="f"(前复权)**;
- 指标端点 `get_ma/macd/boll/kdj` 默认 `adjust="n"(不复权)` → 同一评估内口径混用;
- 前复权的"错"不在数值本身, 而在 **历史值会被未来除权改写 → 当时不可得 → 时点不一致**。

实测证据 (迈瑞 get_kline_history, 600519, 2001-08-27 close):
- 前复权(qfq): -312.54  ← 被数十年分红打成**负值**, pct_change/比率类特征全失真
- 后复权(hfq):   35.55  ← 上市首日价, **历史值固定**
- 不复权(raw):   35.55

统一基准: **后复权(hfq, adjust="b")** —— 历史价固定, 未来除权只放大其后的价格,
对任意时点 t, 其历史序列在 t 之后不再被改写 → 时点一致。
(原始价+复权因子 `sf` 为备选; 但实测迈蕊 `get_kline_history` 的 `sf` 恒为 0,
故直接采用 hfq 端点。)

注意: 本模块仅服务于**评估路径**; 生产路径 (batch_predict/morning_decision/
execute_*/paper_trader) 的复权口径不在此改, 保持原样。
"""
from __future__ import annotations

import pandas as pd

# 统一基准: 后复权
ADJUST_STANDARD = "hfq"

# 迈蕊 adjust 参数映射
_ADJUST_MAP = {"hfq": "b", "qfq": "f", "raw": "n"}

# 统一 K线输出字段 (与 dsl_data_sdk_original.get_kline 的 dict 键对齐)
_KLINE_FIELDS = ["date", "open", "high", "low", "close", "volume", "amount", "prev_close", "adjust"]


def normalize_code_6(symbol: str) -> str:
    """把任意代码格式规整为 6 位数字代码 (迈蕊 K线端点要求的入参格式)。"""
    s = str(symbol).strip()
    # 去掉 sh/sz 前缀 与 .SH/.SZ 后缀
    s = s.lower().replace("sh", "").replace("sz", "").split(".")[0]
    return s


def get_kline_adjusted(code: str, start: str, end: str, adjust: str = ADJUST_STANDARD) -> pd.DataFrame:
    """
    统一复权价入口。返回与 `get_kline` 相同字段结构的 DataFrame, 且按日期升序。

    :param code: 股票代码 (任意格式: 600519 / sh600519 / 600519.SH / 000858)
    :param start: 开始日期 "YYYY-MM-DD"
    :param end:   结束日期 "YYYY-MM-DD"
    :param adjust: 复权口径: hfq(后复权,默认) / qfq(前复权,旧行为) / raw(不复权)
    :return: DataFrame[date, open, high, low, close, volume, amount, prev_close, adjust]
    """
    if adjust not in _ADJUST_MAP:
        raise ValueError(f"adjust 必须是 {list(_ADJUST_MAP)} 之一, got={adjust!r}")

    from config.mairui_api_config import get_kline_history

    mairui_adj = _ADJUST_MAP[adjust]
    code6 = normalize_code_6(code)
    # 迈蕊 K线端点对 st/et 不生效(返回全历史), 这里统一客户端过滤日期
    raw = get_kline_history(code6, period="d", adjust=mairui_adj)
    if not raw or not isinstance(raw, list) or len(raw) == 0:
        return pd.DataFrame(columns=_KLINE_FIELDS)

    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("t", ""))[:10]
        if len(t) < 10 or "-" not in t:
            continue
        if t < start or t > end:
            continue
        try:
            rows.append({
                "date": t,
                "open": float(item.get("o", 0)),
                "high": float(item.get("h", 0)),
                "low": float(item.get("l", 0)),
                "close": float(item.get("c", 0)),
                "volume": float(item.get("v", 0)),
                "amount": float(item.get("a", 0)),
                "prev_close": float(item.get("pc", 0)),
                "adjust": adjust,
            })
        except (ValueError, TypeError):
            continue

    df = pd.DataFrame(rows, columns=_KLINE_FIELDS)
    if len(df) == 0:
        return df
    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


# 受影响指标清单 (FX-2 口径对照): 评估脚本 build_features 里**本地**基于 close/high/low/
# open/volume/prev_close 计算的指标, 全部直接受 close 复权口径影响。
# 分类: ①价格水平类(绝对值) ②比率类(相对值) ③价差/动量类。
ADJUST_IMPACT_TABLE = {
    "价格水平类(绝对值, 直接随复权因子缩放)": [
        "close/open/high/low 本身", "prev_close", "amount(成交额)"
    ],
    "比率类(同口径下自抵消, 跨口径才失真的部分)": [
        "ma{w}_ratio(=close/MA)", "vol{w}d(=rolling_std/close)",
        "atr14", "hl_ratio", "close_pos", "amplitude",
    ],
    "动量/价差类(前复权负值下 pct_change 失真最严重)": [
        "mom_{w}d(=close.pct_change)", "vol_change", "open_gap",
        "rsi14/macd/bb_pos/bb_width", "drawdown_20/60", "price_52w_pos",
        "target_{h}d(=close.shift(-h)/close-1)",
    ],
}


def adjust_impact_summary() -> str:
    """产出 FX-2 口径差异的量化说明 (供审计报告引用)。"""
    return (
        "统一基准=后复权(hfq)。前复权(qfq)历史值会被未来除权改写且可能变负值:\n"
        "  实测 600519 @2001-08-27 close: qfq=-312.54 vs hfq=35.55 vs raw=35.55。\n"
        "  → 负 close 使 pct_change/ratio 类特征(pct_change(-a)/负值)产生符号翻转与量级爆炸,\n"
        "    drawdown=close/rolling_max-1 在负 base 下失真, target_{h}d 跨除权日被改写。\n"
        "  → hfq 历史价固定为正, 是时点一致的唯一可行基准(备选 raw+sf, 但迈蕊 sf 恒0)。"
    )


if __name__ == "__main__":
    # 快速自检: 对比三种口径的 close
    for adj in ("qfq", "hfq", "raw"):
        df = get_kline_adjusted("600519", "2024-01-02", "2024-09-12", adjust=adj)
        if len(df):
            print(f"[{adj}] n={len(df)} first_close={df['close'].iloc[0]:.2f} "
                  f"last_close={df['close'].iloc[-1]:.2f}")
        else:
            print(f"[{adj}] EMPTY")
