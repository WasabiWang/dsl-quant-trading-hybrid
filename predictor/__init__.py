"""DSL量化预测模块 - 多Agent + LightGBM多因子预测机制

统一入口:
    from predictor import FeatureEngineer, ModelTrainer
    from predictor import AgentSwarm, DualAgentVerifier
    from predictor import PredictorCron, load_stock_data

数据源:
    from predictor.mairui_data import get_history_kline, get_realtime_quote
"""

from predictor.features import FeatureEngineer
from predictor.models import ModelTrainer
from predictor.agents import AgentSwarm, macro_agent, flow_agent, tech_agent, risk_agent, sentiment_agent, profiling_agent
from predictor.verifier import DualAgentVerifier
from predictor.data_loader import load_stock_data, list_available_symbols, get_data_summary, load_multi_stocks
from predictor.scheduler import PredictorCron
from predictor.mairui_data import (
    get_history_kline, get_realtime_quote, get_fundamentals,
    get_basic_info, get_history_macd, get_history_kdj,
    get_capital_flow, get_stop_price, load_stock_data_mairui
)

__all__ = [
    'FeatureEngineer',
    'ModelTrainer', 
    'AgentSwarm',
    'DualAgentVerifier',
    'PredictorCron',
    'load_stock_data',
    'list_available_symbols',
    'get_data_summary',
    'load_multi_stocks',
    # 麦蕊数据源
    'get_history_kline',
    'get_realtime_quote',
    'get_fundamentals',
    'get_basic_info',
    'get_history_macd',
    'get_history_kdj',
    'get_capital_flow',
    'get_stop_price',
    'load_stock_data_mairui',
]
