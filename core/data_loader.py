#!/usr/bin/env python3
"""
数据加载器 - 支持多数据源、假期避让、实时更新
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import json
import time
from typing import Optional, Dict, List, Tuple
import warnings
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)

class DataLoader:
    """智能数据加载器，支持多数据源和假期避让"""
    
    def __init__(self, config_path: str = None):
        """
        初始化数据加载器
        
        Args:
            config_path: 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.cache = {}
        self.cache_ttl = 300  # 5分钟缓存
        self.holidays = self._load_holidays()
        
        # 初始化数据源
        self.data_sources = self._init_data_sources()
        
        logger.info("数据加载器初始化完成")
    
    def _load_config(self, config_path: str) -> Dict:
        """加载配置"""
        import os as _os
        _tushare_token = _os.environ.get('TUSHARE_TOKEN', '')
        default_config = {
            'data_sources': {
                'tushare': {
                    'enable': bool(_tushare_token),
                    'token': _tushare_token,
                    'priority': 1
                },
                'sina': {
                    'enable': True,
                    'priority': 2
                },
                'eastmoney': {
                    'enable': True,
                    'priority': 3
                },
                'akshare': {
                    'enable': True,
                    'priority': 4
                },
                'yfinance': {
                    'enable': True,
                    'priority': 5
                }
            },
            'cache': {
                'enabled': True,
                'ttl': 300
            },
            'holiday_check': {
                'enabled': True,
                'auto_adjust': True
            }
        }
        
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    user_config = json.load(f)
                    # 合并配置
                    default_config.update(user_config)
            except Exception as e:
                logger.warning(f"加载配置文件失败: {e}")
        
        return default_config
    
    def _load_holidays(self) -> List[str]:
        """加载假期数据: 统一委托给 config.holiday_calendar

        消除与 holiday_calendar.py 的重复逻辑。
        返回格式: ['YYYY-MM-DD', ...]
        """
        from config.holiday_calendar import get_holidays_for_year
        now = datetime.now()
        # 覆盖前后各1年确保足够范围
        holidays_set = set()
        for y in range(now.year - 1, now.year + 2):
            h = get_holidays_for_year(y)
            if h:
                holidays_set.update(d.strftime('%Y-%m-%d') for d in h)
        return sorted(holidays_set)
    
    def _init_data_sources(self) -> Dict:
        """初始化数据源"""
        data_sources = {}
        
        # tushare
        if self.config['data_sources']['tushare']['enable']:
            try:
                import tushare as ts
                token = self.config['data_sources']['tushare']['token']
                ts.set_token(token)
                data_sources['tushare'] = {
                    'module': ts,
                    'pro': ts.pro_api(),
                    'priority': self.config['data_sources']['tushare']['priority']
                }
                logger.info("tushare数据源初始化成功")
            except Exception as e:
                logger.warning(f"tushare初始化失败: {e}")
        
        # 新浪财经
        if self.config['data_sources']['sina']['enable']:
            try:
                # 新浪财经接口在fetch_market_data.py中
                data_sources['sina'] = {
                    'priority': self.config['data_sources']['sina']['priority']
                }
                logger.info("新浪财经数据源初始化成功")
            except Exception as e:
                logger.warning(f"新浪财经初始化失败: {e}")
        
        # 东方财富
        if self.config['data_sources']['eastmoney']['enable']:
            try:
                data_sources['eastmoney'] = {
                    'priority': self.config['data_sources']['eastmoney']['priority']
                }
                logger.info("东方财富数据源初始化成功")
            except Exception as e:
                logger.warning(f"东方财富初始化失败: {e}")
        
        # akshare
        if self.config['data_sources']['akshare']['enable']:
            try:
                import akshare as ak
                data_sources['akshare'] = {
                    'module': ak,
                    'priority': self.config['data_sources']['akshare']['priority']
                }
                logger.info("akshare数据源初始化成功")
            except Exception as e:
                logger.warning(f"akshare初始化失败: {e}")
        
        # yfinance
        if self.config['data_sources']['yfinance']['enable']:
            try:
                import yfinance as yf
                data_sources['yfinance'] = {
                    'module': yf,
                    'priority': self.config['data_sources']['yfinance']['priority']
                }
                logger.info("yfinance数据源初始化成功")
            except Exception as e:
                logger.warning(f"yfinance初始化失败: {e}")
        
        logger.info(f"共初始化 {len(data_sources)} 个数据源")
        return data_sources
    
    def is_trading_day(self, date: str = None) -> bool:
        """
        检查是否为交易日
        
        Args:
            date: 日期字符串 'YYYY-MM-DD'，默认为今天
            
        Returns:
            bool: 是否为交易日
        """
        if not self.config['holiday_check']['enabled']:
            return True
        
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
        
        # 检查是否为假期
        if date in self.holidays:
            logger.info(f"{date} 是假期")
            return False
        
        # 检查是否为周末
        dt = datetime.strptime(date, '%Y-%m-%d')
        if dt.weekday() >= 5:  # 5=周六, 6=周日
            logger.info(f"{date} 是周末")
            return False
        
        # 检查是否为特殊休市日（如台风、特殊事件）
        # 这里可以添加更多的检查逻辑
        
        return True
    
    def adjust_for_holiday(self, start_date: str, end_date: str) -> Tuple[str, str]:
        """
        根据假期调整日期范围
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            调整后的开始日期和结束日期
        """
        if not self.config['holiday_check']['auto_adjust']:
            return start_date, end_date
        
        # 向后调整开始日期，避开假期
        adjusted_start = start_date
        while not self.is_trading_day(adjusted_start):
            dt = datetime.strptime(adjusted_start, '%Y-%m-%d')
            dt = dt - timedelta(days=1)
            adjusted_start = dt.strftime('%Y-%m-%d')
            logger.debug(f"调整开始日期避开假期: {adjusted_start}")
        
        # 向前调整结束日期，避开假期
        adjusted_end = end_date
        while not self.is_trading_day(adjusted_end):
            dt = datetime.strptime(adjusted_end, '%Y-%m-%d')
            dt = dt + timedelta(days=1)
            adjusted_end = dt.strftime('%Y-%m-%d')
            logger.debug(f"调整结束日期避开假期: {adjusted_end}")
        
        if adjusted_start != start_date or adjusted_end != end_date:
            logger.info(f"日期调整: {start_date}->{adjusted_start}, {end_date}->{adjusted_end}")
        
        return adjusted_start, adjusted_end
    
    def load_stock_data(self, symbol: str, start_date: str, end_date: str, 
                       adjust: str = 'qfq', market: str = 'CN') -> Optional[pd.DataFrame]:
        """
        加载股票数据，支持多数据源回退
        
        Args:
            symbol: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            adjust: 复权类型 'qfq'/'hfq'/''
            market: 市场 'CN'/'HK'/'US'
            
        Returns:
            DataFrame 或 None
        """
        # 检查缓存
        cache_key = f"{symbol}_{start_date}_{end_date}_{adjust}_{market}"
        if self.config['cache']['enabled'] and cache_key in self.cache:
            timestamp, data = self.cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                logger.debug(f"使用缓存数据: {symbol}")
                return data.copy()
        
        # 根据假期调整日期
        if self.config['holiday_check']['enabled']:
            start_date, end_date = self.adjust_for_holiday(start_date, end_date)
        
        logger.info(f"加载数据: {symbol} {start_date} 到 {end_date} ({market})")
        
        # 按优先级尝试数据源
        sorted_sources = sorted(self.data_sources.items(), 
                              key=lambda x: x[1]['priority'])
        
        df = None
        used_source = None
        
        for source_name, source_info in sorted_sources:
            try:
                if source_name == 'tushare' and market in ['CN', 'HK']:
                    df = self._load_from_tushare(symbol, start_date, end_date, adjust, market)
                elif source_name == 'sina' and market == 'CN':
                    df = self._load_from_sina(symbol, start_date, end_date)
                elif source_name == 'eastmoney' and market == 'CN':
                    df = self._load_from_eastmoney(symbol, start_date, end_date)
                elif source_name == 'akshare' and market == 'CN':
                    df = self._load_from_akshare(symbol, start_date, end_date, adjust)
                elif source_name == 'yfinance' and market in ['US', 'HK']:
                    df = self._load_from_yfinance(symbol, start_date, end_date)
                
                if df is not None and not df.empty:
                    used_source = source_name
                    logger.info(f"数据源 {source_name} 成功: {len(df)} 条记录")
                    break
                    
            except Exception as e:
                logger.warning(f"数据源 {source_name} 失败: {e}")
                continue
        
        if df is None or df.empty:
            logger.error(f"所有数据源都失败: {symbol}")
            return None

        # 标准化数据格式
        df = self._standardize_data(df, symbol, market)

        # v4.5.12 fix: ST股自动过滤
        if df.get('is_st', pd.Series([0])).iloc[0] == 1:
            logger.warning(f"⚠️ {symbol} 为ST/*ST股，自动过滤")
            # 返回带is_st标记的空DataFrame（保留列结构便于调用方判断）
            # 调用方应检查返回值是否为空或is_st标记
            return None

        # 缓存数据
        if self.config['cache']['enabled']:
            self.cache[cache_key] = (time.time(), df.copy())

        logger.info(f"数据加载完成: {symbol} ({len(df)} 条记录, 来源: {used_source})")
        return df
    
    def _load_from_tushare(self, symbol: str, start_date: str, end_date: str,
                          adjust: str, market: str) -> Optional[pd.DataFrame]:
        """从tushare加载数据 (P2-15 fix: 支持复权参数)"""
        try:
            ts_code = symbol
            if market == 'HK' and not symbol.endswith('.HK'):
                ts_code = f"{symbol}.HK"

            # P2-15: 使用pro_bar接口支持复权 (qfq/hfq/None)
            adj_map = {"qfq": "qfq", "hfq": "hfq", "": None, "none": None}
            adj_param = adj_map.get(adjust.lower() if adjust else "", None)
            try:
                df = self.data_sources['tushare']['pro'].pro_bar(
                    ts_code=ts_code,
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    adj=adj_param or 'qfq',
                    factors=['tor', 'vr']  # 换手率+量比
                )
            except Exception:
                # pro_bar不可用，降级到daily API（不复权）
                df = self.data_sources['tushare']['pro'].daily(
                    ts_code=ts_code,
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', '')
                )
                if df is not None and not df.empty:
                    logger.warning(f"⚠️ {symbol} TuShare数据未复权(pro_bar不可用), 若需要复权建议优先使用akshare")

            if df is not None and not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.set_index('trade_date').sort_index()
                return df

        except Exception as e:
            logger.warning(f"tushare加载失败: {e}")

        return None
    
    def _load_from_sina(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从新浪财经加载数据"""
        try:
            # 这里调用fetch_market_data.py中的函数
            sys.path.insert(0, os.path.expanduser('~/.openclaw/workspace'))
            from scripts.fetch_market_data import get_stock_historical
            
            df = get_stock_historical(symbol, start_date, end_date)
            return df
            
        except Exception as e:
            logger.warning(f"新浪财经加载失败: {e}")
        
        return None
    
    def _load_from_eastmoney(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从东方财富加载数据 v4.5.12"""
        try:
            import requests
            # 判断市场前缀
            if symbol.startswith('6'):
                secid = f'1.{symbol}'
            elif symbol.startswith('0') or symbol.startswith('3'):
                secid = f'0.{symbol}'
            else:
                secid = f'1.{symbol}'
            beg = start_date.replace('-', '')
            end = end_date.replace('-', '')
            url = (
                f'https://push2his.eastmoney.com/api/qt/stock/kline/get'
                f'?secid={secid}&fields1=f1,f2,f3,f4,f5,f6'
                f'&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
                f'&klt=101&fqt=1&beg={beg}&end={end}'
            )
            resp = requests.get(url, timeout=10,
                headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                         'Referer': 'https://quote.eastmoney.com/'})
            data = resp.json()
            if data.get('data') and data['data'].get('klines'):
                rows = []
                for line in data['data']['klines']:
                    fields = line.split(',')
                    rows.append({
                        'date': fields[0],
                        'open': float(fields[1]),
                        'close': float(fields[2]),
                        'high': float(fields[3]),
                        'low': float(fields[4]),
                        'volume': float(fields[5]),
                        'amount': float(fields[6]),
                        'pct_chg': float(fields[8]) if len(fields) > 8 else 0,
                        'turnover': float(fields[10]) if len(fields) > 10 else 0,
                    })
                df = pd.DataFrame(rows)
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date').sort_index()
                return df
        except Exception as e:
            logger.warning(f"东方财富加载失败: {e}")
        return None
    
    def _load_from_akshare(self, symbol: str, start_date: str, end_date: str, 
                          adjust: str) -> Optional[pd.DataFrame]:
        """从akshare加载数据 — v4.6.4: 东财+BaoStock双层fallback"""
        try:
            code = symbol.split('.')[0]
            df = None
            
            # 优先东财K线
            try:
                df = self.data_sources['akshare']['module'].stock_zh_a_hist(
                    symbol=code,
                    period='daily',
                    start_date=start_date.replace('-', ''),
                    end_date=end_date.replace('-', ''),
                    adjust=adjust
                )
            except Exception:
                pass
            
            # 降级BaoStock
            if df is None or df.empty:
                logger.info(f"东财K线不可用, 降级BaoStock: {symbol}")
                import baostock as bs
                code_num = code.zfill(6)
                bs_code = f"sh.{code_num}" if code_num.startswith(('6','9')) else f"sz.{code_num}"
                
                lg = bs.login()
                try:
                    import pandas as pd
                    fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
                    rs = bs.query_history_k_data_plus(bs_code, fields,
                                                       start_date=start_date.replace('-',''),
                                                       end_date=end_date.replace('-',''),
                                                       frequency="d", adjustflag="2")
                    data_list = []
                    while (rs.error_code == '0') and rs.next():
                        data_list.append(rs.get_row_data())
                    if data_list:
                        df = pd.DataFrame(data_list, columns=fields.split(','))
                        for c in ['open','high','low','close','preclose','volume','amount','turn','pctChg']:
                            df[c] = pd.to_numeric(df[c], errors='coerce')
                        df.rename(columns={
                            'date':'日期','code':'股票代码','open':'开盘','high':'最高',
                            'low':'最低','close':'收盘','volume':'成交量','amount':'成交额',
                            'turn':'换手率','pctChg':'涨跌幅'
                        }, inplace=True)
                finally:
                    bs.logout()
            
            if df is not None and not df.empty:
                df['date'] = pd.to_datetime(df['日期'])
                df = df.set_index('date').sort_index()
                return df
                
        except Exception as e:
            logger.warning(f"akshare/BaoStock加载失败: {e}")
        
        return None
    
    def _load_from_yfinance(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """从yfinance加载数据"""
        try:
            yf_symbol = symbol
            if symbol.endswith('.HK'):
                yf_symbol = symbol.replace('.HK', '.HK')
            
            df = self.data_sources['yfinance']['module'].download(
                yf_symbol,
                start=start_date,
                end=end_date,
                progress=False
            )
            
            if df is not None and not df.empty:
                return df
                
        except Exception as e:
            logger.warning(f"yfinance加载失败: {e}")
        
        return None
    
    def _check_st_status(self, symbol: str) -> bool:
        """检测股票是否为ST/*ST — v4.6.4: 东财API封锁, 改用akshare名称映射"""
        try:
            import akshare as ak
            # 使用 stock_info_a_code_name 获取名称 (不依赖push2 API)
            code = symbol.zfill(6)
            df = ak.stock_info_a_code_name()
            row = df[df['code'].astype(str).str.zfill(6) == code]
            if not row.empty:
                name = str(row['name'].iloc[0])
                return bool(name and ('ST' in name.upper() or '*ST' in name.upper()))
            return False
        except Exception:
            if symbol.startswith('000') or symbol.startswith('600'):
                return False
            return False

    def _standardize_data(self, df: pd.DataFrame, symbol: str, market: str) -> pd.DataFrame:
        """标准化数据格式"""
        # 确保有必要的列
        required_columns = ['open', 'high', 'low', 'close', 'volume']

        # 重命名列
        column_mapping = {
            '开盘': 'open', '开盘价': 'open',
            '最高': 'high', '最高价': 'high',
            '最低': 'low', '最低价': 'low',
            '收盘': 'close', '收盘价': 'close',
            '成交量': 'volume', '成交额': 'amount',
            '涨跌幅': 'pct_chg', '换手率': 'turnover'
        }

        df = df.rename(columns=column_mapping)

        # 添加缺失的列
        for col in required_columns:
            if col not in df.columns:
                if col == 'volume' and 'amount' in df.columns and 'close' in df.columns:
                    df['volume'] = df['amount'] / df['close']
                else:
                    df[col] = np.nan

        # 确保数据类型正确
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 添加股票元信息
        df['symbol'] = symbol
        df['market'] = market
        df['is_st'] = int(self._check_st_status(symbol))

        # 排序
        df = df.sort_index()

        # 去除重复
        df = df[~df.index.duplicated(keep='first')]

        return df
    
    def get_realtime_quote(self, symbol: str, market: str = 'CN') -> Optional[Dict]:
        """
        获取实时行情

        Args:
            symbol: 股票代码
            market: 市场

        Returns:
            实时行情字典，含 price/name/open/high/low/volume/amount/change_pct
        """
        try:
            from core.dsl_data_sdk import get_price
            result = get_price(symbol)
            if result and 'error' not in result:
                return result
        except Exception:
            pass
        return None

# 如果这是模块的主类，确保文件正确结束
if __name__ == "__main__":
    print("DataLoader module loaded successfully")