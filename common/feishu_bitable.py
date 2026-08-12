#!/usr/bin/env python3
"""
飞书多维表格API封装 - 不依赖外部第三方包
"""
import os
import sys
import json
import requests
from typing import List, Dict, Any

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from common.config import config as _config
    config = _config
except ImportError:
    from config import config

class FeishuBitable:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
    
    def _init(self):
        self.app_id = config.feishu_app_id
        self.app_secret = config.feishu_app_secret
        self.base_url = "https://open.feishu.cn/open-apis"
        self._tenant_access_token = None
        self._token_expire_time = 0
    
    def _get_tenant_access_token(self) -> str:
        """获取租户访问令牌，自动缓存和刷新"""
        import time
        now = int(time.time())
        if self._tenant_access_token and now < self._token_expire_time - 60:
            return self._tenant_access_token
        
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                self._tenant_access_token = data["tenant_access_token"]
                self._token_expire_time = now + data["expire"]
                return self._tenant_access_token
            else:
                print(f"❌ 获取飞书Token失败：{data.get('msg')}")
                return ""
        except Exception as e:
            print(f"❌ 获取飞书Token异常：{e}")
            return ""
    
    def list_records(self, app_token: str, table_id: str, page_size: int = 500) -> List[Dict]:
        """列出表格所有记录"""
        token = self._get_tenant_access_token()
        if not token:
            return []
        
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size={page_size}"
        headers = {
            "Authorization": f"Bearer {token}"
        }
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("items", [])
            else:
                print(f"❌ 获取多维表格记录失败：{data.get('msg')}")
                return []
        except Exception as e:
            print(f"❌ 获取多维表格记录异常：{e}")
            return []
    
    def create_record(self, app_token: str, table_id: str, fields: Dict) -> bool:
        """创建单条记录"""
        token = self._get_tenant_access_token()
        if not token:
            return False
        
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "fields": fields
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("code") == 0
        except Exception as e:
            print(f"❌ 创建多维表格记录异常：{e}")
            return False

    def update_record(self, app_token: str, table_id: str, record_id: str, fields: Dict) -> bool:
        """更新单条记录"""
        token = self._get_tenant_access_token()
        if not token:
            return False
        
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "fields": fields
        }
        try:
            resp = requests.put(url, json=payload, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("code") == 0
        except Exception as e:
            print(f"❌ 更新多维表格记录异常：{e}")
            return False

    def batch_create_records(self, app_token: str, table_id: str, records: List[Dict]) -> int:
        """批量创建记录，自动分页（每批最多500条）"""
        token = self._get_tenant_access_token()
        if not token:
            return 0
        
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        created = 0
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i:i+batch_size]
            payload = {"records": [{"fields": r} for r in batch]}
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0:
                    created += len(batch)
            except Exception as e:
                print(f"❌ 批量创建多维表格记录异常：{e}")
        return created

    def batch_delete_records(self, app_token: str, table_id: str, record_ids: List[str]) -> int:
        """批量删除记录，自动分页"""
        token = self._get_tenant_access_token()
        if not token:
            return 0
        
        url = f"{self.base_url}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        deleted = 0
        batch_size = 500
        for i in range(0, len(record_ids), batch_size):
            batch = record_ids[i:i+batch_size]
            payload = {"records": batch}
            try:
                resp = requests.delete(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 0:
                    deleted += len(batch)
            except Exception as e:
                print(f"❌ 批量删除多维表格记录异常：{e}")
        return deleted

    def get_record_id_by_field(self, app_token: str, table_id: str, field_name: str, field_value: str) -> str:
        """按字段值查找第一条匹配的record_id"""
        records = self.list_records(app_token, table_id)
        for r in records:
            f = r.get("fields", {})
            if str(f.get(field_name, "")) == str(field_value):
                return r.get("record_id", "")
        return ""

# 全局实例
feishu_bitable = FeishuBitable()
