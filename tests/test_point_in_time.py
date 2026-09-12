#!/usr/bin/env python3
"""DSL edge-verification P0 — 点内时点 (point-in-time / no-lookahead) 一致性测试套件.

判据 (universal, 不依赖具体函数签名):
  对同一序列 + 同一检验点 T, 用「完整数据」和「截断至 T 的数据」分别算特征,
  断言 T 行的值一致 (np.isclose(..., equal_nan=True))。
  不一致 → 该特征用到了未来数据 (前视 / lookahead)。

覆盖:
  1. rolling(250, min_periods=60).rank(pct=True) 全量 vs 截断, T 行一致
  2. 反例守护: 旧的整段 rank(pct=True) 必须失败本检验 (证明测试能抓前视)
  3. 边界: 样本不足 min_periods → NaN 且不报错
  4. _PCT_WINDOW / _PCT_MIN 在两个源文件中存在且为 250 / 60
  5. 静态守护: 两个源文件不存在裸的 feats[...].rank(pct=True);
     排除 batch_predict.py 中合法的截面 rank (_values_series.rank(pct=True))
  6. 静态扫描器自检 (证明守护非空转)

独立运行:
  cd <项目根> && PYTHONPATH=<项目根> .venv/bin/python3 tests/test_point_in_time.py
退出码: 全部通过 0, 失败非 0。同时兼容 pytest 收集 (用例均无参)。
"""

from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["scripts/train_predictor_enhanced.py", "scripts/batch_predict.py"]

EXPECTED_PCT_WINDOW = 250
EXPECTED_PCT_MIN = 60

RTOL = 1e-12
ATOL = 1e-12


# ----------------------------------------------------------------- helpers
def _isclose(a, b) -> bool:
    """点内时点比较: NaN 对 NaN 视为一致。"""
    return bool(np.isclose(a, b, equal_nan=True, rtol=RTOL, atol=ATOL))


def _pit_row(feature_fn, series: pd.Series, t: int):
    """(完整数据在 T 行的值, 截断至 T 的数据在 T 行的值)。"""
    full = feature_fn(series)
    truncated = feature_fn(series.iloc[: t + 1])
    return float(full.iloc[t]), float(truncated.iloc[t])


def _sample_series(n: int = 400, seed: int = 11, kind: str = "random") -> pd.Series:
    rng = np.random.default_rng(seed)
    if kind == "random":
        steps = rng.normal(0.0, 1.0, n)
    elif kind == "trend":
        steps = np.linspace(-1.0, 1.0, n) + rng.normal(0.0, 0.2, n)
    elif kind == "flat":
        steps = np.zeros(n)
    else:  # pragma: no cover - 防呆
        raise ValueError(kind)
    return pd.Series(np.cumsum(steps), index=pd.RangeIndex(n, name="t"), dtype="float64")


def _rolling_rank(x: pd.Series) -> pd.Series:
    """与两个源文件一致的修复后写法 (点内时点)。"""
    return x.rolling(EXPECTED_PCT_WINDOW, min_periods=EXPECTED_PCT_MIN).rank(pct=True)


def _scan_rank_calls(source: str):
    """AST 扫描 `.rank(pct=True)` 调用, 分类返回 (rolling, bare_feats, other)。

    - rolling:     接收者是 `.rolling(...)` → 点内时点, 安全
    - bare_feats:  接收者表达式含 `feats` 且无 rolling → 前视, 需报错
    - other:       其余 (如截面 rank `_values_series.rank(pct=True)`) → 合法
    """
    tree = ast.parse(source)
    rolling, bare_feats, other = [], [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "rank"):
            continue
        if not any(
            kw.arg == "pct"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        ):
            continue
        receiver = func.value
        snippet = (ast.get_source_segment(source, receiver) or "").strip()
        if (
            isinstance(receiver, ast.Call)
            and isinstance(receiver.func, ast.Attribute)
            and receiver.func.attr == "rolling"
        ):
            rolling.append((node.lineno, f"{snippet}.rank(pct=True)"))
        elif "feats" in snippet:
            bare_feats.append((node.lineno, f"{snippet}.rank(pct=True)"))
        else:
            other.append((node.lineno, f"{snippet}.rank(pct=True)"))
    return rolling, bare_feats, other


def _read_source(rel: str) -> str:
    path = PROJECT_ROOT / rel
    assert path.exists(), f"缺少被测源文件: {rel}"
    return path.read_text(encoding="utf-8")


# ------------------------------------------------------------------- 用例
def test_rolling_rank_is_point_in_time():
    """1. 修复后的滚动分位: 全量 vs 截断, 各检验点 T 行完全一致。"""
    cut_points = (120, 250, 300, 399)
    checks = 0
    for kind in ("random", "trend", "flat"):
        series = _sample_series(400, seed=11, kind=kind)
        for t in cut_points:
            a, b = _pit_row(_rolling_rank, series, t)
            assert _isclose(a, b), (
                f"[{kind}] T={t}: 全量与截断数据分位不一致 "
                f"full={a!r} truncated={b!r} → 该特征用到未来数据"
            )
            checks += 1
        # 更强形式: 前缀 0..300 每一行都必须一致 (不只末端一行)
        full = _rolling_rank(series)
        trunc = _rolling_rank(series.iloc[:301])
        assert np.isclose(
            full.iloc[:301].to_numpy(), trunc.iloc[:301].to_numpy(), equal_nan=True, rtol=RTOL, atol=ATOL
        ).all(), f"[{kind}] 前缀 0..300 存在不一致行 → 前视"
    return f"3 序列 × {len(cut_points)} 截断点 = {checks} 次一致 + 前缀 0..300 逐行一致"


def test_naive_full_series_rank_is_caught_as_lookahead():
    """2. 反例守护: 旧的整段 rank(pct=True) 必须被判为前视 (测试不能是空转)。"""
    series = _sample_series(400, seed=23, kind="random")
    mid = 200
    a, b = _pit_row(lambda x: x.rank(pct=True), series, mid)
    assert not _isclose(a, b), (
        f"守护失效: 整段 rank(pct=True) 在 T={mid} 竟然通过了点内时点检验 "
        f"(full={a!r}, truncated={b!r}); 说明该套件抓不到前视"
    )
    # 对照: 末端行之后没有数据, 整段写法此时恰好等价 → 证明本检验不是恒假
    last = len(series) - 1
    c, d = _pit_row(lambda x: x.rank(pct=True), series, last)
    assert _isclose(c, d), f"对照失败: 末端 T={last} 整段 rank 应等价 (full={c!r}, truncated={d!r})"
    return (
        f"整段 rank 在 T={mid} 被判定前视 (full={a:.6f} != truncated={b:.6f}); "
        f"末端 T={last} 一致 (对照, 检验非恒假)"
    )


def test_insufficient_samples_yields_nan_without_error():
    """3. 边界: T 处样本不足 min_periods → NaN, 且不抛异常。"""
    short = _sample_series(30, seed=5)
    try:
        out = _rolling_rank(short)
    except Exception as exc:  # noqa: BLE001 - 必须不报错
        raise AssertionError(f"样本不足时滚动分位抛异常: {exc!r}") from exc
    assert bool(out.isna().all()), "样本不足 (30 < min_periods) 时应全为 NaN"

    series = _sample_series(100, seed=6)
    out = _rolling_rank(series)
    below = EXPECTED_PCT_MIN - 2          # 59 个样本 → 不足
    exact = EXPECTED_PCT_MIN - 1          # 60 个样本 → 恰好够
    assert bool(np.isnan(out.iloc[below])), f"T={below} (样本 {EXPECTED_PCT_MIN - 1} < min_periods) 应为 NaN"
    assert not bool(np.isnan(out.iloc[exact])), f"T={exact} (样本 == min_periods) 应产出有效值"

    a, b = _pit_row(_rolling_rank, series, below)
    assert _isclose(a, b) and np.isnan(a), f"不足样本时全量/截断应同为 NaN (full={a!r}, truncated={b!r})"
    return f"n=30 全 NaN 无异常; T={below} NaN / T={exact} 有效; 截断口径同为 NaN"


def test_pct_window_constants_defined_in_sources():
    """4. 两个源文件模块级 _PCT_WINDOW=250 / _PCT_MIN=60。"""
    summaries = []
    for rel in SOURCES:
        tree = ast.parse(_read_source(rel), filename=rel)
        consts = {}
        for node in tree.body:  # 仅模块级赋值
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("_PCT_WINDOW", "_PCT_MIN"):
                        consts[target.id] = (
                            node.value.value if isinstance(node.value, ast.Constant) else None
                        )
        assert consts.get("_PCT_WINDOW") == EXPECTED_PCT_WINDOW, (
            f"{rel}: _PCT_WINDOW 应为 {EXPECTED_PCT_WINDOW}, 实际 {consts.get('_PCT_WINDOW')!r}"
        )
        assert consts.get("_PCT_MIN") == EXPECTED_PCT_MIN, (
            f"{rel}: _PCT_MIN 应为 {EXPECTED_PCT_MIN}, 实际 {consts.get('_PCT_MIN')!r}"
        )
        summaries.append(f"{rel}: _PCT_WINDOW={consts['_PCT_WINDOW']}, _PCT_MIN={consts['_PCT_MIN']}")
    return "; ".join(summaries)


def test_no_bare_feats_rank_in_sources():
    """5. 静态守护: 无漏改的裸 feats[...].rank(pct=True); 截面 rank 不得误报。"""
    summaries = []
    for rel in SOURCES:
        rolling, bare_feats, other = _scan_rank_calls(_read_source(rel))
        assert not bare_feats, (
            f"{rel}: 发现裸 feats[...].rank(pct=True) (前视未修) → "
            f"行号 {[ln for ln, _ in bare_feats]}: {[s for _, s in bare_feats]}"
        )
        assert len(rolling) >= 3, (
            f"{rel}: 期望至少 3 处点内时点滚动分位, 实际 {len(rolling)} 处 "
            f"(可能被改回整段 rank)"
        )
        summaries.append(f"{rel}: rolling={len(rolling)} 处, 截面/其他={len(other)} 处")

    # batch_predict.py 的截面 rank 是正确写法, 必须被识别为合法例外而非误报
    _, bare_feats, other = _scan_rank_calls(_read_source("scripts/batch_predict.py"))
    assert not bare_feats, f"截面 rank 被误报为前视: {bare_feats}"
    assert any("_values_series" in snippet for _, snippet in other), (
        "未识别出合法的截面 rank `_values_series.rank(pct=True)`; 排除规则可能失效"
    )
    return "; ".join(summaries) + f"; 截面例外已排除: {[s for _, s in other]}"


def test_static_guard_scanner_is_not_vacuous():
    """6. 扫描器自检: 能抓手写前视、放行滚动写法、放行截面写法。"""
    bad = 'feats["mom_5d_pct"] = feats["mom_5d"].rank(pct=True)\n'
    rolling, bare_feats, other = _scan_rank_calls(bad)
    assert len(bare_feats) == 1 and not rolling, f"漏报裸 feats rank: {(rolling, bare_feats, other)}"

    good = 'feats["mom_5d_pct"] = feats["mom_5d"].rolling(250, min_periods=60).rank(pct=True)\n'
    rolling, bare_feats, other = _scan_rank_calls(good)
    assert len(rolling) == 1 and not bare_feats, f"误报滚动 rank: {(rolling, bare_feats, other)}"

    cross_sectional = "_ranks_pct = _values_series.rank(pct=True)\n"
    rolling, bare_feats, other = _scan_rank_calls(cross_sectional)
    assert not bare_feats and len(other) == 1, f"截面 rank 未正确归类: {(rolling, bare_feats, other)}"
    return "裸 feats rank → 报错; rolling rank → 放行; _values_series 截面 rank → 放行"


# ------------------------------------------------------------------ runner
def _collect_tests():
    module = sys.modules[__name__]
    return [
        (name, getattr(module, name))
        for name in sorted(dir(module))
        if name.startswith("test_") and callable(getattr(module, name))
    ]


def main() -> int:
    tests = _collect_tests()
    print("Point-in-Time 一致性套件 — 前视 (lookahead) 检验")
    print(f"项目根: {PROJECT_ROOT}")
    print(f"被测源码: {', '.join(SOURCES)}")
    print("-" * 68)
    passed = failed = 0
    for name, fn in tests:
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - 汇总所有失败
            failed += 1
            print(f"❌ {name}")
            print(f"   {type(exc).__name__}: {exc}")
            tb = traceback.format_exc().rstrip().splitlines()
            for line in tb[-4:]:
                print(f"   {line.strip()}")
        else:
            passed += 1
            print(f"✅ {name}")
            if detail:
                print(f"     {detail}")
    print("-" * 68)
    print(f"{passed} 通过, {failed} 失败 (共 {len(tests)} 用例)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
