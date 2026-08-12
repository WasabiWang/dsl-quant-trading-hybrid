#!/usr/bin/env python3
"""DSL v4.5.1 三层股票池生成脚本"""
import json, akshare as ak, pandas as pd, numpy as np

with open("/tmp/dsl_index_constituents.json") as f:
    idx = json.load(f)

all_codes_raw = set(idx['csi300'] + idx['csi500'] + idx['csi1000'])
print(f"候选池: {len(all_codes_raw)} 只")

# 获取全A实时行情
print("获取行情...")
spot = ak.stock_zh_a_spot()
spot['code_num'] = spot['代码'].str.replace('^(sh|sz|bj)', '', regex=True)
spot = spot[spot['code_num'].isin(all_codes_raw)].copy()

for col in ['最新价', '涨跌幅', '成交量', '成交额']:
    spot[col] = pd.to_numeric(spot[col], errors='coerce')
spot = spot.dropna(subset=['最新价', '成交额'])
spot = spot[~spot['名称'].str.contains('ST|退', na=False)]
spot = spot[spot['最新价'] > 1]

csi300_set = set(idx['csi300'])
csi500_set = set(idx['csi500'])
csi1000_set = set(idx['csi1000'])
spot['in300'] = spot['code_num'].isin(csi300_set)
spot['in500'] = spot['code_num'].isin(csi500_set)
spot['in1000'] = spot['code_num'].isin(csi1000_set)

print(f"有效: {len(spot)} 只 | 成交额: {spot['成交额'].min()/1e4:.0f}万-{spot['成交额'].max()/1e8:.0f}亿")

# Core Pool: CSI500 ex-CSI300, volume > 50M, non-financial
core = spot[spot['in500'] & ~spot['in300']]
core = core[core['成交额'] > 5e7]
core = core[core['涨跌幅'] > -5]
core = core[~core['名称'].str.contains('银行|保险|证券|信托', na=False)]
core = core.sort_values('成交额', ascending=False)

# Cycle Pool: keyword match in name
cycle_kw = ['化工', '化学', '材料', '养殖', '农牧', '饲料', '半导体', '芯片', '硅',
            '光伏', '锂', '钴', '稀土', '铜', '铝', '钢铁', '煤炭', '石油', '化纤',
            '氟', '磷', '钛', '农药', '化肥', '维生素', '纯碱', '玻璃', '水泥', '钼', '钨']
cycle = spot[(spot['成交额'] > 3e7) & (spot['最新价'] < 500)]
cycle_matched = cycle[cycle['名称'].str.contains('|'.join(cycle_kw), na=False)]
cycle_matched = cycle_matched.sort_values('成交额', ascending=False)

# Growth Pool: CSI1000 only, small momentum, vol > 50M
growth = spot[spot['in1000'] & ~spot['in300'] & ~spot['in500']]
growth = growth[growth['成交额'] > 5e7]
growth = growth[growth['涨跌幅'] > -3]
growth = growth[growth['最新价'] < 200]
growth = growth[~growth['名称'].str.contains('银行|保险|证券|信托|ST', na=False)]
growth = growth.sort_values(['涨跌幅', '成交额'], ascending=[False, False])

def to_list(df, label, n=10):
    result = []
    for _, r in df.head(n).iterrows():
        idx_label = '300' if r['in300'] else ('500' if r['in500'] else '1000')
        result.append({
            'code': str(r['code_num']).zfill(6),
            'name': r['名称'],
            'price': round(float(r['最新价']), 2),
            'pct': round(float(r['涨跌幅']), 2),
            'vol_yi': round(float(r['成交额']) / 1e8, 1),
            'index': idx_label
        })
    return result

core_list = to_list(core, 'core', 10)
cycle_list = to_list(cycle_matched, 'cycle', 8)
growth_list = to_list(growth, 'growth', 6)

result = {
    "version": "v4.5.1",
    "screened_at": "2026-04-26",
    "method": "CSI300+500+1000成分股 × 实时行情初筛",
    "tiers": {
        "core": {"description": "CSI500隐形冠军+消费估值修复", "weight": 0.60, "stocks": core_list},
        "cycle": {"description": "化工/养殖/半导体/周期反转", "weight": 0.25, "stocks": cycle_list},
        "growth": {"description": "CSI1000成长加速", "weight": 0.15, "stocks": growth_list}
    }
}

with open("/tmp/dsl_stock_pool_v4.5.1.json", "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

total = len(core_list) + len(cycle_list) + len(growth_list)
print(f"\n🎉 三层池共 {total} 只")

for tier_label, tier_name, stocks in [
    ("🥇", "核心池(CSI500隐形冠军)", core_list),
    ("🥈", "周期池", cycle_list),
    ("🥉", "成长池(CSI1000)", growth_list)
]:
    print(f"\n{'='*60}")
    print(f"{tier_label} {tier_name} ({len(stocks)}只)")
    print(f"{'='*60}")
    for s in stocks:
        print(f"  {s['code']} {s['name']:<8s} | {s['price']:>8.2f} | {s['pct']:>+6.2f}% | {s['vol_yi']:.1f}亿 | CSI{s['index']}")
