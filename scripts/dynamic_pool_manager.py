#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSL v4.5.1 动态股票池管理器
- 每周末用麦蕊API重筛PE/ROE/市值/换手率
- 黑名单机制（停牌/ST/爆雷/流动性不足）
- 自动写入 stock_pool.yaml + master_stock_pool.yaml
- 支持手动触发和 Cron 定时触发
"""

import sys, os, yaml, json, time, argparse
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.mairui_api_config import (
    get_stock_real, get_financial_indicators, get_top_flow_holders,
    get_holder_count, get_stock_concepts
)

CONFIG_DIR = PROJECT_ROOT / "config"
CACHE_DIR = PROJECT_ROOT / "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# ==================== 配置 ====================
# 筛选阈值
THRESHOLDS = {
    "min_pe": 3,         # PE>=3 排除极端亏损
    "max_pe": 200,       # PE>200 估值过高（科技成长股PE天然偏高）
    "min_pb": 0.2,       # PB 过低可能是银行/破净
    "max_pb": 20,        # PB 过高估值泡沫
    "min_market_cap": 5e7,   # 麦蕊API返回市值偏小~100倍（茅台17246亿→172亿），5e7≈实际50亿
    "min_turnover_rate": 0.2,  # 蓝筹股换手率可低至0.1%~0.3%，从0.3%降至0.2%
    "max_holder_decline": -30,  # 股东户数月环比增幅不超过30%
}

# 固定黑名单（可手动维护）
MANUAL_BLACKLIST = set()  # 如 {"600000", "601398"}

# 概念加分（政策利好板块）
CONCEPT_BONUS = {
    "AI芯片": 5, "算力": 5, "国产替代": 4, "新能源车": 4,
    "光伏": 3, "储能": 3, "机器人": 4, "半导体": 4,
    "军工": 3, "创新药": 3, "5G": 3, "人工智能": 5,
    "自动驾驶": 3, "固态电池": 4, "低空经济": 4,
    "电力改革": 2, "消费": 2, "银行": 1, "地产": -3,
}

# 池结构定义
POOL_STRUCTURE = {
    "core":        {"desc": "隐形冠军/成长龙头", "allocation": 0.25, "max_single": 0.20, "min_stocks": 5, "max_stocks": 8},
    "cyclical":    {"desc": "周期反转/困境修复", "allocation": 0.20, "max_single": 0.15, "min_stocks": 5, "max_stocks": 8},
    "growth":      {"desc": "高成长加速",         "allocation": 0.15, "max_single": 0.15, "min_stocks": 5, "max_stocks": 7},
    "bluechip":    {"desc": "蓝筹防御",           "allocation": 0.15, "max_single": 0.15, "min_stocks": 5, "max_stocks": 8},
    "flex":        {"desc": "灵活机动",           "allocation": 0.10, "max_single": 0.10, "min_stocks": 3, "max_stocks": 5},
}

# 因子权重（每层不同）
FACTOR_WEIGHTS = {
    "core":      {"valuation": 0.30, "trend": 0.20, "fundamental": 0.25, "momentum": 0.10, "volume": 0.05, "sentiment": 0.10},
    "cyclical":  {"momentum": 0.30, "volume": 0.20, "sentiment": 0.15, "trend": 0.15, "valuation": 0.10, "fundamental": 0.10},
    "growth":    {"momentum": 0.35, "volume": 0.25, "sentiment": 0.20, "trend": 0.10, "valuation": 0.05, "fundamental": 0.05},
    "bluechip":  {"valuation": 0.35, "fundamental": 0.25, "momentum": 0.15, "trend": 0.10, "volume": 0.05, "sentiment": 0.10},
    "flex":      {"momentum": 0.30, "sentiment": 0.25, "volume": 0.20, "trend": 0.15, "valuation": 0.05, "fundamental": 0.05},
}

# 旧池保留标的（确保连续性，避免模型白训练）
LEGACY_KEEP = {
    # 保留全部已有标的至少1个季度，禁止踢出已训练好的标的
    # 2026-05-09更新: 对应42只预测池，废弃空池cyclical
    "core": ["000725","002156","002179","002261","002281","002466","002475","002709","300014","300223","300390","300613","300857","600487","600584","600726","600809","603259","603893","603986"],
    "growth": ["000858","300418","300458","603599","688235","688608"],
    "bluechip": ["000063","002415","002460","002594","300059","300124","300750","600036","600276","600519","600887","601318","601899"],
    "flex": ["300274","301308","688525"],
}


def build_rating(stock_code: str, stock_name: str, sector: str, concepts: list) -> dict:
    """基于麦蕊API计算综合评分"""
    try:
        real = get_stock_real(stock_code)
        fin = get_financial_indicators(f"{stock_code}.SZ" if stock_code[0] in "03" else f"{stock_code}.SH", limit=1)
    except Exception as e:
        print(f"  ⚠️ 数据获取失败 {stock_code} {stock_name}: {e}")
        return None

    if not fin or not real:
        return None

    f = fin[0] if isinstance(fin, list) else fin

    # 估值
    pe = float(real.get("pe_ttm", 0) or 0)
    pb = float(real.get("pb_ratio", 0) or 0)
    mc = float(real.get("market_cap", 0) or 0)

    if pe < THRESHOLDS["min_pe"] or pe > THRESHOLDS["max_pe"]:
        return None
    if pb < THRESHOLDS["min_pb"] or pb > THRESHOLDS["max_pb"]:
        return None
    if mc < THRESHOLDS["min_market_cap"]:
        return None

    # 基本面
    roe = float(f.get("jqjzcsyl", 0) or 0)      # 净资产收益率(加权)
    gpm = float(f.get("xsmlv", 0) or 0)          # 销售毛利率
    npm = float(f.get("jlv", 0) or 0)            # 净利率
    ni_growth = float(f.get("jlrzz", 0) or 0)    # 净利润同比增长率
    rev_growth = float(f.get("zyyrsrzz", 0) or 0) # 主营收入同比增长

    # 流动性
    turnover = float(real.get("turnover_rate", 0) or 0)
    if turnover < THRESHOLDS["min_turnover_rate"]:
        return None

    # 概念加分
    concept_score = sum(CONCEPT_BONUS.get(c, 0) for c in concepts)

    # 综合评分 (0-100)
    score = 50  # 基准分

    # 成长性 (35%)
    if ni_growth > 30: score += 12
    elif ni_growth > 15: score += 8
    elif ni_growth > 5: score += 4
    if rev_growth > 20: score += 8
    elif rev_growth > 10: score += 4
    if roe > 15: score += 10
    elif roe > 8: score += 5
    if gpm > 40: score += 5

    # 估值合理性 (30%)
    if pe < 20: score += 15
    elif pe < 30: score += 10
    elif pe < 40: score += 5
    if pb < 3: score += 10
    elif pb < 5: score += 5
    # 市值/PB安全边际 (mc/pb ratio)
    if mc > 0 and pb > 0 and (mc / pb) < 2e10: score += 5

    # 流动性 (15%)
    if turnover > 3: score += 8
    elif turnover > 1.5: score += 4
    # NOTE: 麦蕊API返回mc偏小~100倍，阈值相应调低
    if mc > 1e9: score += 7   # ≈实际市值>1000亿
    elif mc > 5e8: score += 3  # ≈实际市值>500亿

    # 概念/政策 (20%)
    score += concept_score

    return {
        "code": stock_code,
        "name": stock_name,
        "sector": sector,
        "concepts": concepts,
        "score": score,
        "pe": round(pe, 2),
        "pb": round(pb, 2),
        "mc": mc,
        "roe": round(roe, 2),
        "gpm": round(gpm, 2),
        "ni_growth": round(ni_growth, 2),
        "rev_growth": round(rev_growth, 2),
        "turnover": round(turnover, 2),
    }


def classify_stocks(rated: list) -> dict:
    """按评分和行业自动分层"""
    pools = {k: [] for k in POOL_STRUCTURE}

    for r in sorted(rated, key=lambda x: x["score"], reverse=True):
        # bluechip: 市值>1000亿(API值~1%, 故阈值1e9≈实际1000亿) + 蓝筹行业
        if r["mc"] > 1e9 and r["sector"] in ["银行","保险","白酒","乳业","电力","有色金属"]:
            pools["bluechip"].append(r)
            continue
        # cyclical: 有色/化工/锂电
        if r["sector"] in ["有色金属","基础化工","电力设备"] and "锂" in str(r.get("concepts","")):
            pools["cyclical"].append(r)
            continue
        # growth: 高增速
        if r["ni_growth"] > 30:
            pools["growth"].append(r)
            continue
        # core: 其余高分->核心
        pools["core"].append(r)

    # flex: 低分高流动性（作为灵活池补充）
    for r in rated:
        if all(r["code"] not in [s["code"] for s in v] for v in pools.values()):
            if r["turnover"] > 2:
                pools["flex"].append(r)

    # 截断到 max_stocks
    for k, v in pools.items():
        pools[k] = v[:POOL_STRUCTURE[k]["max_stocks"]]

    # 兜底：如果某层不达标，从高分往下补
    for k, cfg in POOL_STRUCTURE.items():
        if len(pools[k]) < cfg["min_stocks"]:
            existing_codes = set()
            for pv in pools.values():
                for item in pv:
                    existing_codes.add(item["code"])
            remaining = [r for r in rated if r["code"] not in existing_codes]
            pools[k].extend(remaining[:cfg["min_stocks"] - len(pools[k])])

    return pools


def generate_stock_pool_yaml(pools: dict, output_path: Path):
    """生成 stock_pool.yaml"""
    header = f"""# DSL v4.5.1 动态股票池配置
# 生成日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}
# 生成方式: 麦蕊API自动重筛
# 下次更新: 每周末自动执行
#
# 仓位分配:
#   核心池 {POOL_STRUCTURE['core']['allocation']*100:.0f}%
#   周期池 {POOL_STRUCTURE['cyclical']['allocation']*100:.0f}%
#   成长池 {POOL_STRUCTURE['growth']['allocation']*100:.0f}%
#   蓝筹池 {POOL_STRUCTURE['bluechip']['allocation']*100:.0f}%
#   灵活池 {POOL_STRUCTURE['flex']['allocation']*100:.0f}%

tiers:
"""
    doc = {"tiers": {}}

    for tier_key, stocks in pools.items():
        cfg = POOL_STRUCTURE[tier_key]
        entry = {
            "description": cfg["desc"],
            "allocation": cfg["allocation"],
            "max_single": cfg["max_single"],
            "holding_days": "21-42" if tier_key in ["core","bluechip"] else "14-28",
            "factor_weights": FACTOR_WEIGHTS[tier_key],
            "stocks": []
        }
        for s in stocks:
            entry["stocks"].append({
                "code": s["code"],
                "name": s["name"],
                "sector": s.get("sector", ""),
                "score": s.get("score", 0),
            })
        doc["tiers"][tier_key] = entry

    doc["risk_notes"] = [
        "核心池科技/半导体集中度需关注，单行业回撤时整体池受影响",
        "周期池锂电材料集中，需关注锂价周期",
        "建议每周六凌晨用麦蕊API重新筛选PE/ROE",
        f"黑名单(size={len(MANUAL_BLACKLIST)}): {', '.join(sorted(MANUAL_BLACKLIST))}",
    ]

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # 写入header（yaml.dump 不支持头部注释）
    content = output_path.read_text()
    output_path.write_text(header + "\n" + content)

    print(f"✅ stock_pool.yaml 已写入: {output_path}")


def generate_master_pool_yaml(pools: dict, output_path: Path):
    """生成 master_stock_pool.yaml（训练/预测/盘前统一使用）
    v4.5.3: 去重归一化 — 同一标的只保留最高优先级 tier
    """
    now = datetime.now().strftime('%Y-%m-%d')
    header = f"""# DSL量化交易系统 — 统一主股票池
# v4.5.3: 麦蕊API动态筛选 + 黑名单 + 旧池保护 + 去重归一化
# 生成日期: {now}
# 下次更新: 每周末
# tier优先级: bluechip > core > growth > cyclical > flex

master_pool:
"""
    # ── 去重归一化: 同标的保留最高优先级的 tier ──
    TIER_PRIORITY = {"bluechip": 1, "core": 2, "growth": 3, "cyclical": 4, "flex": 5}
    seen = {}  # {code: (entry, priority)}
    dup_count = 0
    
    for tier_key, stocks in pools.items():
        tier_pri = TIER_PRIORITY.get(tier_key, 99)
        for s in stocks:
            code = s["code"]
            if code in seen:
                prev_pri = seen[code][1]
                # 新tier优先级更高则替换
                if tier_pri < prev_pri:
                    seen[code] = (s, tier_pri)
                dup_count += 1
            else:
                seen[code] = (s, tier_pri)
    
    entries = []
    for code, (s, pri) in sorted(seen.items()):
        tier_key = next(k for k,v in TIER_PRIORITY.items() if v == pri)
        concepts_str = ",".join(str(c) for c in (s.get("concepts", []) or []))
        entries.append(f'  - {{symbol: "{code}", name: "{s["name"]}", tier: "{tier_key}", sector: "{s.get("sector","")}", concept: "{concepts_str}", score: {s.get("score",0)}}}')

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        for e in entries:
            f.write(e + "\n")
        f.write(f"\n# 总计: {len(seen)} 只标的 (去重前{len(seen)+dup_count}条, 去重{dup_count}条)\n")
        f.write(f"# 黑名单: {', '.join(sorted(MANUAL_BLACKLIST))}\n")

    print(f"✅ master_stock_pool.yaml 已写入: {output_path} ({len(seen)}只唯一标的, 去重{dup_count}条)")


def main():
    parser = argparse.ArgumentParser(description="DSL动态股票池管理器")
    parser.add_argument("--dry-run", action="store_true", help="仅分析不写入")
    parser.add_argument("--blacklist", nargs="*", default=[], help="添加黑名单标的")
    parser.add_argument("--force", action="store_true", help="强制覆盖旧池保护")
    args = parser.parse_args()

    # 更新黑名单
    if args.blacklist:
        MANUAL_BLACKLIST.update(args.blacklist)
        print(f"🛑 黑名单已更新: {args.blacklist}")

    print(f"{'='*70}")
    print(f"DSL v4.5.1 动态股票池管理器")
    print(f"{'='*70}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"数据源: 麦蕊API (Licence configured)")
    print(f"黑名单规模: {len(MANUAL_BLACKLIST)}")
    print()

    # Step 1: 收集候选标的
    # 从现有池 + 扩展候选池 取合集
    current_pool_path = CONFIG_DIR / "stock_pool.yaml"
    if current_pool_path.exists():
        with open(current_pool_path) as f:
            old_pool = yaml.safe_load(f)
        current_codes = set()
        for tier in old_pool["tiers"].values():
            for s in tier["stocks"]:
                current_codes.add(s["code"])
        print(f"现有池标的: {len(current_codes)} 只")
    else:
        current_codes = set()
        print("无现有池，从头构建")

    # 候选 = 现有池 + 沪深300核心标的
    hsj300_codes = [
        "600519","300750","601318","600036","600276","600887","601012","002415",
        "601899","000063","000858","600900","600809","002594","300059","688981",
        "300124","002230","000725","002475","601688","600030","603259","300274",
    ]
    all_candidate_codes = set(str(c) for c in current_codes) | set(hsj300_codes)
    all_candidate_codes -= set(str(c) for c in MANUAL_BLACKLIST)

    print(f"候选标的(去重+去黑名单): {len(all_candidate_codes)} 只")
    print()

    # Step 2: 逐只评分
    rated = []
    failed = []
    for i, code in enumerate(sorted(all_candidate_codes)):
        print(f"[{i+1}/{len(all_candidate_codes)}] 评分 {code}...", end=" ")
        try:
            # 获取概念
            concepts = []
            try:
                concept_data = get_stock_concepts(code)
                if isinstance(concept_data, list):
                    concepts = [c.get("name","") for c in concept_data[:10]]
            except:
                pass

            # 用代码查名称（从旧池映射）
            name_map = {}
            if current_pool_path.exists():
                for tier in old_pool["tiers"].values():
                    for s in tier["stocks"]:
                        name_map[s["code"]] = s.get("name","")
            name = name_map.get(code, code)

            result = build_rating(code, name, "", concepts)
            if result:
                rated.append(result)
                print(f"✅ {result['score']}分 PE={result['pe']} ROE={result['roe']}%")
            else:
                failed.append(code)
                print("❌ 不达标")
        except Exception as e:
            failed.append(code)
            print(f"❌ {e}")
        time.sleep(0.1)  # 限流

    print()
    print(f"达标: {len(rated)} 只 | 不达标: {len(failed)} 只")
    print()

    # Step 3: 分类
    pools = classify_stocks(rated)

    # Step 4: 旧池保护（保留已训练标的不被踢出）— 跨层去重
    if not args.force:
        # 先收集所有已有的标的代码
        all_existing_codes = set()
        for tier_stocks in pools.values():
            for s in tier_stocks:
                all_existing_codes.add(s["code"])
        
        for tier_key, protected in LEGACY_KEEP.items():
            if tier_key not in pools:
                pools[tier_key] = []
            existing = {s["code"] for s in pools[tier_key]}
            added_legacy = 0
            skipped_other_tier = 0
            force_added = 0
            for code in protected:
                if code in existing:
                    continue  # 已在当前层
                if code in all_existing_codes:
                    skipped_other_tier += 1
                    continue  # 已在其他层，不重复添加
                # 从排行榜找该标的
                p_stock = next((r for r in rated if r["code"] == code), None)
                if p_stock:
                    pools[tier_key].append(p_stock)
                    all_existing_codes.add(code)
                    added_legacy += 1
                else:
                    # 强力保护: API评分不通过的LEGACY标的也强制保留
                    name_map = {}
                    if current_pool_path.exists():
                        for tier in old_pool["tiers"].values():
                            for s in tier["stocks"]:
                                name_map[s["code"]] = s.get("name", "")
                    legacy_entry = {
                        "code": code, "name": name_map.get(code, code),
                        "sector": "", "concepts": [], "score": 45,
                        "pe": 0, "pb": 0, "mc": 0, "roe": 0,
                        "gpm": 0, "ni_growth": 0, "rev_growth": 0, "turnover": 0,
                    }
                    pools[tier_key].append(legacy_entry)
                    all_existing_codes.add(code)
                    force_added += 1
            if added_legacy or skipped_other_tier or force_added:
                print(f"  {tier_key}旧池保护: +{added_legacy}新评分, +{force_added}强制恢复, {skipped_other_tier}已在其他层跳过")

    total = sum(len(v) for v in pools.values())
    print("池构成:")
    for k, v in pools.items():
        names = ", ".join(s["name"] for s in v[:3])
        print(f"  {k}: {len(v)}只 ({names}...)")

    print(f"\n总计: {total} 只标的")
    print()

    if args.dry_run:
        print("🔍 DRY-RUN 模式，不写入文件")
        return 0

    # Step 5: 写入
    generate_stock_pool_yaml(pools, CONFIG_DIR / "stock_pool.yaml")
    generate_master_pool_yaml(pools, CONFIG_DIR / "master_stock_pool.yaml")

    # 备份旧配置
    backup_dir = CONFIG_DIR / "archive"
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    for fn in ["stock_pool.yaml", "master_stock_pool.yaml"]:
        src = CONFIG_DIR / fn
        if src.exists():
            import shutil
            shutil.copy(src, backup_dir / f"{fn}.{ts}")

    # 写入元数据
    meta = {
        "last_update": datetime.now().isoformat(),
        "total_rated": len(rated),
        "total_failed": len(failed),
        "total_pool": total,
        "blacklist": sorted(list(MANUAL_BLACKLIST)),
        "pool_sizes": {k: len(v) for k, v in pools.items()},
    }
    with open(CACHE_DIR / "pool_update_meta.json", "w") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"✅ 备份已保存: {backup_dir}")
    print(f"✅ 元数据已保存: {CACHE_DIR}/pool_update_meta.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
