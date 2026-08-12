"""
交易成本计算器
模拟真实交易的所有成本：印花税、佣金、过户费、滑点
完全贴合A股和港股真实交易规则
"""
from typing import Literal
from config.constants import get_slippage_bps, COMMISSION_RATE as A_COMMISSION_RATE

class TradingCostCalculator:
    # A股交易费率
    A_STAMP_TAX = 0.001  # 印花税：卖出时收取0.1%
    A_COMMISSION_RATE = A_COMMISSION_RATE  # 佣金：从config.constants统一导入
    A_COMMISSION_MIN = 5.0  # 最低佣金5元
    A_TRANSFER_FEE = 0.00001  # 过户费：万0.1，双向收取
    # 滑点通过 get_slippage_bps(market_cap) 动态计算，见 config/constants.py

    # 港股交易费率
    # P1-FIX: 港股印花税自2023年11月起降为0.065%/边 (原0.13%双边)
    HK_STAMP_TAX = 0.00065  # 0.065% per side (2023-11 reduction)，不足1元按1元
    HK_COMMISSION_RATE = 0.0003  # 佣金：万3
    HK_COMMISSION_MIN = 3.0  # 最低佣金3港币
    HK_TRANSFER_FEE = 0.00002  # 过户费：万0.2
    HK_SLIPPAGE = 0.0015  # 滑点：默认0.15%
    
    @classmethod
    def calculate_trading_cost(
        cls,
        market: Literal["A", "HK"],
        trade_type: Literal["buy", "sell"],
        price: float,
        quantity: int,
        slippage: float = None
    ) -> dict:
        """
        计算交易总成本
        :param market: 市场类型：A=A股，HK=港股
        :param trade_type: 交易类型：buy=买入，sell=卖出
        :param price: 成交价格
        :param quantity: 成交数量
        :param slippage: 自定义滑点，默认使用市场默认值
        :return: 成本明细：
            - total_cost: 总成本
            - stamp_tax: 印花税
            - commission: 佣金
            - transfer_fee: 过户费
            - slippage_cost: 滑点成本
            - actual_trade_amount: 实际交易金额（含滑点）
            - net_amount: 净收付金额（买入是支出，卖出是收入）
        """
        trade_amount = price * quantity
        # 滑点成本
        if slippage is None:
            if market == "A":
                slippage_bps = get_slippage_bps()  # 从config.constants统一读取
                slippage = slippage_bps / 10000.0
            else:
                slippage = cls.HK_SLIPPAGE
        slippage_cost = trade_amount * slippage
        # 实际交易金额（含滑点：买入多付，卖出少收）
        if trade_type == "buy":
            actual_price = price * (1 + slippage)
        else:
            actual_price = price * (1 - slippage)
        actual_trade_amount = actual_price * quantity
        
        if market == "A":
            # A股成本计算
            stamp_tax = 0.0
            if trade_type == "sell":
                stamp_tax = actual_trade_amount * cls.A_STAMP_TAX
            
            # 佣金
            commission = actual_trade_amount * cls.A_COMMISSION_RATE
            if commission < cls.A_COMMISSION_MIN:
                commission = cls.A_COMMISSION_MIN
            
            # 过户费：双向收取
            transfer_fee = actual_trade_amount * cls.A_TRANSFER_FEE
            
        elif market == "HK":
            # 港股成本计算
            stamp_tax = actual_trade_amount * cls.HK_STAMP_TAX
            stamp_tax = max(1.0, stamp_tax)  # 最低1港币
            
            # 佣金
            commission = actual_trade_amount * cls.HK_COMMISSION_RATE
            if commission < cls.HK_COMMISSION_MIN:
                commission = cls.HK_COMMISSION_MIN
            
            # 过户费
            transfer_fee = actual_trade_amount * cls.HK_TRANSFER_FEE
            
        else:
            raise ValueError(f"不支持的市场类型：{market}")
        
        total_cost = stamp_tax + commission + transfer_fee + slippage_cost
        
        # 净收付金额：买入 = 实际交易金额 + 总成本（支出），卖出 = 实际交易金额 - 总成本（收入）
        if trade_type == "buy":
            net_amount = actual_trade_amount + total_cost
        else:
            net_amount = actual_trade_amount - total_cost
        
        return {
            "total_cost": round(total_cost, 2),
            "stamp_tax": round(stamp_tax, 2),
            "commission": round(commission, 2),
            "transfer_fee": round(transfer_fee, 2),
            "slippage_cost": round(slippage_cost, 2),
            "actual_price": round(actual_price, 2),
            "actual_trade_amount": round(actual_trade_amount, 2),
            "net_amount": round(net_amount, 2)
        }
    
    @classmethod
    def calculate_profit(
        cls,
        market: Literal["A", "HK"],
        buy_price: float,
        sell_price: float,
        quantity: int
    ) -> dict:
        """
        计算一笔完整交易的实际利润和收益率
        :param market: 市场类型
        :param buy_price: 买入价格
        :param sell_price: 卖出价格
        :param quantity: 交易数量
        :return: 收益明细
        """
        # 买入成本
        buy_cost = cls.calculate_trading_cost(market, "buy", buy_price, quantity)
        total_buy_amount = buy_cost["net_amount"]  # 买入总支出
        
        # 卖出收入
        sell_cost = cls.calculate_trading_cost(market, "sell", sell_price, quantity)
        total_sell_amount = sell_cost["net_amount"]  # 卖出总收入
        
        profit = total_sell_amount - total_buy_amount
        profit_rate = profit / total_buy_amount
        
        return {
            "profit": round(profit, 2),
            "profit_rate": round(profit_rate * 100, 2),
            "total_buy_cost": buy_cost["total_cost"],
            "total_sell_cost": sell_cost["total_cost"],
            "total_fees": round(buy_cost["total_cost"] + sell_cost["total_cost"], 2),
            "fee_ratio": round((buy_cost["total_cost"] + sell_cost["total_cost"]) / total_buy_amount * 100, 2)
        }

# 全局单例
trading_cost_calculator = TradingCostCalculator()
