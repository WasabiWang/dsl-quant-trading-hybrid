"""
智能选股器 (Smart Stock Selector)

根据市场体制和配置，从股票池中筛选标的。
不再硬编码个股，改为从配置文件读取各体制对应的推荐标的池。
"""

import json
import os
from typing import List, Dict, Optional


CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "regime_stock_pools.json",
)

# 默认配置（当配置文件不存在时使用）
DEFAULT_POOLS = {
    "BULL_RUN": {
        "name": "牛市池（高Beta龙头）",
        "description": "牛市主升浪时推荐的高Beta龙头股",
        "stocks": [],
    },
    "BEAR_CRASH": {
        "name": "熊市池（高股息防守）",
        "description": "熊市防守期推荐的高股息、低波动标的",
        "stocks": [],
    },
    "SIDEWAYS": {
        "name": "震荡池（高波动题材）",
        "description": "震荡市中适合网格交易的高波动标的",
        "stocks": [],
    },
}


def _load_pools() -> Dict[str, Dict]:
    """从配置文件加载各体制对应的股票池"""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                pools = json.load(f)
            # 确保三个体制都有定义
            for regime in ["BULL_RUN", "BEAR_CRASH", "SIDEWAYS"]:
                if regime not in pools or not isinstance(pools[regime].get("stocks"), list):
                    pools[regime] = DEFAULT_POOLS[regime]
            return pools
        except Exception as e:
            print(f"⚠️ 加载股票池配置失败: {e}，使用默认空池")
    return dict(DEFAULT_POOLS)


class SmartStockSelector:
    """智能选股器，根据市场体制从配置文件中读取对应股票池"""

    def __init__(self, stock_pool: List[str]):
        """
        Args:
            stock_pool: 用户的A股关注池（代码列表，如 ['002594', '600036']）
        """
        self.stock_pool = stock_pool
        self.pools = _load_pools()

    def select(self, market_regime: str) -> List[str]:
        """
        根据市场环境选股
        
        Args:
            market_regime: BULL_RUN / BEAR_CRASH / SIDEWAYS
        
        Returns:
            筛选后的股票代码列表（取stock_pool和推荐池的交集，如果交集为空则返回全池）
        """
        # 获取该体制推荐的标的池
        regime_config = self.pools.get(market_regime, DEFAULT_POOLS.get(market_regime, {}))
        recommended = set(regime_config.get("stocks", []))
        user_pool = set(self.stock_pool)

        # 取交集：从用户关注池中匹配推荐标的
        matched = list(recommended & user_pool)
        
        regime_label = {
            "BULL_RUN": "🐂 牛市",
            "BEAR_CRASH": "🐻 熊市",
            "SIDEWAYS": "📉 震荡市",
        }.get(market_regime, market_regime)

        if matched:
            print(f"{regime_label}：从关注池匹配到 {len(matched)} 只推荐标的: {matched}")
            return matched
        
        # 无匹配时静默回退到全池，但给出提示
        print(f"{regime_label}：关注池无匹配推荐标的，回退到全池: {self.stock_pool}")
        return list(user_pool)


def create_default_config(stock_pool: List[str]):
    """
    创建默认配置文件，从现有用户池中按规则自动分类
    
    Args:
        stock_pool: 用户关注池
    """
    import json
    pools = dict(DEFAULT_POOLS)
    pools["BULL_RUN"]["stocks"] = [s for s in stock_pool]  # 默认全进牛市池
    pools["BEAR_CRASH"]["stocks"] = []
    pools["SIDEWAYS"]["stocks"] = []
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(pools, f, ensure_ascii=False, indent=2)
    print(f"✅ 已创建默认股票池配置文件: {CONFIG_PATH}")
    print(f"  请根据实际持仓编辑各体制对应的证券池")
    print(f"  文件格式: {{'BULL_RUN': {{'stocks': ['600519', ...]}}, 'BEAR_CRASH': {{...}}, 'SIDEWAYS': {{...}}}}")
