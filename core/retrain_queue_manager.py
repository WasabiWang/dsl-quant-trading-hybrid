#!/usr/bin/env python3
"""
core/retrain_queue_manager.py — DSL v4.5.9 重训队列统一管理器

=== 设计目标 ===
retrain_queue.json 的唯一写入入口。
禁止 calibration_feedback / sync_stock_pool / batch_predict 等其他模块直接读写 retrain_queue.json，
必须通过本管理器操作。消除多源写入导致的条目丢失/重复/格式不一致问题。

=== 数据流 ===
       ┌──────────────────────────┐
       │  calibration_feedback    │
       │  (精度退化检测)           │──→ queue_for_retrain()
       ├──────────────────────────┤
       │  sync_stock_pool         │
       │  (池变更后加入新标的重训)   │──→ queue_for_retrain()
       ├──────────────────────────┤
       │  batch_predict           │
       │  (低精度标的自动加入)      │──→ queue_for_retrain()
       ├──────────────────────────┤
       │  batch_train             │
       │  (消费队列)               │──→ dequeue() / mark_completed()
       └──────────────────────────┘
                     │
                     ▼
         ┌──────────────────────┐
         │  retrain_queue.json  │ ← 唯一写入源
         └──────────────────────┘

=== 队列格式 ===
  [
    {
      "symbol": "000001",
      "name": "平安银行",
      "priority": "critical",   # critical / high / medium / low
      "reason": "精度0.42<45%",
      "accuracies": [...],       # 加入时的精度历史(快照)
      "requested_at": "2026-05-07T06:00:00",
      "status": "queued",        # queued / training / completed / failed
      "trained_at": null,
      "hash": "abc123"           # 去重指纹
    },
    ...
  ]

=== 使用示例 ===
  from core.retrain_queue_manager import RetrainQueueManager
  mgr = RetrainQueueManager()
  mgr.queue_for_retrain("000001", "平安银行", "critical", "精度低于45%")
  mgr.queue_for_retrain("002466", "天齐锂业", "high", "精度持续退化")
  pending = mgr.list_pending()
  mgr.mark_completed("000001")
  mgr.dequeue(batch_size=3)
"""

import os, json, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Tuple
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from common.file_lock import FileLock, Timeout

PROJECT_ROOT = Path(__file__).parent.parent
QUEUE_PATH = PROJECT_ROOT / "data" / "retrain_queue.json"
LOCK_DIR = PROJECT_ROOT / "data" / "locks"
LOCK_TIMEOUT = 15  # seconds


class RetrainQueueManager:
    """重训队列统一管理器（线程安全，文件锁保护）"""

    def __init__(self, queue_path: Optional[Path] = None):
        self.queue_path = queue_path or QUEUE_PATH
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # ──────────── 公共写入 API ────────────

    def queue_for_retrain(self, symbol: str, name: str,
                          priority: str = "medium",
                          reason: str = "",
                          accuracies: Optional[List[float]] = None) -> bool:
        """将一只标的加入重训队列（幂等：同一symbol+priority不会重复添加）

        Args:
            symbol: 股票代码
            name: 股票名称
            priority: critical / high / medium / low
            reason: 加入原因
            accuracies: 精度历史快照

        Returns:
            True=新加入, False=已存在跳过
        """
        queue = self._load_lock()
        fingerprint = self._fingerprint(symbol, priority)

        # 去重：同一symbol+priority且状态为queued/training的不重复添加
        for item in queue:
            if item.get("symbol") == symbol:
                existing_fp = item.get("hash", "")
                existing_status = item.get("status", "queued")
                if existing_status in ("queued", "training"):
                    # 更新原因和priority（如果是更高优先级）
                    prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
                    existing_prio = prio_order.get(item.get("priority", "low"), 9)
                    new_prio = prio_order.get(priority, 9)
                    if new_prio < existing_prio:
                        item["priority"] = priority
                        item["reason"] = reason
                        item["requested_at"] = datetime.now().isoformat()
                    return False  # 已存在

        entry = {
            "symbol": symbol,
            "name": name,
            "priority": priority,
            "reason": reason,
            "accuracies": accuracies or [],
            "requested_at": datetime.now().isoformat(),
            "status": "queued",
            "trained_at": None,
            "result": None,
            "hash": fingerprint,
        }
        queue.append(entry)
        self._save_lock(queue)
        return True

    def queue_batch(self, items: List[Dict]) -> Tuple[int, int]:
        """批量加入队列

        Args:
            items: [{"symbol":str, "name":str, "priority":str, ...}]

        Returns:
            (added_count, skipped_count)
        """
        added = 0
        skipped = 0
        for item in items:
            if self.queue_for_retrain(
                symbol=item["symbol"],
                name=item.get("name", item["symbol"]),
                priority=item.get("priority", "medium"),
                reason=item.get("reason", ""),
                accuracies=item.get("accuracies"),
            ):
                added += 1
            else:
                skipped += 1
        return added, skipped

    def sync_low_accuracy_from_calibration(
        self,
        calibration_path: Optional[Path] = None,
        degraded_path: Optional[Path] = None,
        critical: float = 0.45,
        planned: float = 0.50,
    ) -> Dict:
        """Queue low-accuracy symbols from calibration/degraded model state.

        completed entries do not suppress a new queue request; only queued or
        training entries are de-duplicated by queue_for_retrain().
        """
        import yaml

        cal_path = calibration_path or (PROJECT_ROOT / "confidence_data" / "prediction_calibration.json")
        deg_path = degraded_path or (PROJECT_ROOT / "confidence_data" / "degraded_models.json")
        if not cal_path.exists():
            return {"added": 0, "skipped": 0, "candidates": 0, "reason": "calibration_missing"}

        try:
            with open(cal_path, "r", encoding="utf-8") as f:
                calibration = json.load(f)
        except Exception as e:
            return {"added": 0, "skipped": 0, "candidates": 0, "reason": f"calibration_invalid:{e}"}

        name_map = {}
        pool_degraded = set()
        pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        try:
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = yaml.safe_load(f) or {}
            for item in pool.get("master_pool", []):
                symbol = str(item.get("symbol", "")).zfill(6)
                if symbol:
                    name_map[symbol] = item.get("name", symbol)
                    if item.get("degraded", False):
                        pool_degraded.add(symbol)
        except Exception:
            pass

        degraded = {}
        if deg_path.exists():
            try:
                with open(deg_path, "r", encoding="utf-8") as f:
                    degraded = json.load(f) or {}
            except Exception:
                degraded = {}

        candidates = []
        stock_accuracy = calibration.get("stock_accuracy", {})
        for symbol, info in stock_accuracy.items():
            if not isinstance(info, dict):
                continue
            symbol = str(symbol).zfill(6)
            # v4.7.2 P1-1: degraded标的(池内degraded标记或degraded_models)停止自动入队
            # 与batch_train停训机制对齐, 堵住"入队→跳过→expired→再入队"死循环; 此类标的手动重训走Dashboard
            if symbol in pool_degraded or symbol in degraded:
                continue
            acc = info.get("last_accuracy", None)
            if not isinstance(acc, (int, float)):
                continue
            if acc >= planned:
                continue
            if acc < critical:
                priority = "critical"
                reason = f"校准精度{acc:.2%}<45%，紧急重训"
            elif acc < planned:
                priority = "medium"
                reason = f"校准精度{acc:.2%}<50%，计划重训"
            else:
                priority = "medium"
                reason = f"degraded_models标记低精度，当前精度{acc:.2%}"
            candidates.append({
                "symbol": symbol,
                "name": info.get("name") or name_map.get(symbol, symbol),
                "priority": priority,
                "reason": reason,
                "accuracies": info.get("accuracies", [acc]),
            })

        added, skipped = self.queue_batch(candidates)
        return {
            "added": added,
            "skipped": skipped,
            "candidates": len(candidates),
            "critical": sum(1 for c in candidates if c.get("priority") == "critical"),
            "medium": sum(1 for c in candidates if c.get("priority") == "medium"),
        }

    def mark_completed(self, symbol: str, result: Optional[Dict] = None) -> bool:
        """标记某标的训练完成

        Args:
            symbol: 股票代码
            result: 训练结果 {"accuracy": 0.55, "status": "success", ...}

        Returns:
            True=标记成功, False=队列中未找到
        """
        queue = self._load_lock()
        found = False
        for item in queue:
            if item.get("symbol") == symbol and item.get("status") in ("queued", "training"):
                item["status"] = "completed"
                item["trained_at"] = datetime.now().isoformat()
                item["result"] = result
                found = True
                break
        if found:
            self._save_lock(queue)
        return found

    def mark_failed(self, symbol: str, error: str) -> bool:
        """标记某标的训练失败（保留在队列中供重试）"""
        queue = self._load_lock()
        found = False
        for item in queue:
            if item.get("symbol") == symbol and item.get("status") in ("queued", "training"):
                item["status"] = "failed"
                item["trained_at"] = datetime.now().isoformat()
                item["result"] = {"error": error}
                found = True
                break
        if found:
            self._save_lock(queue)
        return found

    def reset_stale_training(self, max_age_hours: int = 6) -> int:
        """将超时的 training 条目恢复为 queued，避免训练进程中断后队列卡死。"""
        queue = self._load_lock()
        now = datetime.now()
        reset = 0
        for item in queue:
            if item.get("status") != "training":
                continue
            started_at = item.get("started_at") or item.get("trained_at") or item.get("requested_at")
            try:
                started = datetime.fromisoformat(started_at) if started_at else now
            except Exception:
                started = now - timedelta(hours=max_age_hours + 1)
            if now - started > timedelta(hours=max_age_hours):
                item["status"] = "queued"
                item["started_at"] = None
                item["result"] = {
                    "stale_reset_at": now.isoformat(),
                    "previous_started_at": started_at,
                    "reason": f"training超过{max_age_hours}小时未完成，自动回队列",
                }
                item["retry_count"] = int(item.get("retry_count", 0)) + 1
                reset += 1
        if reset:
            self._save_lock(queue)
        return reset

    def reconcile_completed_from_training_status(self, training_status_path: Optional[Path] = None) -> int:
        """用 cache/training_status.json 对账，补齐已训练但仍停留在队列中的条目。"""
        path = training_status_path or (PROJECT_ROOT / "cache" / "training_status.json")
        if not path.exists():
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                training_status = json.load(f)
        except Exception:
            return 0

        queue = self._load_lock()
        now = datetime.now()
        completed = 0
        for item in queue:
            if item.get("status") not in ("queued", "training"):
                continue
            symbol = item.get("symbol", "")
            status = training_status.get(symbol, {})
            if not status or status.get("status") != "success":
                continue
            last_train_date = status.get("last_train_date", "")
            requested_at = item.get("requested_at", "")
            if not self._is_training_after_request(last_train_date, requested_at):
                continue
            item["status"] = "completed"
            item["trained_at"] = status.get("last_train_time") or now.isoformat()
            item["result"] = {
                "status": "success",
                "direction_accuracy": status.get("direction_accuracy", 0),
                "model_fresh": status.get("model_fresh", False),
                "source": "training_status_reconcile",
            }
            completed += 1
        if completed:
            self._save_lock(queue)
        return completed

    def dequeue(self, batch_size: int = 5,
                allowed_priorities: Optional[List[str]] = None) -> List[Dict]:
        """从队列中取出待重训条目（按优先级排序）

        Args:
            batch_size: 最多取出数量
            allowed_priorities: 过滤优先级，如 ["critical", "high"]

        Returns:
            取出的条目列表（status→training）
        """
        queue = self._load_lock()
        prio_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

        # 筛选排队中的条目，按优先级+加入时间排序
        pending = [
            item for item in queue
            if item.get("status") == "queued"
            and (allowed_priorities is None
                 or item.get("priority") in allowed_priorities)
        ]
        pending.sort(key=lambda x: (
            prio_order.get(x.get("priority", "low"), 9),
            x.get("requested_at", "")
        ))

        batch = pending[:batch_size]
        symbols_in_batch = {item["symbol"] for item in batch}

        # 标记为 training
        changed = False
        for item in queue:
            if item.get("symbol") in symbols_in_batch and item.get("status") == "queued":
                item["status"] = "training"
                item["started_at"] = datetime.now().isoformat()
                changed = True

        if changed:
            self._save_lock(queue)

        return batch

    # ──────────── 只读查询 API ────────────

    def list_pending(self) -> List[Dict]:
        """列出所有待重训条目"""
        queue = self._load_lock()
        return [item for item in queue if item.get("status") == "queued"]

    def list_by_status(self, status: str) -> List[Dict]:
        """按状态过滤"""
        queue = self._load_lock()
        return [item for item in queue if item.get("status") == status]

    def get_summary(self) -> Dict:
        """获取队列汇总"""
        queue = self._load_lock()
        total = len(queue)
        queued = sum(1 for item in queue if item.get("status") == "queued")
        training = sum(1 for item in queue if item.get("status") == "training")
        completed = sum(1 for item in queue if item.get("status") == "completed")
        failed = sum(1 for item in queue if item.get("status") == "failed")

        # 按优先级汇总
        prio_count = {}
        for item in queue:
            p = item.get("priority", "unknown")
            prio_count[p] = prio_count.get(p, 0) + 1

        return {
            "total": total,
            "queued": queued,
            "training": training,
            "completed": completed,
            "failed": failed,
            "by_priority": prio_count,
        }

    def validate_pool_consistency(self, pool_symbols: set) -> List[str]:
        """验证队列中所有条目是否在股票池中

        Args:
            pool_symbols: 当前股票池symbol集合

        Returns:
            stale_symbols: 已不在池中的symbol列表
        """
        queue = self._load_lock()
        stale = []
        for item in queue:
            sym = item.get("symbol", "")
            if sym and sym not in pool_symbols:
                stale.append(sym)
        return stale

    def remove_stale(self, pool_symbols: set) -> int:
        """移除队列中已不在股票池的条目

        Args:
            pool_symbols: 当前股票池symbol集合

        Returns:
            移除数量
        """
        queue = self._load_lock()
        before = len(queue)
        queue = [item for item in queue if item.get("symbol", "") in pool_symbols]
        removed = before - len(queue)
        if removed > 0:
            self._save_lock(queue)
        return removed

    def clear_all(self) -> int:
        """清空队列"""
        queue = self._load_lock()
        count = len(queue)
        self._save_lock([])
        return count

    def clear_completed(self) -> int:
        """清除已完成的条目"""
        queue = self._load_lock()
        before = len(queue)
        queue = [item for item in queue if item.get("status") not in ("completed", "failed")]
        removed = before - len(queue)
        if removed > 0:
            self._save_lock(queue)
        return removed

    # ──────────── 内部方法（文件锁保护）────────────

    @staticmethod
    def _fingerprint(symbol: str, priority: str) -> str:
        """生成去重指纹"""
        return f"{symbol}_{priority[:3]}"

    @staticmethod
    def _is_training_after_request(last_train_date: str, requested_at: str) -> bool:
        """Compare date-level training status with queue request time."""
        if not last_train_date:
            return False
        try:
            trained_day = datetime.fromisoformat(last_train_date).date()
        except Exception:
            try:
                trained_day = datetime.strptime(last_train_date[:10], "%Y-%m-%d").date()
            except Exception:
                return False
        try:
            requested_day = datetime.fromisoformat(requested_at).date()
        except Exception:
            requested_day = trained_day
        return trained_day >= requested_day

    def _load_lock(self) -> list:
        """线程安全加载队列（文件锁 + 重试）"""
        lock_path = LOCK_DIR / "retrain_queue.lock"
        lock = FileLock(str(lock_path), timeout=LOCK_TIMEOUT)
        retries = 3
        for attempt in range(retries):
            try:
                with lock:
                    if self.queue_path.exists():
                        with open(self.queue_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        return data if isinstance(data, list) else []
                    return []
            except (Timeout, OSError) as e:
                if attempt < retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise RuntimeError(f"无法获取队列锁(重试{retries}次): {e}") from e

    def _save_lock(self, queue: list):
        """线程安全保存队列"""
        lock_path = LOCK_DIR / "retrain_queue.lock"
        lock = FileLock(str(lock_path), timeout=LOCK_TIMEOUT)
        retries = 3
        for attempt in range(retries):
            try:
                with lock:
                    # 写时备份
                    if self.queue_path.exists():
                        bak = self.queue_path.with_suffix(".json.bak")
                        self.queue_path.rename(bak)
                    with open(self.queue_path, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                return
            except (Timeout, OSError) as e:
                if attempt < retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise RuntimeError(f"无法保存队列锁(重试{retries}次): {e}") from e


# ──────────── 便捷函数 ────────────

_mgr_instance = None


def get_retrain_queue_manager() -> RetrainQueueManager:
    """获取全局队列管理器单例"""
    global _mgr_instance
    if _mgr_instance is None:
        _mgr_instance = RetrainQueueManager()
    return _mgr_instance


def queue_for_retrain(symbol: str, name: str, priority: str = "medium",
                      reason: str = "") -> bool:
    """便捷：将一只标的加入重训队列"""
    return get_retrain_queue_manager().queue_for_retrain(symbol, name, priority, reason)


def validate_retrain_queue(pool_symbols: Optional[set] = None) -> Dict:
    """验证重训队列一致性

    Args:
        pool_symbols: 当前股票池集合（如为None则自动加载）

    Returns:
        {"valid": bool, "stale": [str], "message": str}
    """
    import yaml
    mgr = get_retrain_queue_manager()

    if pool_symbols is None:
        pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        if pool_path.exists():
            with open(pool_path, "r", encoding="utf-8") as f:
                pool = yaml.safe_load(f)
            pool_symbols = {s["symbol"] for s in pool.get("master_pool", []) if s.get("symbol")}
        else:
            return {"valid": False, "stale": [], "message": "股票池文件不存在"}

    stale = mgr.validate_pool_consistency(pool_symbols)
    removed = mgr.remove_stale(pool_symbols)

    if stale:
        return {
            "valid": False,
            "stale": stale,
            "removed": removed,
            "message": f"清理{removed}只已不在池中的标的: {', '.join(stale[:5])}",
        }
    return {
        "valid": True,
        "stale": [],
        "removed": 0,
        "message": "队列与股票池一致",
    }


def get_queue_summary() -> Dict:
    """获取队列汇总(便捷)"""
    return get_retrain_queue_manager().get_summary()


# ──────────── 自测 ────────────
if __name__ == "__main__":
    import yaml
    print(f"{'='*60}")
    print("🧪 RetrainQueueManager 测试")
    print(f"{'='*60}")

    mgr = RetrainQueueManager()

    # 清空已有数据，确保测试干净
    mgr.clear_all()

    # 测试加入
    print("\n📝 加入队列...")
    assert mgr.queue_for_retrain("000001", "平安银行", "critical", "精度低于45%"), "首次应成功"
    assert not mgr.queue_for_retrain("000001", "平安银行", "critical", "重复"), "重复应跳过"
    assert mgr.queue_for_retrain("002466", "天齐锂业", "high", "精度退化")
    assert mgr.queue_for_retrain("300390", "天华新能", "medium", "精度低于阈值")
    assert mgr.queue_for_retrain("603799", "华友钴业", "low", "精度停滞")

    # 测试查询
    summary = mgr.get_summary()
    print(f"  队列汇总: {summary}")
    assert summary["queued"] == 4

    # 测试 dequeue
    print("\n📦 取出高优先级批...")
    batch = mgr.dequeue(batch_size=2)
    assert len(batch) == 2
    assert batch[0]["priority"] == "critical", "critical应优先"
    assert batch[0]["status"] == "training"

    summary = mgr.get_summary()
    assert summary["queued"] == 2
    assert summary["training"] == 2

    # 测试标记完成
    print("\n✅ 标记完成...")
    assert mgr.mark_completed("000001", {"accuracy": 0.62, "status": "success"})

    # 测试池一致性
    print("\n🔍 池一致性验证...")
    pool = {"000001", "002466", "300390", "000002"}
    stale = mgr.validate_pool_consistency(pool)
    print(f"  失联标的: {stale}")
    assert "603799" in stale, "603799不在测试池中"

    removed = mgr.remove_stale(pool)
    print(f"  清理: {removed}只")
    assert removed >= 1

    print("\n✅ 所有测试通过!")
    mgr.clear_all()
