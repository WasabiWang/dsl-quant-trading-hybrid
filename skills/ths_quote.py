#!/usr/bin/env python3
"""
同花顺行情Skill v1.0
完全兼容akshare接口格式，可无缝替换
基于pytdx实现，稳定高速
"""
import os
import pandas as pd
from pytdx.hq import TdxHq_API

os.environ['NO_PROXY'] = '*'

class THSQuote:
    def __init__(self):
        self.api = TdxHq_API()
        # 同花顺行情服务器地址（国内高可用节点）
        self.server = ('114.80.63.18', 7709)
        
    def _connect(self):
        """连接行情服务器，兼容不同pytdx版本"""
        try:
            if hasattr(self.api, 'is_connected') and self.api.is_connected():
                return
            if hasattr(self.api, 'connected') and self.api.connected:
                return
        except:
            pass
        # 未连接，重新建立连接
        self.api.connect(*self.server, time_out=5)
    
    def stock_zh_a_spot(self):
        """获取A股实时行情，返回格式与akshare完全一致"""
        self._connect()
        # 获取沪市+深市股票
        sh_data = self.api.get_security_list(1, 0)
        sz_data = self.api.get_security_list(0, 0)
        
        # 合并数据
        df = pd.concat([
            pd.DataFrame(sh_data),
            pd.DataFrame(sz_data)
        ])
        
        # 字段映射到akshare格式
        df = df.rename(columns={
            'code': '代码',
            'name': '名称',
            'price': '最新价',
            'last_close': '昨收',
            'open': '今开',
            'high': '最高',
            'low': '最低',
            'vol': '成交量',
            'amount': '成交额'
        })
        
        # 补全涨跌幅等字段
        df['涨跌幅'] = (df['最新价'] - df['昨收']) / df['昨收'] * 100
        df['涨跌额'] = df['最新价'] - df['昨收']
        
        return df
    
    def stock_hk_spot(self):
        """获取港股实时行情，返回格式与akshare完全一致"""
        self._connect()
        hk_data = self.api.get_security_list(3, 0)
        df = pd.DataFrame(hk_data)
        
        # 字段映射
        df = df.rename(columns={
            'code': '代码',
            'name': '名称',
            'price': '最新价',
            'last_close': '昨收',
            'open': '今开',
            'high': '最高',
            'low': '最低',
            'vol': '成交量',
            'amount': '成交额'
        })
        
        df['涨跌幅'] = (df['最新价'] - df['昨收']) / df['昨收'] * 100
        return df
    
    def __del__(self):
        """断开连接"""
        try:
            if self.api.is_connected():
                self.api.disconnect()
        except:
            pass

# 对外暴露和akshare一致的接口
_ths = THSQuote()
stock_zh_a_spot = _ths.stock_zh_a_spot
stock_hk_spot = _ths.stock_hk_spot
