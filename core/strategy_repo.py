#!/usr/bin/env python3
"""
strategy_repo.py - 策略市场/仓库
策略注册、评分、版本管理、自动回测排名

功能：
1. 策略注册与版本管理
2. 策略评分与排名
3. 自动回测与性能评估
4. 策略发现与推荐

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import json
import hashlib
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

class StrategyRepository:
    """策略仓库"""
    
    def __init__(self, repo_dir: str = "strategies/repo"):
        """
        初始化策略仓库
        
        Args:
            repo_dir: 仓库目录
        """
        self.repo_dir = Path(repo_dir)
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        
        # 子目录
        self.strategies_dir = self.repo_dir / "strategies"
        self.backtest_dir = self.repo_dir / "backtests"
        self.metadata_dir = self.repo_dir / "metadata"
        
        for dir_path in [self.strategies_dir, self.backtest_dir, self.metadata_dir]:
            dir_path.mkdir(exist_ok=True)
        
        # 加载元数据
        self.metadata = self._load_metadata()
    
    def _load_metadata(self) -> Dict[str, Any]:
        """加载仓库元数据"""
        metadata_file = self.metadata_dir / "repo_metadata.json"
        if metadata_file.exists():
            with open(metadata_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            return {
                "version": "1.0.0",
                "created_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat(),
                "total_strategies": 0,
                "strategies": {}
            }
    
    def _save_metadata(self):
        """保存仓库元数据"""
        metadata_file = self.metadata_dir / "repo_metadata.json"
        self.metadata["last_updated"] = datetime.now().isoformat()
        with open(metadata_file, 'w', encoding='utf-8') as f:
            json.dump(self.metadata, f, ensure_ascii=False, indent=2)
    
    def register_strategy(self, strategy_path: str, strategy_data: Dict[str, Any] = None) -> str:
        """
        注册策略到仓库
        
        Args:
            strategy_path: 策略文件路径
            strategy_data: 策略数据（如果为None则从文件读取）
            
        Returns:
            策略ID
        """
        # 加载策略数据
        if strategy_data is None:
            with open(strategy_path, 'r', encoding='utf-8') as f:
                strategy_data = json.load(f)
        
        # 生成策略ID
        strategy_name = strategy_data.get("name", "unnamed")
        strategy_version = strategy_data.get("version", "1.0.0")
        strategy_hash = self._calculate_strategy_hash(strategy_data)
        strategy_id = f"{strategy_name}_{strategy_version}_{strategy_hash[:8]}"
        
        # 检查是否已存在
        if strategy_id in self.metadata["strategies"]:
            print(f"⚠️ 策略已存在: {strategy_id}")
            return strategy_id
        
        # 保存策略文件
        strategy_file = self.strategies_dir / f"{strategy_id}.json"
        with open(strategy_file, 'w', encoding='utf-8') as f:
            json.dump(strategy_data, f, ensure_ascii=False, indent=2)
        
        # 更新元数据
        strategy_info = {
            "id": strategy_id,
            "name": strategy_name,
            "version": strategy_version,
            "author": strategy_data.get("author", "unknown"),
            "description": strategy_data.get("description", ""),
            "created_at": strategy_data.get("created_at", datetime.now().isoformat()),
            "registered_at": datetime.now().isoformat(),
            "file_path": str(strategy_file),
            "hash": strategy_hash,
            "status": "registered",  # registered, testing, active, deprecated
            "performance": {},
            "backtest_results": [],
            "tags": self._extract_tags(strategy_data),
            "parameters": list(strategy_data.get("parameters", {}).keys()),
            "signals": len(strategy_data.get("signals", [])),
            "downloads": 0,
            "rating": 0.0,
            "reviews": []
        }
        
        self.metadata["strategies"][strategy_id] = strategy_info
        self.metadata["total_strategies"] = len(self.metadata["strategies"])
        self._save_metadata()
        
        print(f"✅ 策略注册成功: {strategy_id}")
        return strategy_id
    
    def _calculate_strategy_hash(self, strategy_data: Dict[str, Any]) -> str:
        """计算策略哈希值"""
        # 移除可变字段
        strategy_copy = strategy_data.copy()
        strategy_copy.pop("created_at", None)
        strategy_copy.pop("author", None)
        
        # 转换为JSON字符串并计算哈希
        strategy_json = json.dumps(strategy_copy, sort_keys=True)
        return hashlib.md5(strategy_json.encode()).hexdigest()
    
    def _extract_tags(self, strategy_data: Dict[str, Any]) -> List[str]:
        """从策略中提取标签"""
        tags = []
        description = strategy_data.get("description", "").lower()
        name = strategy_data.get("name", "").lower()
        
        # 策略类型标签
        strategy_types = ["trend", "mean_reversion", "breakout", "momentum", "arbitrage", "hedging"]
        for stype in strategy_types:
            if stype in description or stype in name:
                tags.append(stype)
        
        # 指标标签
        indicators = ["ma", "ema", "macd", "rsi", "bollinger", "stochastic", "atr", "volume"]
        for indicator in indicators:
            if indicator in description:
                tags.append(indicator)
        
        # 市场标签
        markets = ["stock", "future", "forex", "crypto", "a_share", "hk", "us"]
        for market in markets:
            if market in description:
                tags.append(market)
        
        # 时间框架标签
        timeframes = ["intraday", "daily", "weekly", "monthly", "swing", "position"]
        for tf in timeframes:
            if tf in description:
                tags.append(tf)
        
        return list(set(tags))  # 去重
    
    def get_strategy(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """获取策略信息"""
        if strategy_id in self.metadata["strategies"]:
            return self.metadata["strategies"][strategy_id]
        return None
    
    def get_strategy_file(self, strategy_id: str) -> Optional[Path]:
        """获取策略文件路径"""
        strategy_info = self.get_strategy(strategy_id)
        if strategy_info and "file_path" in strategy_info:
            return Path(strategy_info["file_path"])
        return None
    
    def list_strategies(self, 
                       status: str = None, 
                       tags: List[str] = None,
                       min_rating: float = 0.0,
                       limit: int = 100) -> List[Dict[str, Any]]:
        """
        列出策略
        
        Args:
            status: 状态过滤
            tags: 标签过滤
            min_rating: 最低评分
            limit: 返回数量限制
            
        Returns:
            策略列表
        """
        strategies = []
        
        for strategy_id, strategy_info in self.metadata["strategies"].items():
            # 状态过滤
            if status and strategy_info.get("status") != status:
                continue
            
            # 评分过滤
            if strategy_info.get("rating", 0.0) < min_rating:
                continue
            
            # 标签过滤
            if tags:
                strategy_tags = strategy_info.get("tags", [])
                if not any(tag in strategy_tags for tag in tags):
                    continue
            
            strategies.append(strategy_info)
        
        # 按评分排序
        strategies.sort(key=lambda x: x.get("rating", 0.0), reverse=True)
        
        return strategies[:limit]
    
    def search_strategies(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """搜索策略"""
        results = []
        query_lower = query.lower()
        
        for strategy_id, strategy_info in self.metadata["strategies"].items():
            score = 0
            
            # 名称匹配
            if query_lower in strategy_info.get("name", "").lower():
                score += 10
            
            # 描述匹配
            if query_lower in strategy_info.get("description", "").lower():
                score += 5
            
            # 作者匹配
            if query_lower in strategy_info.get("author", "").lower():
                score += 3
            
            # 标签匹配
            for tag in strategy_info.get("tags", []):
                if query_lower in tag.lower():
                    score += 2
            
            if score > 0:
                strategy_info["search_score"] = score
                results.append(strategy_info)
        
        # 按搜索分数排序
        results.sort(key=lambda x: x.get("search_score", 0), reverse=True)
        
        return results[:limit]
    
    def update_strategy_performance(self, strategy_id: str, backtest_result: Dict[str, Any]):
        """
        更新策略性能数据
        
        Args:
            strategy_id: 策略ID
            backtest_result: 回测结果
        """
        if strategy_id not in self.metadata["strategies"]:
            print(f"❌ 策略不存在: {strategy_id}")
            return
        
        strategy_info = self.metadata["strategies"][strategy_id]
        
        # 保存回测结果
        backtest_id = f"backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        backtest_file = self.backtest_dir / f"{backtest_id}.json"
        
        backtest_data = {
            "id": backtest_id,
            "strategy_id": strategy_id,
            "timestamp": datetime.now().isoformat(),
            "result": backtest_result
        }
        
        with open(backtest_file, 'w', encoding='utf-8') as f:
            json.dump(backtest_data, f, ensure_ascii=False, indent=2)
        
        # 更新策略信息
        if "backtest_results" not in strategy_info:
            strategy_info["backtest_results"] = []
        
        strategy_info["backtest_results"].append(backtest_id)
        
        # 计算性能指标
        performance = self._calculate_performance_metrics(backtest_result)
        strategy_info["performance"] = performance
        
        # 更新评分
        strategy_info["rating"] = self._calculate_strategy_rating(performance)
        
        # 更新状态
        if strategy_info["status"] == "registered":
            strategy_info["status"] = "testing"
        
        self._save_metadata()
        print(f"✅ 策略性能更新: {strategy_id}, 评分: {strategy_info['rating']:.2f}")
    
    def _calculate_performance_metrics(self, backtest_result: Dict[str, Any]) -> Dict[str, Any]:
        """计算性能指标"""
        metrics = {
            "total_return": backtest_result.get("total_return", 0.0),
            "annual_return": backtest_result.get("annual_return", 0.0),
            "sharpe_ratio": backtest_result.get("sharpe_ratio", 0.0),
            "max_drawdown": backtest_result.get("max_drawdown", 0.0),
            "win_rate": backtest_result.get("win_rate", 0.0),
            "profit_factor": backtest_result.get("profit_factor", 0.0),
            "total_trades": backtest_result.get("total_trades", 0),
            "avg_trade": backtest_result.get("avg_trade", 0.0),
            "calmar_ratio": backtest_result.get("calmar_ratio", 0.0),
            "sortino_ratio": backtest_result.get("sortino_ratio", 0.0)
        }
        return metrics
    
    def _calculate_strategy_rating(self, performance: Dict[str, Any]) -> float:
        """计算策略评分"""
        rating = 0.0
        
        # 总收益率权重
        total_return = performance.get("total_return", 0.0)
        if total_return > 0:
            rating += min(total_return * 2, 20)  # 最多20分
        
        # 夏普比率权重
        sharpe = performance.get("sharpe_ratio", 0.0)
        if sharpe > 0:
            rating += min(sharpe * 5, 20)  # 最多20分
        
        # 最大回撤权重（负向）
        max_dd = abs(performance.get("max_drawdown", 0.0))
        if max_dd > 0:
            rating -= min(max_dd * 50, 20)  # 最多扣20分
        
        # 胜率权重
        win_rate = performance.get("win_rate", 0.0)
        if win_rate > 0:
            rating += min(win_rate * 20, 20)  # 最多20分
        
        # 交易次数权重（适度交易）
        total_trades = performance.get("total_trades", 0)
        if 10 <= total_trades <= 100:
            rating += 10
        elif total_trades > 100:
            rating += 5
        
        # 确保评分在0-100之间
        rating = max(0, min(rating, 100))
        
        return rating
    
    def add_strategy_review(self, strategy_id: str, review: Dict[str, Any]):
        """
        添加策略评价
        
        Args:
            strategy_id: 策略ID
            review: 评价内容
        """
        if strategy_id not in self.metadata["strategies"]:
            print(f"❌ 策略不存在: {strategy_id}")
            return
        
        strategy_info = self.metadata["strategies"][strategy_id]
        
        # 添加评价
        review["timestamp"] = datetime.now().isoformat()
        review["review_id"] = f"review_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        if "reviews" not in strategy_info:
            strategy_info["reviews"] = []
        
        strategy_info["reviews"].append(review)
        
        # 更新下载次数
        if review.get("action") == "download":
            strategy_info["downloads"] = strategy_info.get("downloads", 0) + 1
        
        self._save_metadata()
        print(f"✅ 评价已添加: {strategy_id}")
    
    def get_top_strategies(self, n: int = 10, 
                          min_trades: int = 5,
                          min_days: int = 30) -> List[Dict[str, Any]]:
        """
        获取顶级策略
        
        Args:
            n: 返回数量
            min_trades: 最小交易次数
            min_days: 最小测试天数
            
        Returns:
            顶级策略列表
        """
        strategies = []
        
        for strategy_id, strategy_info in self.metadata["strategies"].items():
            # 过滤条件
            if strategy_info.get("status") != "active":
                continue
            
            performance = strategy_info.get("performance", {})
            if performance.get("total_trades", 0) < min_trades:
                continue
            backtest_days = performance.get("days", performance.get("backtest_days", min_days))
            if backtest_days < min_days:
                continue
            
            # 这里可以添加更多过滤条件
            
            strategies.append(strategy_info)
        
        # 按评分排序
        strategies.sort(key=lambda x: x.get("rating", 0.0), reverse=True)
        
        return strategies[:n]
    
    def generate_repo_report(self) -> str:
        """生成仓库报告"""
        report = []
        report.append("=" * 60)
        report.append("📊 策略仓库报告")
        report.append("=" * 60)
        report.append(f"仓库版本: {self.metadata.get('version', 'N/A')}")
        report.append(f"创建时间: {self.metadata.get('created_at', 'N/A')}")
        report.append(f"最后更新: {self.metadata.get('last_updated', 'N/A')}")
        report.append(f"策略总数: {self.metadata.get('total_strategies', 0)}")
        
        # 状态统计
        status_count = {}
        for strategy_info in self.metadata["strategies"].values():
            status = strategy_info.get("status", "unknown")
            status_count[status] = status_count.get(status, 0) + 1
        
        report.append("策略状态统计:")
        for status, count in sorted(status_count.items()):
            report.append(f"  • {status}: {count}")
        
        # 评分分布
        ratings = [s.get("rating", 0.0) for s in self.metadata["strategies"].values()]
        if ratings:
            avg_rating = sum(ratings) / len(ratings)
            max_rating = max(ratings)
            min_rating = min(ratings)
            report.append(f"平均评分: {avg_rating:.2f}")
            report.append(f"最高评分: {max_rating:.2f}")
            report.append(f"最低评分: {min_rating:.2f}")
        
        # 热门标签
        all_tags = []
        for strategy_info in self.metadata["strategies"].values():
            all_tags.extend(strategy_info.get("tags", []))
        
        from collections import Counter
        tag_counter = Counter(all_tags)
        top_tags = tag_counter.most_common(10)
        
        report.append("热门标签:")
        for tag, count in top_tags:
            report.append(f"  • {tag}: {count}")
        
        # 顶级策略
        top_strategies = self.get_top_strategies(5)
        report.append("顶级策略:")
        for i, strategy in enumerate(top_strategies, 1):
            name = strategy.get("name", "N/A")
            rating = strategy.get("rating", 0.0)
            status = strategy.get("status", "N/A")
            report.append(f"  {i}. {name} (评分: {rating:.2f}, 状态: {status})")
        
        report.append("=" * 60)
        return "\n".join(report)
    
    def export_strategy(self, strategy_id: str, export_dir: str = "exports") -> Optional[str]:
        """
        导出策略
        
        Args:
            strategy_id: 策略ID
            export_dir: 导出目录
            
        Returns:
            导出文件路径
        """
        strategy_info = self.get_strategy(strategy_id)
        if not strategy_info:
            print(f"❌ 策略不存在: {strategy_id}")
            return None
        
        # 创建导出目录
        export_path = Path(export_dir)
        export_path.mkdir(exist_ok=True)
        
        # 加载策略数据
        strategy_file = self.get_strategy_file(strategy_id)
        if not strategy_file or not strategy_file.exists():
            print(f"❌ 策略文件不存在: {strategy_id}")
            return None
        
        with open(strategy_file, 'r', encoding='utf-8') as f:
            strategy_data = json.load(f)
        
        # 添加仓库信息
        export_data = {
            "strategy": strategy_data,
            "repository_info": {
                "strategy_id": strategy_id,
                "registered_at": strategy_info.get("registered_at"),
                "rating": strategy_info.get("rating", 0.0),
                "performance": strategy_info.get("performance", {}),
                "tags": strategy_info.get("tags", []),
                "downloads": strategy_info.get("downloads", 0)
            },
            "export_info": {
                "exported_at": datetime.now().isoformat(),
                "repository_version": self.metadata.get("version", "1.0.0")
            }
        }
        
        # 生成导出文件名
        export_filename = f"{strategy_id}_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        export_filepath = export_path / export_filename
        
        # 保存导出文件
        with open(export_filepath, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2)
        
        # 增加下载次数
        self.add_strategy_review(strategy_id, {
            "action": "download",
            "user": "export",
            "comment": "策略导出"
        })
        
        print(f"✅ 策略已导出: {export_filepath}")
        return str(export_filepath)


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='策略仓库管理')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # 注册策略
    register_parser = subparsers.add_parser('register', help='注册策略')
    register_parser.add_argument('strategy_file', type=str, help='策略文件路径')
    
    # 列出策略
    list_parser = subparsers.add_parser('list', help='列出策略')
    list_parser.add_argument('--status', type=str, help='状态过滤')
    list_parser.add_argument('--tags', type=str, help='标签过滤（逗号分隔）')
    list_parser.add_argument('--min-rating', type=float, default=0.0, help='最低评分')
    list_parser.add_argument('--limit', type=int, default=20, help='返回数量')
    
    # 搜索策略
    search_parser = subparsers.add_parser('search', help='搜索策略')
    search_parser.add_argument('query', type=str, help='搜索查询')
    search_parser.add_argument('--limit', type=int, default=20, help='返回数量')
    
    # 获取策略
    get_parser = subparsers.add_parser('get', help='获取策略信息')
    get_parser.add_argument('strategy_id', type=str, help='策略ID')
    
    # 导出策略
    export_parser = subparsers.add_parser('export', help='导出策略')
    export_parser.add_argument('strategy_id', type=str, help='策略ID')
    export_parser.add_argument('--output', type=str, default='exports', help='输出目录')
    
    # 生成报告
    report_parser = subparsers.add_parser('report', help='生成仓库报告')
    
    args = parser.parse_args()
    
    # 创建仓库实例
    repo = StrategyRepository()
    
    if args.command == 'register':
        strategy_id = repo.register_strategy(args.strategy_file)
        print(f"✅ 策略注册成功: {strategy_id}")
    
    elif args.command == 'list':
        tags = args.tags.split(',') if args.tags else None
        strategies = repo.list_strategies(
            status=args.status,
            tags=tags,
            min_rating=args.min_rating,
            limit=args.limit
        )
        
        print(f"📋 找到 {len(strategies)} 个策略:")
        for i, strategy in enumerate(strategies, 1):
            name = strategy.get("name", "N/A")
            rating = strategy.get("rating", 0.0)
            status = strategy.get("status", "N/A")
            tags = ", ".join(strategy.get("tags", []))
            print(f"{i:2d}. {name} (评分: {rating:.2f}, 状态: {status})")
            if tags:
                print(f"    标签: {tags}")
    
    elif args.command == 'search':
        strategies = repo.search_strategies(args.query, args.limit)
        print(f"🔍 搜索 '{args.query}' 找到 {len(strategies)} 个策略:")
        for i, strategy in enumerate(strategies, 1):
            name = strategy.get("name", "N/A")
            rating = strategy.get("rating", 0.0)
            score = strategy.get("search_score", 0)
            print(f"{i:2d}. {name} (评分: {rating:.2f}, 搜索分数: {score})")
    
    elif args.command == 'get':
        strategy_info = repo.get_strategy(args.strategy_id)
        if strategy_info:
            print(json.dumps(strategy_info, ensure_ascii=False, indent=2))
        else:
            print(f"❌ 策略不存在: {args.strategy_id}")
    
    elif args.command == 'export':
        export_path = repo.export_strategy(args.strategy_id, args.output)
        if export_path:
            print(f"✅ 策略已导出到: {export_path}")
    
    elif args.command == 'report':
        report = repo.generate_repo_report()
        print(report)
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
