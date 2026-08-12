#!/usr/bin/env python3
"""
log_to_bitable.py - Bitable归档模块
将反馈日志归档到飞书多维表格

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import json
import pandas as pd
from datetime import datetime
from typing import Dict, List, Any, Optional

class BitableArchiver:
    """Bitable归档器"""
    
    def __init__(self, app_token: str = None, table_id: str = None):
        """
        初始化Bitable归档器
        
        Args:
            app_token: Bitable应用token
            table_id: 表格ID
        """
        self.app_token = app_token or os.getenv("FEISHU_BITABLE_TOKEN")
        self.table_id = table_id or os.getenv("FEISHU_BITABLE_TABLE_ID")
        
        # 默认的反馈记录表结构
        self.default_table_structure = {
            "fields": [
                {"field_name": "记录ID", "field_type": 1},  # 文本
                {"field_name": "时间戳", "field_type": 5},  # 日期时间
                {"field_name": "策略类型", "field_type": 1},
                {"field_name": "标的代码", "field_type": 1},
                {"field_name": "建议动作", "field_type": 1},
                {"field_name": "置信度", "field_type": 2},  # 数字
                {"field_name": "验证分数", "field_type": 2},
                {"field_name": "置信等级", "field_type": 1},
                {"field_name": "实际结果", "field_type": 1},
                {"field_name": "是否正确", "field_type": 7},  # 复选框
                {"field_name": "备注", "field_type": 1}
            ]
        }
    
    def _call_feishu_api(self, endpoint: str, method: str = "GET", data: Dict = None) -> Optional[Dict]:
        """
        调用飞书API
        
        Args:
            endpoint: API端点
            method: HTTP方法
            data: 请求数据
            
        Returns:
            API响应
        """
        # 这里应该实现实际的飞书API调用
        # 由于需要真实的飞书权限，这里使用模拟实现
        
        print(f"📤 调用飞书API: {method} {endpoint}")
        
        if data:
            print(f"  请求数据: {json.dumps(data, ensure_ascii=False)[:200]}...")
        
        # 模拟成功响应
        return {
            "code": 0,
            "msg": "success",
            "data": {
                "record_id": f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                "updated_at": datetime.now().isoformat()
            }
        }
    
    def create_feedback_table(self, table_name: str = "策略反馈记录") -> Optional[str]:
        """
        创建反馈记录表
        
        Args:
            table_name: 表格名称
            
        Returns:
            表格ID
        """
        if not self.app_token:
            print("❌ 缺少Bitable应用token")
            return None
        
        endpoint = f"/open-apis/bitable/v1/apps/{self.app_token}/tables"
        
        data = {
            "table": {
                "name": table_name,
                "fields": self.default_table_structure["fields"]
            }
        }
        
        response = self._call_feishu_api(endpoint, "POST", data)
        
        if response and response.get("code") == 0:
            table_id = response.get("data", {}).get("table_id")
            print(f"✅ 创建反馈表成功: {table_id}")
            return table_id
        else:
            print(f"❌ 创建反馈表失败: {response}")
            return None
    
    def archive_feedback_entry(self, feedback_entry: Dict) -> Optional[str]:
        """
        归档反馈条目到Bitable
        
        Args:
            feedback_entry: 反馈条目
            
        Returns:
            记录ID
        """
        if not self.app_token or not self.table_id:
            print("❌ 缺少Bitable配置")
            return None
        
        # 准备Bitable记录数据
        record_data = self._prepare_record_data(feedback_entry)
        
        endpoint = f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records"
        
        data = {
            "record": {
                "fields": record_data
            }
        }
        
        response = self._call_feishu_api(endpoint, "POST", data)
        
        if response and response.get("code") == 0:
            record_id = response.get("data", {}).get("record_id")
            print(f"✅ 归档反馈条目成功: {record_id}")
            return record_id
        else:
            print(f"❌ 归档反馈条目失败: {response}")
            return None
    
    def _prepare_record_data(self, feedback_entry: Dict) -> Dict:
        """
        准备Bitable记录数据
        
        Args:
            feedback_entry: 反馈条目
            
        Returns:
            Bitable记录字段数据
        """
        suggestion = feedback_entry.get("data", {})
        validation = feedback_entry.get("validation_result", {})
        actual = feedback_entry.get("actual_result", {})
        
        record_data = {
            "记录ID": feedback_entry.get("id", ""),
            "时间戳": feedback_entry.get("timestamp", ""),
            "策略类型": suggestion.get("strategy", ""),
            "标的代码": suggestion.get("symbol", ""),
            "建议动作": suggestion.get("action", ""),
            "置信度": suggestion.get("confidence", 0),
            "验证分数": validation.get("score", 0),
            "置信等级": validation.get("confidence_level", ""),
            "实际结果": actual.get("actual_action", ""),
            "是否正确": actual.get("was_correct", False),
            "备注": suggestion.get("reason", "")
        }
        
        return record_data
    
    def batch_archive_feedback(self, feedback_entries: List[Dict]) -> Dict:
        """
        批量归档反馈条目
        
        Args:
            feedback_entries: 反馈条目列表
            
        Returns:
            批量归档结果
        """
        results = {
            "total": len(feedback_entries),
            "success": 0,
            "failed": 0,
            "record_ids": []
        }
        
        for entry in feedback_entries:
            record_id = self.archive_feedback_entry(entry)
            if record_id:
                results["success"] += 1
                results["record_ids"].append(record_id)
            else:
                results["failed"] += 1
        
        print(f"📊 批量归档完成: 成功{results['success']}/失败{results['failed']}")
        return results
    
    def sync_feedback_log(self, feedback_log_file: str) -> Dict:
        """
        同步反馈日志到Bitable
        
        Args:
            feedback_log_file: 反馈日志文件路径
            
        Returns:
            同步结果
        """
        if not os.path.exists(feedback_log_file):
            print(f"❌ 反馈日志文件不存在: {feedback_log_file}")
            return {"error": "文件不存在"}
        
        try:
            with open(feedback_log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            
            entries = log_data.get("entries", [])
            
            if not entries:
                print("ℹ️ 反馈日志为空，无需同步")
                return {"total": 0, "success": 0, "failed": 0}
            
            print(f"📋 开始同步{len(entries)}条反馈记录...")
            
            # 只同步未归档的条目（没有archive_timestamp）
            entries_to_sync = []
            for entry in entries:
                if "archive_timestamp" not in entry:
                    entries_to_sync.append(entry)
            
            if not entries_to_sync:
                print("ℹ️ 所有条目已归档，无需同步")
                return {"total": 0, "success": 0, "failed": 0}
            
            print(f"📤 需要同步{len(entries_to_sync)}条新记录")
            
            # 批量归档
            results = self.batch_archive_feedback(entries_to_sync)
            
            # 更新日志文件中的归档时间戳
            if results["success"] > 0:
                self._update_archive_timestamps(feedback_log_file, entries_to_sync, results["record_ids"])
            
            return results
            
        except Exception as e:
            print(f"❌ 同步反馈日志失败: {e}")
            return {"error": str(e)}
    
    def _update_archive_timestamps(self, log_file: str, entries: List[Dict], record_ids: List[str]):
        """更新日志文件中的归档时间戳"""
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                log_data = json.load(f)
            
            # 创建记录ID到条目的映射
            entry_map = {entry["id"]: entry for entry in entries}
            
            # 更新归档时间戳
            for i, entry in enumerate(log_data.get("entries", [])):
                if entry["id"] in entry_map and i < len(record_ids):
                    entry["archive_timestamp"] = datetime.now().isoformat()
                    entry["archive_record_id"] = record_ids[i]
            
            # 保存更新后的日志
            with open(log_file, 'w', encoding='utf-8') as f:
                json.dump(log_data, f, indent=2, ensure_ascii=False)
            
            print(f"💾 更新{len(record_ids)}条记录的归档时间戳")
            
        except Exception as e:
            print(f"⚠️ 更新归档时间戳失败: {e}")

def test_bitable_archiver():
    """测试Bitable归档器"""
    print("=" * 60)
    print("🧪 Bitable归档器测试")
    print("=" * 60)
    
    # 使用模拟配置
    archiver = BitableArchiver(
        app_token=os.getenv("FEISHU_TEST_APP_TOKEN", "test_app_token"),  # P1-7: 测试token从环境变量读取
        table_id="test_table_id"
    )
    
    # 测试创建表格
    table_id = archiver.create_feedback_table("测试反馈表")
    print(f"✅ 创建表格: {table_id}")
    
    # 测试归档反馈条目
    feedback_entry = {
        "id": "entry_20260419_143000_001",
        "timestamp": "2026-04-19T14:30:00",
        "type": "strategy_suggestion",
        "data": {
            "strategy": "MA交叉策略",
            "symbol": "600760",
            "action": "buy",
            "confidence": 0.75,
            "reason": "MA5上穿MA20"
        },
        "validation_result": {
            "valid": True,
            "score": 0.82,
            "confidence_level": "high"
        },
        "actual_result": {
            "actual_action": "buy",
            "was_correct": True
        }
    }
    
    record_id = archiver.archive_feedback_entry(feedback_entry)
    print(f"✅ 归档条目: {record_id}")
    
    # 测试批量归档
    entries = [feedback_entry, feedback_entry]  # 两个相同条目用于测试
    results = archiver.batch_archive_feedback(entries)
    print(f"✅ 批量归档结果: 成功{results['success']}/失败{results['failed']}")
    
    # 创建测试日志文件
    test_log_file = "test_feedback_log.json"
    test_log_data = {
        "version": "1.0",
        "created_at": datetime.now().isoformat(),
        "entries": entries
    }
    
    with open(test_log_file, 'w', encoding='utf-8') as f:
        json.dump(test_log_data, f, indent=2, ensure_ascii=False)
    
    # 测试同步日志
    sync_results = archiver.sync_feedback_log(test_log_file)
    print(f"✅ 同步日志结果: {sync_results}")
    
    # 清理测试文件
    if os.path.exists(test_log_file):
        os.remove(test_log_file)
    
    print("\n" + "=" * 60)
    print("✅ Bitable归档器测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_bitable_archiver()