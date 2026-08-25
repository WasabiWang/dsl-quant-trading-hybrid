#!/usr/bin/env python3
"""
v4.6.9f P2-1: 股票池精度联动 — 基于校准精度动态调整 tier

分层逻辑 (与 master_pool.yaml 头部注释一致):
  alpha ≥ 60%  (高置信度交易标的)
  core  ≥ 55%  (正常交易标的)
  bench ≥ 50%  (观察标的)
  <50%  → 标记 degraded (降级观察, 不物理删除)

用法:
  python3 scripts/update_pool_by_accuracy.py [--dry-run]

输出:
  - 更新 master_pool.yaml 的 tier 字段
  - 打印升降级明细
"""
import os, sys, json, yaml
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

POOL_PATH = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
OBS_PATH = PROJECT_ROOT / "config" / "observation_pool.yaml"
CALIB_PATH = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"

# 分层阈值
TIER_THRESHOLDS = [("alpha", 0.60), ("core", 0.55), ("bench", 0.50)]

# v4.7.0 P1-2: 观察池回池规则
OBS_REENTRY_MIN_SAMPLES = 3   # 最少验证样本数
OBS_REENTRY_MIN_ACC = 0.50    # 方向精度阈值
OBS_REENTRY_LOOKBACK_DAYS = 30

# v4.6.9h P1: 行业配额 — 单行业上限25% (29只→≤7只)
# 电子行业当前9只(31%)超限, 池刷新时超额行业的最低精度标的中, degraded优先移出观察
MAX_SECTOR_RATIO = 0.25


def sector_quota_check(stocks: list) -> list:
    """行业配额检查: 单行业占比>25%时, 超额标的中degraded/低精度优先移出
    返回需要标记的symbol集合
    """
    from collections import Counter
    sec_count = Counter(s.get("sector", "?") for s in stocks)
    max_allowed = max(1, int(len(stocks) * MAX_SECTOR_RATIO))
    out = set()
    for sec, cnt in sec_count.items():
        if cnt <= max_allowed:
            continue
        # 该行业超额 → 移出低精度标的(degraded优先移出, 精度升序)
        # 排序key: degraded的排末尾(优先被移出), 非degraded精度高的排前面(优先保留)
        sec_stocks = [s for s in stocks if s.get("sector") == sec]
        sec_stocks.sort(key=lambda x: (1 if x.get("degraded") else 0, -x.get("accuracy", 0)))
        # 保留精度最高的 max_allowed 只, 其余移出
        for s in sec_stocks[max_allowed:]:
            out.add(s["symbol"])
    return out


def load_calibration_accuracy() -> dict:
    """从校准文件读取每只股票的最新精度"""
    if not CALIB_PATH.exists():
        print(f"❌ 校准文件不存在: {CALIB_PATH}")
        return {}
    with open(CALIB_PATH, "r", encoding="utf-8") as f:
        calib = json.load(f)
    stock_acc = calib.get("stock_accuracy", {})
    result = {}
    for sym, data in stock_acc.items():
        if isinstance(data, dict):
            acc = data.get("last_accuracy", 0) or 0
            # 默认精度0.5视为未训练, 不参与降级
            result[sym] = {"accuracy": acc, "name": data.get("name", sym)}
    return result


def _load_held_symbols() -> set:
    """v4.7.0 P1-1: 读取模拟盘持仓 — 持仓标的不可自动移出"""
    held = set()
    try:
        sp = PROJECT_ROOT / "data" / "simulation_portfolio.json"
        if sp.exists():
            with open(sp, encoding="utf-8") as f:
                sim = json.load(f)
            pos = sim.get("positions", {})
            if isinstance(pos, dict):
                held = {str(k) for k in pos.keys()}
            elif isinstance(pos, list):
                for x in pos:
                    if isinstance(x, dict):
                        held.add(str(x.get("symbol", x.get("code", ""))))
    except Exception:
        pass
    return held


def _auto_move_remove_candidates(stocks: list, held: set) -> list:
    """v4.7.0 P1-1: remove_candidate且不在持仓 → 物理移出到观察池
    修复: 旧逻辑只标记不执行, 标记标的一直占资源且污染信号
    返回移出的symbol列表; stocks被原地修改为主池剩余
    """
    moved, keep = [], []
    for s in stocks:
        if s.get("remove_candidate") and s.get("symbol") not in held:
            s["removed_at"] = datetime.now().strftime("%Y-%m-%d")
            s["remove_reason"] = "remove_candidate自动移出(v4.7.0 P1-1)"
            moved.append(s)
        else:
            keep.append(s)
    if not moved:
        return []
    # 合并写入观察池
    obs_data = {"purpose": "池外观察池 — 移出标的自此跟踪, 30天精度均值≥50%可回池bench",
                 "observation_pool": []}
    if OBS_PATH.exists():
        try:
            with open(OBS_PATH, encoding="utf-8") as f:
                _o = yaml.safe_load(f) or {}
            obs_data["observation_pool"] = _o.get("observation_pool", [])
        except Exception:
            pass
    existing = {str(x.get("symbol", "")) for x in obs_data["observation_pool"]}
    for s in moved:
        if s["symbol"] not in existing:
            obs_data["observation_pool"].append(s)
    obs_data["updated_at"] = datetime.now().isoformat()
    with open(OBS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(obs_data, f, allow_unicode=True, sort_keys=False)
    # 主池原地瘦身
    stocks[:] = keep
    return [s["symbol"] for s in moved]


def _observation_realized_accuracy(symbol: str, lookback_days: int = OBS_REENTRY_LOOKBACK_DAYS) -> dict:
    """v4.7.0 P1-2: 观察池标的预测方向兑现精度
    独立于check_realized_accuracy(其把hold信号无条件记correct, 污染观察数据)
    数据源: calibration.daily_records(predicted_return) + 麦蕊K线(actual)
    """
    try:
        with open(CALIB_PATH, encoding="utf-8") as f:
            cal = json.load(f)
    except Exception:
        return {"samples": 0, "correct": 0, "acc": 0.0}
    recs = []
    today = datetime.now().date()
    for dr in cal.get("daily_records", []):
        try:
            d = datetime.strptime(dr["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if (today - d).days > lookback_days:
            continue
        for s in dr.get("stocks", []):
            if s.get("symbol") != symbol:
                continue
            pr = s.get("predicted_return", 0) or 0
            if pr == 0:
                continue
            h = s.get("horizon", "1d") or "1d"
            try:
                hd = int(''.join(ch for ch in h if ch.isdigit())) or 1
            except Exception:
                hd = 1
            recs.append((d.isoformat(), float(pr), hd))
    if not recs:
        return {"samples": 0, "correct": 0, "acc": 0.0}
    try:
        from config.mairui_api_config import get_kline_history
        correct = verified = 0
        for dstr, pr, hd in recs[-15:]:
            try:
                kl = get_kline_history(symbol, period="d", adjust="f",
                                       start_date=dstr, limit=hd + 10)
                if not kl or len(kl) < 2:
                    continue
                base = float(kl[0].get("c", 0))
                idx = min(hd, len(kl) - 1)
                end = float(kl[idx].get("c", 0))
                if not base or not end:
                    continue
                actual = end / base - 1
                verified += 1
                if (pr > 0 and actual > 0) or (pr < 0 and actual < 0):
                    correct += 1
            except Exception:
                continue
        acc = correct / verified if verified else 0.0
        return {"samples": verified, "correct": correct, "acc": acc}
    except Exception:
        return {"samples": 0, "correct": 0, "acc": 0.0}


def _check_observation_reentry(stocks: list, held: set) -> list:
    """v4.7.0 P1-2: 观察池回池 — 近30天≥3样本且方向精度≥50% → 回bench
    返回回池symbol列表
    """
    if not OBS_PATH.exists():
        return []
    try:
        with open(OBS_PATH, encoding="utf-8") as f:
            obs_data = yaml.safe_load(f) or {}
        obs_list = obs_data.get("observation_pool", [])
    except Exception:
        return []
    reentered, keep = [], []
    for s in obs_list:
        sym = str(s.get("symbol", ""))
        r = _observation_realized_accuracy(sym)
        if r["samples"] >= OBS_REENTRY_MIN_SAMPLES and r["acc"] >= OBS_REENTRY_MIN_ACC:
            if sym not in held and not any(x.get("symbol") == sym for x in stocks):
                entry = {"symbol": sym, "name": s.get("name", sym),
                         "tier": "bench", "sector": s.get("sector", ""),
                         "score": 50, "accuracy": round(r["acc"], 4),
                         "min_confidence": 0.5, "degraded": False,
                         "remove_candidate": False,
                         "pool_updated_at": datetime.now().strftime("%Y-%m-%d"),
                         "reentry_note": f"观察回池: {r['samples']}样本 方向精度{r['acc']:.1%}≥{OBS_REENTRY_MIN_ACC:.0%}"}
                stocks.append(entry)
                reentered.append(sym)
                print(f"  🔁 {sym} {s.get('name', '')}: 观察回池 (样本{r['samples']} 精度{r['acc']:.1%})")
            continue
        keep.append(s)
        if r["samples"] > 0:
            print(f"  🔍 {sym} {s.get('name', '')}: 观察中 (样本{r['samples']} 精度{r['acc']:.1%}, 需≥{OBS_REENTRY_MIN_SAMPLES}样本&{OBS_REENTRY_MIN_ACC:.0%})")
    obs_data["observation_pool"] = keep
    obs_data["updated_at"] = datetime.now().isoformat()
    with open(OBS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(obs_data, f, allow_unicode=True, sort_keys=False)
    return reentered


def tier_for_accuracy(acc: float) -> str:
    """按精度确定目标tier"""
    for tier, threshold in TIER_THRESHOLDS:
        if acc >= threshold:
            return tier
    return "bench"  # <50% 保留bench但标记degraded


def main(dry_run: bool = False):
    if not POOL_PATH.exists():
        print(f"❌ 股票池不存在: {POOL_PATH}")
        return 1

    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = yaml.safe_load(f)

    acc_map = load_calibration_accuracy()
    if not acc_map:
        print("❌ 无校准数据, 跳过")
        return 1

    stocks = pool.get("master_pool", [])
    changes = []
    for s in stocks:
        sym = s.get("symbol", "")
        info = acc_map.get(sym)
        if not info:
            continue
        acc = info["accuracy"]
        old_tier = s.get("tier", "bench")
        new_tier = tier_for_accuracy(acc)

        # <50% → 标记 degraded; <48% 且已 degraded 过的 → remove_candidate (池外观察)
        degraded = acc < 0.50
        old_degraded = s.get("degraded", False)
        # v4.6.9h P2: bench瘦身 — 精度<48%且已degraded ≥1轮 → 标记remove_candidate
        # 说明: 该标的历史精度持续低位, 保留在池内只占资源; 移出池但保留观察
        remove_candidate = degraded and acc < 0.48 and (old_degraded or s.get("pool_updated_at"))
        old_remove = s.get("remove_candidate", False)

        if new_tier != old_tier or degraded != old_degraded or remove_candidate != old_remove:
            changes.append({
                "symbol": sym, "name": info["name"],
                "old_tier": old_tier, "new_tier": new_tier,
                "accuracy": round(acc, 4),
                "degraded": degraded,
                "remove_candidate": remove_candidate,
            })
            if not dry_run:
                s["tier"] = new_tier
                s["degraded"] = degraded
                s["remove_candidate"] = remove_candidate
                s["accuracy"] = round(acc, 4)
                s["pool_updated_at"] = datetime.now().strftime("%Y-%m-%d")

    if dry_run:
        print(f"🔍 [DRY-RUN] 检测到 {len(changes)} 处变化:")
    else:
        # v4.6.9h P1: 行业配额检查 — 电子>25%时超额标的中degraded优先标记
        try:
            _quota_out = sector_quota_check(stocks)
            if _quota_out:
                _qnames = ", ".join(
                    f'{s["symbol"]}({s.get("name","")})' for s in stocks if s["symbol"] in _quota_out)
                print(f"🏷️ 行业超配额({MAX_SECTOR_RATIO:.0%}): {len(_quota_out)}只标记观察 {_qnames}")
                for s in stocks:
                    if s["symbol"] in _quota_out:
                        s["out_of_quota"] = True
        except Exception as _qe:
            print(f"⚠️ 行业配额检查失败: {_qe}")
        # v4.7.0 P1-1: remove_candidate自动移出(持仓保护)
        _held = _load_held_symbols()
        _moved = _auto_move_remove_candidates(stocks, _held)
        if _moved:
            print(f"🚚 自动移出观察池: {len(_moved)}只 → {_moved}")
        # v4.7.0 P1-2: 观察池回池检查
        _reentered = _check_observation_reentry(stocks, _held)
        if _reentered:
            print(f"🔁 观察回池: {len(_reentered)}只 → {_reentered}")
        # 备份 + 写回
        backup = POOL_PATH.with_suffix(".yaml.bak.poolacc")
        import shutil
        shutil.copy2(POOL_PATH, backup)
        with open(POOL_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(pool, f, allow_unicode=True, sort_keys=False)
        print(f"✅ 已更新 {len(changes)} 处 (备份: {backup.name})")

    for c in changes:
        arrow = "⬆️" if c["new_tier"] != c["old_tier"] and c["new_tier"] in ("alpha", "core") else ("⬇️" if c["new_tier"] == "bench" and c["old_tier"] in ("alpha", "core") else "➡️")
        deg = " [⚠️精度<50%]" if c["degraded"] else ""
        print(f"  {arrow} {c['symbol']} {c['name']}: {c['old_tier']}→{c['new_tier']} (acc={c['accuracy']:.1%}){deg}")

    # 汇总
    if not dry_run:
        tiers = {}
        for s in stocks:
            t = s.get("tier", "?")
            tiers[t] = tiers.get(t, 0) + 1
        print(f"\n📊 池分布: {tiers}")
        degraded_count = sum(1 for s in stocks if s.get("degraded"))
        print(f"⚠️ degraded标的: {degraded_count}只")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="股票池精度联动")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写回")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
