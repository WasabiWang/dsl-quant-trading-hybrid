#!/usr/bin/env python3
"""
统一数据源适配器 - 对接所有实际数据源
支持：akshare、东方财富、飞书多维表格、新闻API
"""
import os
import sys
import json
import akshare as ak
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Any
import hashlib

# v4.5.1: 数据源适配 — 麦蕊优先, akshare/tushare降级为可选
# akshare
try:
    import akshare as ak
    HAS_AKSHARE = True
except ImportError:
    ak = None
    HAS_AKSHARE = False

# tushare
try:
    import tushare as ts
    ts.set_token(os.getenv("TUSHARE_TOKEN"))
    pro = ts.pro_api()
    HAS_TUSHARE = True
except Exception:
    ts = None
    pro = None
    HAS_TUSHARE = False

# 麦蕊API (v4.5.1 主数据源)
try:
    from predictor.mairui_data import (
        get_realtime_quote, get_realtime_quotes_batch,
        get_history_kline, get_fundamentals,
        get_history_macd, get_history_kdj
    )
    HAS_MAIRUI = True
except ImportError:
    HAS_MAIRUI = False

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from .feishu_bitable import FeishuBitable
from .config import config

# 缓存配置
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "data_adapter")
CACHE_EXPIRE_HOURS = 24
os.makedirs(CACHE_DIR, exist_ok=True)

class DataAdapter:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
    
    def _init(self):
        self.feishu_bitable = FeishuBitable()
        # 飞书多维表格配置
        self.feishu_bitable_token = config.get('FEISHU_BITABLE_TOKEN', '')
        self.holding_table_id = "tblnp0B5TcOyhQCe"  # 持仓追踪表格ID
        self.stock_pool_table_id = "tblxxx"  # 股票池表格ID
        # 接口失败统计
        self.api_fail_count = 0
        self.api_total_count = 0
    
    def _get_cache_key(self, func_name: str, *args, **kwargs) -> str:
        """生成缓存key"""
        key_str = f"{func_name}_{str(args)}_{str(kwargs)}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()
    
    def _read_cache(self, cache_key: str) -> Any:
        """读取缓存，过期返回None"""
        cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            # 检查是否过期
            if datetime.now().timestamp() - cache_data["timestamp"] > CACHE_EXPIRE_HOURS * 3600:
                os.remove(cache_file)
                return None
            return cache_data["data"]
        except Exception as e:
            print(f"⚠️ 读取缓存失败：{e}")
            return None
    
    def _write_cache(self, cache_key: str, data: Any) -> None:
        """写入缓存"""
        try:
            cache_data = {
                "timestamp": datetime.now().timestamp(),
                "data": data
            }
            cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 写入缓存失败：{e}")
    
    def get_api_fail_rate(self) -> float:
        """获取接口失败率"""
        if self.api_total_count == 0:
            return 0.0
        return round(self.api_fail_count / self.api_total_count, 2)
    
    def get_holdings(self) -> List[Dict]:
        """从飞书多维表格获取当前持仓"""
        try:
            records = self.feishu_bitable.list_records(self.feishu_bitable_token, self.holding_table_id)
            holdings = []
            for record in records:
                fields = record['fields']
                holdings.append({
                    'symbol': fields['股票代码'],
                    'stock_name': fields['股票名称'],
                    'position_ratio': fields.get('持仓比例', 0),
                    'cost_price': fields.get('成本价', 0)
                })
            return holdings
        except Exception as e:
            print(f"⚠️ 获取持仓失败：{e}，使用默认持仓")
            return [
                {'symbol': '600760', 'stock_name': '中航沈飞', 'position_ratio': 0.25},
                {'symbol': '000001', 'stock_name': '平安银行', 'position_ratio': 0.2},
                {'symbol': '300750', 'stock_name': '宁德时代', 'position_ratio': 0.3},
                {'symbol': '002594', 'stock_name': '比亚迪', 'position_ratio': 0.25}
            ]
    
    def get_stock_pool(self) -> List[Dict]:
        """获取统一主股票池 — v4.5.1 联合池35只"""
        # 优先从主股票池YAML读取
        master_pool_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'config', 'master_stock_pool.yaml'
        )
        if os.path.exists(master_pool_path):
            try:
                import yaml
                with open(master_pool_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                pool = data.get('master_pool', [])
                if pool and len(pool) > 5:
                    # 去重：同一symbol可能属于多个tier，保留优先级最高的tier
                    TIER_PRIORITY = {'core': 0, 'bluechip': 1, 'growth': 2, 'cyclical': 3, 'flex': 4, 'speculative': 5}
                    seen = {}
                    for s in pool:
                        sym = s['symbol']
                        tier = s.get('tier', '')
                        if sym not in seen or TIER_PRIORITY.get(tier, 99) < TIER_PRIORITY.get(seen[sym].get('tier', ''), 99):
                            seen[sym] = s
                    return [{
                        'symbol': s['symbol'],
                        'name': s.get('name', ''),
                        'stock_name': s.get('name', ''),
                        'tier': s.get('tier', '')
                    } for s in seen.values()]
            except Exception:
                pass
        
        # 回退: config.get_yaml + 持仓
        master_pool = config.get_yaml("master_pool", [])
        if master_pool and len(master_pool) > 5:
            return [{
                'symbol': s['symbol'],
                'name': s.get('name', ''),
                'stock_name': s.get('name', ''),
                'tier': s.get('tier', '')
            } for s in master_pool]
        
        # 最终回退: 持仓 + custom_stock_pool
        holdings = self.get_holdings()
        optional_stocks = config.get_yaml("custom_stock_pool", [
            {'symbol': '600519', 'stock_name': '贵州茅台'},
            {'symbol': '000858', 'stock_name': '五粮液'},
            {'symbol': '601318', 'stock_name': '中国平安'},
            {'symbol': '000063', 'stock_name': '中兴通讯'},
            {'symbol': '600036', 'stock_name': '招商银行'}
        ])
        all_stocks = {}
        for stock in holdings + optional_stocks:
            all_stocks[stock['symbol']] = stock
        return list(all_stocks.values())
    
    def get_sector_fund_flow(self, sector_code: str, sector_name: str = '', days: int = 3) -> float:
        """获取行业资金流得分（0-10分）— 麦蕊实时行情聚合"""
        return self._get_sector_score_from_mairui(sector_code, sector_name, score_type='fund_flow')
    
    def get_sector_price_performance(self, sector_code: str, sector_name: str = '', days: int = 20) -> float:
        """获取行业量价得分（0-10分）— 麦蕊实时行情聚合"""
        return self._get_sector_score_from_mairui(sector_code, sector_name, score_type='price')
    
    def _get_mairui_sector_code(self, sector_name: str) -> str:
        """将行业名称映射为麦蕊申万行业代码"""
        # 映射表（东财/配置名称 → 麦蕊申万代码）
        SECTOR_MAP = {
            '化工': 'sw_jchg', '基础化工': 'sw_jchg',
            '有色金属': 'sw_ysjs', '电子': 'sw_dz',
            '计算机': 'sw_jsj', '医药生物': 'sw_yysw',
            '食品饮料': 'sw_spyl', '银行': 'sw_yx',
            '非银金融': 'sw_fyjr', '房地产': 'sw_fdc',
            '交通运输': 'sw_jtys',
        }
        return SECTOR_MAP.get(sector_name, '')
    
    def _get_sector_score_from_mairui(self, sector_code: str, sector_name: str = '', score_type: str = 'price') -> float:
        """通过麦蕊API获取行业成分股并聚合计算行业得分"""
        try:
            from config.mairui_api_config import get_category_stocks, get_multi_stock_real
            
            # 用sector_code作为cache key
            cache_key = self._get_cache_key(f"sector_{score_type}", sector_code, 0)
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached
            
            # 获取行业名称 → 麦蕊代码（优先使用传入的sector_name）
            search_name = sector_name or sector_code
            mr_code = self._get_mairui_sector_code(search_name)
            if not mr_code:
                # 未知行业，用老方法映射
                mr_code = self._get_mairui_sector_code(
                    search_name.replace('102900000', '化工').replace('102800000', '有色金属')
                    .replace('103000000', '电子').replace('103100000', '计算机')
                    .replace('103200000', '医药生物').replace('103300000', '食品饮料')
                    .replace('103400000', '银行').replace('103500000', '非银金融')
                    .replace('103600000', '房地产').replace('103700000', '交通运输')
                )
            
            if not mr_code:
                return 5.0
            
            # 获取行业成分股
            stocks = get_category_stocks(mr_code)
            if not stocks or len(stocks) < 3:
                return 5.0
            
            # 过滤A股主板/创业板（排除北交所bj）
            a_stocks = [s for s in stocks if s.get('jys') in ('sh', 'sz', 'SH', 'SZ')]
            if not a_stocks:
                a_stocks = stocks  # 回退全部
            
            # 取前15只（按列表顺序，尽可能多覆盖）
            sample = a_stocks[:min(15, len(a_stocks))]
            codes = [s['dm'].split('.')[0] for s in sample]
            
            # 批量获取实时行情（麦蕊支持最多20只）
            try:
                if len(codes) <= 20:
                    quotes = get_multi_stock_real(codes)
                else:
                    quotes = get_multi_stock_real(codes[:20])
                    quotes += get_multi_stock_real(codes[20:40]) if len(codes) > 20 else []
            except Exception:
                # 回退：逐只获取
                from config.mairui_api_config import get_stock_real
                quotes = []
                for c in codes[:15]:
                    try:
                        quotes.append(get_stock_real(c))
                    except Exception:
                        pass
            
            if not quotes:
                return 5.0
            
            if score_type == 'fund_flow':
                # 资金流得分：基于换手率(turnover_rate=tr)聚合
                # tr = 换手率%，高换手率表示资金活跃
                turnovers = []
                for q in quotes:
                    tr = q.get('tr', q.get('turnover_rate', 1))
                    if tr:
                        try:
                            turnovers.append(float(tr))
                        except (ValueError, TypeError):
                            turnovers.append(1.0)
                if not turnovers:
                    score = 5.0
                else:
                    avg_tr = sum(turnovers) / len(turnovers)
                    # 换手率2%→5分, 5%→7.5分, 0.5%→2.5分
                    score = min(10, max(0, 2.5 + avg_tr * 1.25))
                
            else:  # price performance
                # 价格表现：基于涨跌幅(pc)聚合
                changes = []
                for q in quotes:
                    cp = q.get('pc', q.get('change_percent', 0))
                    if cp is not None:
                        try:
                            changes.append(float(cp))
                        except (ValueError, TypeError):
                            changes.append(0.0)
                if not changes:
                    score = 5.0
                else:
                    avg_change = sum(changes) / len(changes)
                    # 平均涨跌0%→5分, +2%→7分, -2%→3分
                    score = min(10, max(0, 5 + avg_change * 1.0))
            
            result = round(score, 2)
            self._write_cache(cache_key, result)
            return result
            
        except Exception as e:
            print(f"⚠️ 麦蕊行业{score_type}计算失败: {e}")
            return 5.0
    
    def get_stock_technical_score(self, symbol: str, days: int = 20) -> float:
        """计算个股技术面得分（0-10分）— 使用麦蕊K线数据"""
        try:
            from dsl_data_sdk_original import get_kline, normalize_symbol
            # 获取近30天日K线
            normalized = normalize_symbol(symbol)
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=days+15)).strftime('%Y-%m-%d')
            kline_data = get_kline(normalized, start_date, end_date)
            
            if not kline_data or len(kline_data) < days:
                return 5.0
            
            # 转为pandas方便计算
            df = pd.DataFrame(kline_data)
            close = df['close'].astype(float)
            volume = df['volume'].astype(float)
            
            # 1. 20日动量（3分）
            mom_20 = (close.iloc[-1] / close.iloc[-20]) - 1
            mom_score = min(3, max(0, mom_20 * 20 + 1.5))
            
            # 2. RSI指标（3分）
            delta = close.iloc[-14:].values - close.iloc[-15:-1].values
            delta_series = pd.Series(delta)
            gain = (delta_series.where(delta_series > 0, 0)).mean()
            loss = (-delta_series.where(delta_series < 0, 0)).mean()
            if loss == 0:
                rsi = 100
            else:
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
            rsi_score = min(3, max(0, rsi / 100 * 3))
            
            # 3. 量价配合（4分）
            volume_ma5 = volume[-5:].mean()
            volume_ma10 = volume[-10:].mean()
            volume_ratio = volume_ma5 / volume_ma10 if volume_ma10 > 0 else 1
            price_up = close.iloc[-1] > close.iloc[-5]
            if price_up and volume_ratio > 1.2:
                volume_score = 4
            elif price_up and volume_ratio > 0.9:
                volume_score = 3
            elif not price_up and volume_ratio < 0.8:
                volume_score = 2
            else:
                volume_score = 2.5
            
            total_score = mom_score + rsi_score + volume_score
            return round(total_score, 2)
        except Exception as e:
            print(f"⚠️ 计算股票{symbol}技术得分失败：{e}")
            return 5.0
    
    def get_stock_fundamental_score(self, symbol: str) -> float:
        """计算个股基本面得分（0-10分）— 统一使用麦蕊API"""
        try:
            from config.mairui_api_config import get_financial_indicators, get_stock_real
            # 麦蕊财务接口需要带后缀：600519.SH / 000001.SZ
            mr_symbol = f"{symbol}.SH" if symbol.startswith('6') else f"{symbol}.SZ"
            
            # 1. 成长性得分 — 麦蕊财务主要指标
            try:
                fin_data = get_financial_indicators(mr_symbol, limit=1)
                if isinstance(fin_data, list) and fin_data:
                    latest = fin_data[0] if isinstance(fin_data[0], dict) else {}
                    # 净利润同比增长率 (字段名可能为 zysrzz/zsrzz/yyzsrzz)
                    yoy_growth = 0
                    for key in ['zysrzz', 'zsrzz', 'yyzsrzz', 'yysrzz', '净利润同比增长率']:
                        if key in latest and latest[key] is not None:
                            val = latest[key]
                            if isinstance(val, str):
                                yoy_growth = float(val.strip('%')) / 100 if '%' in val else float(val)
                            else:
                                yoy_growth = float(val) / 100 if abs(float(val)) > 1 else float(val)
                            break
                    growth_score = min(4, max(0, yoy_growth * 10 + 2))
                else:
                    growth_score = 3
            except Exception as e:
                print(f"⚠️ 获取{symbol}成长性数据失败：{e}")
                growth_score = 3
            
            # 2. 估值得分 — 麦蕊实时行情的pe_ttm/pb
            try:
                realtime = get_stock_real(symbol)
                pe_ttm = realtime.get('pe_ttm')
                if pe_ttm and float(pe_ttm) > 0:
                    pe_ttm = float(pe_ttm)
                    industry_pe_avg = 30  # 行业均值，后续可动态获取
                    if pe_ttm < industry_pe_avg * 0.8:
                        pe_score = 3
                    elif pe_ttm < industry_pe_avg * 1.2:
                        pe_score = 2 + (industry_pe_avg * 1.2 - pe_ttm) / (industry_pe_avg * 0.4)
                    else:
                        pe_score = 1
                else:
                    pe_score = 2
            except Exception as e:
                print(f"⚠️ 获取{symbol}估值失败：{e}")
                pe_score = 2
            
            # 3. ROE得分 — 麦蕊财务主要指标
            try:
                if isinstance(fin_data, list) and fin_data:
                    latest = fin_data[0] if isinstance(fin_data[0], dict) else {}
                    roe = 0
                    for key in ['roe', 'jzcsyl', '净资产收益率', 'ROE']:
                        if key in latest and latest[key] is not None:
                            val = latest[key]
                            if isinstance(val, str):
                                roe = float(val.strip('%')) / 100 if '%' in val else float(val)
                            else:
                                roe = float(val) / 100 if abs(float(val)) > 1 else float(val)
                            break
                    roe_score = min(3, max(0, roe * 20))
                else:
                    roe_score = 2
            except Exception as e:
                print(f"⚠️ 获取{symbol}ROE失败：{e}")
                roe_score = 2
            
            total_score = growth_score + pe_score + roe_score
            return round(total_score, 2)
        except Exception as e:
            print(f"⚠️ 计算股票{symbol}基本面得分失败：{e}")
            return 5.0
    
    def scan_earning_events(self) -> List[Dict]:
        """扫描业绩超预期事件"""
        events = []
        try:
            # 获取最近3天的业绩预告
            start_date = (datetime.now() - timedelta(days=3)).strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            notice_df = ak.stock_notice_report(start_date=start_date, end_date=end_date)
            
            for _, row in notice_df.iterrows():
                if '业绩预告' in row['公告类型'] and '超预期' in row['公告内容']:
                    # 计算历史胜率
                    win_rate = self._get_event_win_rate('earning_surprise', row['股票代码'])
                    events.append({
                        'type': 'earning_surprise',
                        'symbol': row['股票代码'],
                        'stock_name': row['股票简称'],
                        'title': f"{row['股票简称']}业绩超预期",
                        'content': row['公告内容'][:100] + "...",
                        'win_rate': win_rate,
                        'publish_time': row['公告时间']
                    })
            return events
        except Exception as e:
            print(f"⚠️ 扫描业绩事件失败：{e}")
            return []
    
    def scan_policy_events(self) -> List[Dict]:
        """扫描政策利好事件"""
        events = []
        try:
            # 对接新闻API获取行业政策
            # 临时实现，后续对接实际新闻API
            return []
        except Exception as e:
            print(f"⚠️ 扫描政策事件失败：{e}")
            return []
    
    def scan_increase_events(self) -> List[Dict]:
        """扫描大额增持/回购事件"""
        events = []
        try:
            # 获取最近3天的增持回购公告
            start_date = (datetime.now() - timedelta(days=3)).strftime('%Y%m%d')
            end_date = datetime.now().strftime('%Y%m%d')
            notice_df = ak.stock_notice_report(start_date=start_date, end_date=end_date)
            
            for _, row in notice_df.iterrows():
                if '增持' in row['公告类型'] or '回购' in row['公告类型']:
                    # 计算历史胜率
                    win_rate = self._get_event_win_rate('increase_repurchase', row['股票代码'])
                    events.append({
                        'type': 'increase_repurchase',
                        'symbol': row['股票代码'],
                        'stock_name': row['股票简称'],
                        'title': f"{row['股票简称']}{row['公告类型']}",
                        'content': row['公告内容'][:100] + "...",
                        'win_rate': win_rate,
                        'publish_time': row['公告时间']
                    })
            return events
        except Exception as e:
            print(f"⚠️ 扫描增持事件失败：{e}")
            return []
    
    def _get_event_win_rate(self, event_type: str, symbol: str = None) -> float:
        """获取事件历史胜率"""
        # 从事件回测数据库读取历史胜率，临时返回默认值
        win_rate_map = {
            'earning_surprise': 72,
            'increase_repurchase': 65,
            'policy_benefit': 68
        }
        return win_rate_map.get(event_type, 60)

# 全局实例
data_adapter = DataAdapter()
