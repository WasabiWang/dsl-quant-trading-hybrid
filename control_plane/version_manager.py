#!/usr/bin/env python3
"""
版本管理系统 - 管理策略、模型、规则的版本，支持上线/回滚
当前系统版本: v4.5.1
"""
import json
import os
import shutil
from typing import Dict, List, Optional, Any
from datetime import datetime
from common.logger import get_logger
from common.id_generator import IDGenerator

logger = get_logger("version_manager")

class VersionManager:
    """版本管理器"""
    
    def __init__(self, version_dir: str = None):
        if version_dir is None:
            version_dir = os.path.expanduser("~/.openclaw/versions")
        self.version_dir = version_dir
        self.strategies_dir = os.path.join(version_dir, "strategies")
        self.models_dir = os.path.join(version_dir, "models")
        self.rules_dir = os.path.join(version_dir, "rules")
        self.releases_dir = os.path.join(version_dir, "releases")
        
        # 确保目录存在
        for dir_path in [self.strategies_dir, self.models_dir, self.rules_dir, self.releases_dir]:
            os.makedirs(dir_path, exist_ok=True)
    
    def save_version(self, 
                     version_type: str,  # strategies, models, rules
                     name: str, 
                     version: str,
                     content: Any,
                     metadata: Dict = None) -> str:
        """保存一个版本"""
        # 生成版本ID
        version_id = IDGenerator.generate_short_id(f"VER_{version_type.upper()}_")
        
        # 确定保存路径
        if version_type == "strategies":
            save_dir = self.strategies_dir
        elif version_type == "models":
            save_dir = self.models_dir
        elif version_type == "rules":
            save_dir = self.rules_dir
        else:
            raise ValueError(f"不支持的版本类型: {version_type}")
        
        # 创建版本目录
        version_path = os.path.join(save_dir, f"{name}_{version}_{version_id}")
        os.makedirs(version_path, exist_ok=True)
        
        # 保存内容
        content_file = os.path.join(version_path, "content.json")
        with open(content_file, 'w', encoding='utf-8') as f:
            if isinstance(content, (dict, list)):
                json.dump(content, f, ensure_ascii=False, indent=2)
            else:
                f.write(str(content))
        
        # 保存元数据
        meta = {
            "version_id": version_id,
            "name": name,
            "version": version,
            "type": version_type,
            "created_at": datetime.now().isoformat(),
            "metadata": metadata or {}
        }
        meta_file = os.path.join(version_path, "metadata.json")
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        
        logger.info(f"已保存{version_type}版本: {name} v{version} [{version_id}]")
        return version_id
    
    def get_version(self, 
                    version_type: str,
                    name: str,
                    version: str = None) -> Optional[Dict]:
        """获取指定版本"""
        if version_type == "strategies":
            search_dir = self.strategies_dir
        elif version_type == "models":
            search_dir = self.models_dir
        elif version_type == "rules":
            search_dir = self.rules_dir
        else:
            raise ValueError(f"不支持的版本类型: {version_type}")
        
        # 查找匹配的版本
        if not os.path.exists(search_dir):
            return None
            
        for item in os.listdir(search_dir):
            item_path = os.path.join(search_dir, item)
            if not os.path.isdir(item_path):
                continue
                
            # 检查是否匹配名称和版本
            if name in item:
                if version is None or version in item:
                    # 读取元数据
                    meta_file = os.path.join(item_path, "metadata.json")
                    if os.path.exists(meta_file):
                        with open(meta_file, 'r') as f:
                            return json.load(f)
        
        return None
    
    def list_versions(self, 
                      version_type: str,
                      name: str = None) -> List[Dict]:
        """列出所有版本"""
        if version_type == "strategies":
            search_dir = self.strategies_dir
        elif version_type == "models":
            search_dir = self.models_dir
        elif version_type == "rules":
            search_dir = self.rules_dir
        else:
            raise ValueError(f"不支持的版本类型: {version_type}")
        
        versions = []
        if not os.path.exists(search_dir):
            return versions
            
        for item in os.listdir(search_dir):
            item_path = os.path.join(search_dir, item)
            if not os.path.isdir(item_path):
                continue
                
            # 如果指定了名称，检查是否匹标配
            if name and name not in item:
                continue
                
            # 读取元数据
            meta_file = os.path.join(item_path, "metadata.json")
            if os.path.exists(meta_file):
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                        versions.append(meta)
                except:
                    pass
        
        # 按创建时间倒序排序
        versions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return versions
    
    def promote_to_release(self,
                          version_type: str,
                          name: str,
                          version: str,
                          release_name: str = None) -> str:
        """将版本提交到发布环境"""
        # 获取版本信息
        version_info = self.get_version(version_type, name, version)
        if not version_info:
            raise ValueError(f"未找到版本: {version_type}/{name} v{version}")
        
        version_id = version_info["version_id"]
        
        # 确定源路径
        if version_type == "strategies":
            source_dir = os.path.join(self.strategies_dir, f"{name}_{version}_{version_id}")
        elif version_type == "models":
            source_dir = os.path.join(self.models_dir, f"{name}_{version}_{version_id}")
        elif version_type == "rules":
            source_dir = os.path.join(self.rules_dir, f"{name}_{version}_{version_id}")
        else:
            raise ValueError(f"不支持的版本类型: {version_type}")
        
        # 生成发布名称
        if release_name is None:
            release_name = f"{name}_{version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        release_path = os.path.join(self.releases_dir, release_name)
        
        # 复制到发布目录
        if os.path.exists(release_path):
            shutil.rmtree(release_path)
        shutil.copytree(source_dir, release_path)
        
        # 保存发布信息
        release_info = {
            "release_name": release_name,
            "version_type": version_type,
            "name": name,
            "version": version,
            "version_id": version_id,
            "promoted_at": datetime.now().isoformat(),
            "source_path": source_dir
        }
        release_meta = os.path.join(release_path, "release_info.json")
        with open(release_meta, 'w', encoding='utf-8') as f:
            json.dump(release_info, f, ensure_ascii=False, indent=2)
        
        logger.info(f"已提交到发布: {release_name}")
        return release_name
    
    def rollback(self, 
                 version_type: str,
                 release_name: str) -> bool:
        """从发布环境回滚到指定版本"""
        release_path = os.path.join(self.releases_dir, release_name)
        if not os.path.exists(release_path):
            logger.error(f"发布不存在: {release_name}")
            return False
        
        # 读取发布信息
        release_info_path = os.path.join(release_path, "release_info.json")
        if not os.path.exists(release_info_path):
            logger.error(f"发布信息不存在: {release_name}")
            return False
            
        with open(release_info_path, 'r') as f:
            release_info = json.load(f)
        
        # TODO: 实现实际的回滚逻辑（这里需要根据具体系统来实现）
        logger.info(f"准备回滚到发布: {release_name}")
        logger.info(f"  版本类型: {release_info['version_type']}")
        logger.info(f"  名称: {release_info['name']}")
        logger.info(f"  版本: {release_info['version']}")
        
        return True

# 全局实例
version_manager = VersionManager()