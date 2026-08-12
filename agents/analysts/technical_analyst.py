#!/usr/bin/env python3
"""
技术面分析师 - 计算个股技术指标得分
对接akshare真实行情数据，实现多维度技术评分
"""
import os
import sys
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.data_adapter import data_adapter

class TechnicalAnalyst:
    def __init__(self):
        pass

    def calculate_technical_score(self, symbol: str, days: int = 20) -> float:
        """
        计算个股技术面综合得分(0-10分)
        维度：动量(30%) + RSI(20%) + MACD(20%) + 量价配合(30%)
        """
        try:
            # A股代码处理
            if symbol.startswith('6'):
                ak_symbol = f"sh{symbol}"
            else:
                ak_symbol = f"sz{symbol}"
            
            # 获取行情数据
            start_date = (datetime.now() - timedelta(days=days + 30)).strftime("%Y%m%d")
            # P2-FIX: 添加end_date参数，防止回测中拉取未来数据
            end_date = datetime.now().strftime("%Y%m%d")
            stock_df = ak.stock_zh_a_daily(symbol=ak_symbol, start_date=start_date, end_date=end_date)
            
            if len(stock_df) < days + 10:
                return 5.0
            
            close = stock_df["close"].values
            volume = stock_df["volume"].values
            
            # 1. 20日动量得分(30%)
            mom_20 = (close[-1] / close[-days] - 1) * 100
            # 相对于大盘的超额收益
            hs300_return = self._get_hs300_return(days)
            excess_mom = mom_20 - hs300_return
            mom_score = min(3, max(0, excess_mom * 0.3 + 1.5))
            
            # 2. RSI得分(20%)
            delta = close[-14:] - close[-15:-1]
            gain = (delta[delta > 0].sum() / 14) if len(delta[delta > 0]) > 0 else 0
            loss = (-delta[delta < 0].sum() / 14) if len(delta[delta < 0]) > 0 else 0
            
            if loss == 0:
                rsi = 100
            elif gain == 0:
                rsi = 0
            else:
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
            # RSI在30-70为正常，50为最优
            rsi_score = min(2, max(0, 2 - abs(rsi - 50) / 25 * 2))
            
            # 3. MACD得分(20%)
            ema12 = self._calculate_ema(close, 12)
            ema26 = self._calculate_ema(close, 26)
            dif = ema12 - ema26
            dea = self._calculate_ema(dif, 9)
            macd_bar = (dif[-1] - dea[-1]) * 2
            # MACD红柱且向上为好
            if macd_bar > 0 and dif[-1] > dea[-1]:
                macd_score = 2.0
            elif macd_bar < 0 and dif[-1] < dea[-1]:
                macd_score = 0.5
            else:
                macd_score = 1.0
            
            # 4. 量价配合得分(30%)
            volume_ma5 = volume[-5:].mean()
            volume_ma10 = volume[-10:].mean()
            volume_ratio = volume_ma5 / volume_ma10 if volume_ma10 > 0 else 1
            price_change = (close[-1] / close[-5] - 1) * 100
            # 价涨量增为最优
            if price_change > 2 and volume_ratio > 1.2:
                volume_score = 3.0
            elif price_change > 0 and volume_ratio > 0.9:
                volume_score = 2.0
            elif price_change < -2 and volume_ratio > 1.2:
                volume_score = 0.5
            else:
                volume_score = 1.5
            
            total_score = round(mom_score + rsi_score + macd_score + volume_score, 2)
            return total_score
        
        except Exception as e:
            print(f"计算{symbol}技术得分失败: {e}")
            return -1.0  # P2-FIX: 返回-1表示数据不可用，区别于5.0(中性)

    def get_fusion_score(self, symbol: str, days: int = 20) -> dict:
        """P2-FIX: 返回decision_fusion兼容格式的技术面评分"""
        raw_score = self.calculate_technical_score(symbol, days)
        # -1.0 means data unavailable
        if raw_score == -1.0:
            return {
                "tech_score": 0.0,
                "confidence": 0.0,
                "raw_score": raw_score,
                "recommendation": "NO_DATA",
            }
        # 0-10 → [-1, 1]: 5=neutral, 0=-1, 10=+1
        fusion_score = (raw_score - 5.0) / 5.0
        fusion_score = max(-1.0, min(1.0, fusion_score))
        confidence = min(1.0, abs(fusion_score) * 1.5)
        return {
            "tech_score": round(fusion_score, 4),
            "confidence": round(confidence, 4),
            "raw_score": raw_score,  # 保留原始0-10分数
            "recommendation": "BUY" if fusion_score > 0.3 else ("SELL" if fusion_score < -0.3 else "HOLD"),
        }

    def is_data_available(self, symbol: str) -> bool:
        """P2-FIX: 检查数据是否可用"""
        try:
            if symbol.startswith('6'):
                ak_symbol = f"sh{symbol}"
            else:
                ak_symbol = f"sz{symbol}"
            stock_df = ak.stock_zh_a_daily(symbol=ak_symbol, start_date=(datetime.now() - timedelta(days=5)).strftime("%Y%m%d"))
            return len(stock_df) >= 3
        except:
            return False

    def calculate_fundamental_score(self, symbol: str) -> float:
        """计算个股基本面得分(0-10分)（多数据源自动回退）"""
        market = "sh" if symbol.startswith("6") else "sz"
        # 优先级1：同花顺财务接口
        try:
            import time
            time.sleep(0.3)
            fundamental_df = ak.stock_financial_report_sina(stock=symbol, symbol="主要指标")
            if not fundamental_df.empty:
                roe = float(fundamental_df.iloc[0]['净资产收益率'])
                pe = float(fundamental_df.iloc[0]['市盈率']) if '市盈率' in fundamental_df.columns else 10
                pe_score = min(4, max(0, 4 - pe/10 if pe > 0 else 4))
                roe_score = min(3, max(0, roe/5))
                total_score = pe_score + roe_score
                return round(total_score, 2)
        except Exception as e:
            print(f"同花顺获取{symbol}基本面失败: {e}，尝试新浪财经接口")
        
        # 优先级2：新浪财经财务接口
        try:
            import time
            time.sleep(0.3)
            fundamental_df = ak.stock_financial_fx(symbol=f"{market}{symbol}")
            if not fundamental_df.empty:
                pe = float(fundamental_df.iloc[0]['市盈率'])
                roe = float(fundamental_df.iloc[0]['净资产收益率'])
                pe_score = min(4, max(0, 4 - pe/10 if pe > 0 else 4))
                roe_score = min(3, max(0, roe/5))
                total_score = pe_score + roe_score
                return round(total_score, 2)
        except Exception as e:
            print(f"新浪财经获取{symbol}基本面失败: {e}，使用默认值3分")
        
        return 3.0

    def _get_hs300_return(self, days: int) -> float:
        """获取沪深300同期收益率"""
        try:
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
            hs300_df = ak.stock_zh_index_daily(symbol="sh000300", start_date=start_date, end_date=end_date)
            if len(hs300_df) >= days:
                return (hs300_df["close"].iloc[-1] / hs300_df["close"].iloc[-days] - 1) * 100
        except Exception as e:
            print(f"获取沪深300收益率失败: {e}")
        return 0.0

    def _calculate_ema(self, values: np.array, period: int) -> np.array:
        """计算指数移动平均线"""
        ema = np.zeros_like(values)
        ema[0] = values[0]
        multiplier = 2 / (period + 1)
        for i in range(1, len(values)):
            ema[i] = (values[i] - ema[i-1]) * multiplier + ema[i-1]
        return ema
