"""DSL v4.5.9 — Broker factory: create broker instances by name."""

from typing import Dict, Optional
from execution_engine.brokers.base import BaseBroker


# 已注册的券商
_REGISTRY: Dict[str, type] = {}


def register_broker(name: str, broker_cls: type):
    """注册券商实现"""
    _REGISTRY[name] = broker_cls


def list_brokers() -> list:
    """列出所有可用券商"""
    return [
        {"name": name, "available": True}
        for name in _REGISTRY
    ]


def get_broker(name: str = "paper", **kwargs) -> Optional[BaseBroker]:
    """获取券商实例

    Args:
        name: 券商名称 ('paper', 'xtquant', 'easytrader')
        **kwargs: 传给券商构造函数的参数

    Returns:
        BaseBroker 实例或 None
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        # 延迟导入
        if name == "paper":
            from execution_engine.brokers.paper_broker import PaperBroker
            cls = PaperBroker
        elif name == "xtquant":
            try:
                from execution_engine.brokers.xtquant_broker import XtQuantBroker
                cls = XtQuantBroker
            except ImportError:
                raise ImportError("xtquant not installed. pip install xtquant")
        elif name == "easytrader":
            try:
                from execution_engine.brokers.easytrader_broker import EasyTraderBroker
                cls = EasyTraderBroker
            except ImportError:
                raise ImportError("easytrader not installed. pip install easytrader")
        else:
            raise ValueError(f"Unknown broker: {name}. Available: {list(_REGISTRY.keys())}")

        _REGISTRY[name] = cls

    broker = cls(**kwargs)
    return broker


# ── 预注册内置券商 ──
register_broker("paper", None)  # lazy import in get_broker()
