#!/usr/bin/env python3
"""
core/calibration_feedback.py — DSL v4.5.9 校准闭环分析引擎

功能:
  1. 读取 prediction_calibration.json 分析每只标的精度趋势
  2. 识别精度退化/持续低精度标的，生成重训优先级列表
  3. 精度趋势写入 adaptive_params.yaml，供动态阈值引擎消费
  4. 输出重训建议供 batch_train.py 使用

用法:
  from core.calibration_feedback import CalibrationFeedback
  engine = CalibrationFeedback()
  priorities = engine.analyze()        # 返回重训优先级列表
  engine.write_to_adaptive_params()    # 写入 adaptive_params.yaml

数据链:
  prediction_calibration.json → analyze() → adaptive_params.yaml.calibration
                                                     ↓
                                            batch_train.py 读取优先列表
                                                     ↓
                                            重训完成 → 更新 calibration 记录
"""
import os, sys, json, math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "adaptive_params.yaml"
CALIBRATION_DIR = PROJECT_ROOT / "confidence_data"
PRED_CALIBRATION = CALIBRATION_DIR / "prediction_calibration.json"

# 阈值
MIN_CONFIDENCE_THRESHOLD = 0.50       # 低于此值需要重训
DEGRADE_RATIO = 0.95                  # 精度下降超过5%标记为退化
DEGRADE_CONSECUTIVE_DAYS = 3          # 连续N天下降标记为严重退化
CRITICAL_LOW_ACCURACY = 0.45          # 低于此值立即标记
STAGNANT_DAYS_THRESHOLD = 5           # 连续N天精度不变且<阈值标记为停滞
MAX_PRIORITY_ITEMS = 10               # 最大重训优先级数量

# v4.7.6: 麦蕊 get_kline_history 原始列名 t/o/h/l/c/v/a/pc/sf
# → _calc_return_from_df 期望的 date/close 等, 否则 KeyError 被吞 → 恒返回 None
MAIRUI_KLINE_COLUMNS = {
    "t": "date_raw", "o": "open", "h": "high", "l": "low",
    "c": "close", "v": "volume", "a": "amount",
    "pc": "prev_close", "sf": "adjust_flag",
}
# _calc_return_from_df 可接受的收盘价列名(麦蕊映射后为 close, akshare/东财为 收盘)
CLOSE_COLUMN_CANDIDATES = ("close", "收盘", "c")

# 重训优先级等级
PRIORITY_CRITICAL = "critical"        # 精度低于45%
PRIORITY_HIGH = "high"                # 精度持续退化
PRIORITY_MEDIUM = "medium"            # 精度低于阈值
PRIORITY_LOW = "low"                  # 精度停滞/可选


class CalibrationFeedback:
    """校准反馈分析引擎"""
    
    def __init__(self):
        self.config = self._load_config()
        self.calibration = self._load_calibration()
        self._trade_dates_cache = None  # lazy load

    def _is_trading_day(self, date) -> bool:
        """判断是否为A股交易日
        v4.5.7: 修复5/3等假日数据被错误验证的问题
        """
        if self._trade_dates_cache is None:
            try:
                import akshare as ak
                df = ak.tool_trade_date_hist_sina()
                self._trade_dates_cache = set(df["trade_date"].dt.date.tolist())
            except Exception:
                self._trade_dates_cache = None
        if self._trade_dates_cache is not None:
            return date in self._trade_dates_cache
        return date.weekday() < 5
    
    def _load_config(self) -> dict:
        """加载 adaptive_params.yaml"""
        try:
            import yaml
            with open(CONFIG_PATH, "r") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
    
    def _save_config(self):
        """保存 adaptive_params.yaml"""
        try:
            import yaml
            with open(CONFIG_PATH, "w") as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            print(f"⚠️ 校准反馈: 保存 config 失败: {e}")
    
    def _load_calibration(self) -> dict:
        """加载 prediction_calibration.json"""
        try:
            if PRED_CALIBRATION.exists():
                with open(PRED_CALIBRATION, "r") as f:
                    return json.load(f)
        except Exception as e:
            print(f"⚠️ 校准反馈: 加载校准数据失败: {e}")
        return {"stock_accuracy": {}, "daily_records": []}
    
    def analyze(self) -> List[dict]:
        """分析精度趋势，返回重训优先级列表

        v4.5.5 S4: accuracy数组已按训练版本去重，analyze使用去重后数据
        v4.5.5 S5: correct_predictions已从daily_records.realized_correct同步计数

        Returns:
            [{"symbol": str, "name": str, "priority": str,
              "current_acc": float, "reason": str, "action": str}, ...]
        """
        stock_acc = self.calibration.get("stock_accuracy", {})
        daily_records = self.calibration.get("daily_records", [])
        
        priorities = []
        
        for symbol, sa in stock_acc.items():
            name = sa.get("name", symbol)
            accuracies = sa.get("accuracies", [])
            if not accuracies:
                continue
            
            current_acc = sa.get("last_accuracy", accuracies[-1])
            mean_acc = sa.get("mean_accuracy", 
                              sum(accuracies) / len(accuracies) if accuracies else 0)
            
            # ── 条件1: 精度低于45% → critical ──
            if current_acc < CRITICAL_LOW_ACCURACY:
                priorities.append({
                    "symbol": symbol, "name": name,
                    "priority": PRIORITY_CRITICAL,
                    "current_acc": round(current_acc, 4),
                    "mean_acc": round(mean_acc, 4),
                    "reason": f"精度{current_acc:.1%}<{CRITICAL_LOW_ACCURACY:.0%}",
                    "action": "立即重训+增强正则化",
                })
                continue
            
            # ── 条件2: 精度持续退化 (最后3次持续下降) ──
            if len(accuracies) >= DEGRADE_CONSECUTIVE_DAYS + 1:
                last_n = accuracies[-DEGRADE_CONSECUTIVE_DAYS-1:]
                degraded = all(last_n[i] > last_n[i+1] for i in range(len(last_n)-1))
                if degraded:
                    decline_pct = (last_n[0] - last_n[-1]) / max(last_n[0], 0.01)
                    priorities.append({
                        "symbol": symbol, "name": name,
                        "priority": PRIORITY_HIGH,
                        "current_acc": round(current_acc, 4),
                        "mean_acc": round(mean_acc, 4),
                        "reason": f"精度连续{DEGRADE_CONSECUTIVE_DAYS}天下降({decline_pct:.1%})",
                        "action": "优先重训",
                    })
                    continue
            
            # ── 条件3: 精度低于50% → medium ──
            if current_acc < MIN_CONFIDENCE_THRESHOLD:
                priorities.append({
                    "symbol": symbol, "name": name,
                    "priority": PRIORITY_MEDIUM,
                    "current_acc": round(current_acc, 4),
                    "mean_acc": round(mean_acc, 4),
                    "reason": f"精度{current_acc:.1%}<{MIN_CONFIDENCE_THRESHOLD:.0%}",
                    "action": "计划重训",
                })
                continue
            
            # ── 条件4: 精度停滞 (连续N天相同值且<阈值) ──
            if len(set(accuracies[-STAGNANT_DAYS_THRESHOLD:])) == 1 \
               and current_acc < 0.53:
                priorities.append({
                    "symbol": symbol, "name": name,
                    "priority": PRIORITY_LOW,
                    "current_acc": round(current_acc, 4),
                    "mean_acc": round(mean_acc, 4),
                    "reason": f"精度停滞在{current_acc:.1%}超过{STAGNANT_DAYS_THRESHOLD}天",
                    "action": "可选重训",
                })
        
        # 按优先级排序: critical → high → medium → low
        priority_order = {PRIORITY_CRITICAL: 0, PRIORITY_HIGH: 1, 
                         PRIORITY_MEDIUM: 2, PRIORITY_LOW: 3}
        priorities.sort(key=lambda p: (priority_order.get(p["priority"], 9), -p["current_acc"]))
        
        # 限制最大数量
        return priorities[:MAX_PRIORITY_ITEMS]
    
    def get_retrain_plan(self) -> dict:
        """生成重训计划
        
        Returns:
            {"priorities": [...], "summary": str,
             "critical": int, "high": int, "medium": int, "low": int}
        """
        priorities = self.analyze()
        
        critical = sum(1 for p in priorities if p["priority"] == PRIORITY_CRITICAL)
        high = sum(1 for p in priorities if p["priority"] == PRIORITY_HIGH)
        medium = sum(1 for p in priorities if p["priority"] == PRIORITY_MEDIUM)
        low = sum(1 for p in priorities if p["priority"] == PRIORITY_LOW)
        total = len(priorities)
        
        summary_parts = []
        if critical:
            summary_parts.append(f"🔴 {critical}只急迫重训")
        if high:
            summary_parts.append(f"🟠 {high}只优先重训")
        if medium:
            summary_parts.append(f"🟡 {medium}只计划重训")
        if low:
            summary_parts.append(f"🟢 {low}只可选重训")
        
        return {
            "priorities": priorities,
            "summary": " | ".join(summary_parts) if summary_parts else "✅ 无标的需重训",
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
            "total": total,
            "analyzed_at": datetime.now().isoformat(),
        }
    
    def write_to_adaptive_params(self):
        """将校准分析结果写入 adaptive_params.yaml (线程安全)"""
        from common.file_lock import locked_rw
        
        plan = self.get_retrain_plan()
        
        with locked_rw(CONFIG_PATH, is_yaml=True) as (lock, config):
            config.setdefault("calibration", {})
            config["calibration"] = {
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "retrain_plan": {
                    "priority_stocks": [
                        {
                            "symbol": p["symbol"],
                            "name": p["name"],
                            "priority": p["priority"],
                            "current_accuracy": p["current_acc"],
                            "action": p["action"],
                        }
                        for p in plan["priorities"]
                    ],
                    "summary": plan["summary"],
                    "total_flagged": plan["total"],
                },
            }
            # 同步 self.config 引用供后续使用
            self.config = config
        
        # v4.5.7: 将重训标的同步加入 retrain_queue_manager
        try:
            from core.retrain_queue_manager import get_retrain_queue_manager
            mgr = get_retrain_queue_manager()
            added = 0
            for p in plan["priorities"]:
                if mgr.queue_for_retrain(
                    symbol=p["symbol"],
                    name=p["name"],
                    priority=p["priority"],
                    reason=p["reason"],
                    accuracies=[p.get("current_acc", 0)],
                ):
                    added += 1
            if added:
                print(f"  📋 calibration → 重训队列: {added}只新加入")
            sync_result = mgr.sync_low_accuracy_from_calibration()
            if sync_result.get("added", 0):
                print(f"  📋 calibration低精度闭环 → 重训队列: {sync_result['added']}只新加入")
        except Exception as e:
            print(f"  ⚠️ 重训队列同步跳过: {e}")
        
        return plan
    
    def get_threshold_adjustments(self, symbol: str) -> dict:
        """为指定标的返回基于校准数据的阈值调整
        
        Args:
            symbol: 股票代码
            
        Returns:
            {"hard_floor_boost": float, "accuracy_trend": str}
        """
        stock_acc = self.calibration.get("stock_accuracy", {})
        sa = stock_acc.get(symbol, {})
        accuracies = sa.get("accuracies", [])
        current_acc = sa.get("last_accuracy", 0) if sa else 0
        
        # 基于精度趋势调整动态阈值硬底线
        hard_floor_boost = 0.0
        trend = "stable"
        
        if current_acc < 0.40:
            hard_floor_boost = 0.15  # 硬底线 +15%
            trend = "critical_low"
        elif current_acc < 0.45:
            hard_floor_boost = 0.10  # +10%
            trend = "low"
        elif current_acc < 0.50:
            hard_floor_boost = 0.05  # +5%
            trend = "below_par"
        
        # 退化趋势检测
        if len(accuracies) >= 3:
            if accuracies[-3] > accuracies[-2] > accuracies[-1]:
                hard_floor_boost += 0.05
                trend = "degrading"
        
        return {
            "hard_floor_boost": round(hard_floor_boost, 2),
            "accuracy_trend": trend,
            "current_accuracy": round(current_acc, 4),
        }

    # ═══════════════════════════════════════════════
    # 兑现精度回溯验证
    # ═══════════════════════════════════════════════
    
    def verify_daily_record_integrity(self) -> dict:
        """v4.5.7: 验证 sum(daily.correct_predictions) == overall.correct_predictions

        calibration反馈后自动运行，确保聚合计数一致。
        """
        daily_records = self.calibration.get("daily_records", [])
        overall = self.calibration.get("overall_stats", {})

        overall_correct = overall.get("correct_predictions", 0)
        sum_daily = sum(dr.get("correct_predictions", 0) for dr in daily_records)

        if overall_correct == sum_daily:
            return {
                "valid": True,
                "overall_correct": overall_correct,
                "sum_daily_correct": sum_daily,
                "message": f"✅ daily_record完整性OK: overall={overall_correct}, sum(daily)={sum_daily}",
            }

        # 不一致: 自动修复 — 以 sum(daily) 为准
        print(f"⚠️ daily_record完整性断裂: overall={overall_correct} vs sum(daily)={sum_daily}, 自动修复...")
        overall["correct_predictions"] = sum_daily
        self.calibration["overall_stats"] = overall
        self._save_calibration()

        return {
            "valid": False,
            "overall_correct": overall_correct,
            "sum_daily_correct": sum_daily,
            "repaired": True,
            "message": f"🛠️ daily_record已修复: overall={overall_correct}→{sum_daily}",
        }

    def check_realized_accuracy(self, max_days: int = 30) -> dict:
        """回溯验证历史预测的兑现精度
        
        扫描 daily_records 中 realiced_checked=False 的记录，
        对于预测周期已过的标的，比较predicted_return与实际收益。
        
        Args:
            max_days: 最多回溯多少天的预测
            
        Returns:
            {"checked": int, "correct": int, "total": int,
             "accuracy": float, "errors": [str]}
        """
        errors = []
        checked_count = 0
        correct_count = 0
        total_count = 0
        
        daily_records = self.calibration.get("daily_records", [])
        today = datetime.now().date()
        modified = False
        
        for di, dr in enumerate(daily_records):
            try:
                pred_date = datetime.strptime(dr["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            
            days_elapsed = (today - pred_date).days
            if days_elapsed > max_days or days_elapsed < 1:
                continue
            
            for si, s in enumerate(dr.get("stocks", [])):
                # v4.5.3c: 跳过旧格式记录(无predicted_return字段)
                if "predicted_return" not in s:
                    continue
                if s.get("realized_checked", False):
                    continue
                # v4.5.7: 跳过非交易日（假日/周末）的预测记录
                if not self._is_trading_day(pred_date):
                    daily_records[di]["stocks"][si]["realized_checked"] = True
                    modified = True
                    continue
                if s.get("signal", "hold") == "hold":
                    daily_records[di]["stocks"][si]["realized_checked"] = True
                    # v4.7.4(P0-2): hold不再默认判"正确" — hold是方向性预测,
                    # 未验证不得计correct。历史注水: 1644条hold全计correct致兑现
                    # 精度虚高至75%(真实非hold仅46.4%)。correct置None=剔除分母。
                    daily_records[di]["stocks"][si]["realized_correct"] = None
                    modified = True
                    continue
                
                symbol = s.get("symbol", "")
                if not symbol:
                    continue
                
                horizon = s.get("horizon", "5d")
                try:
                    horizon_days = int(''.join(c for c in horizon if c.isdigit()))
                except ValueError:
                    horizon_days = 1
                if horizon_days < 1:
                    horizon_days = 1

                # v4.7.6: 资格门槛从"自然日≥horizon"改为保守的交易日→自然日估算(5交易日≈7自然日),
                # 避免为必然未到期的记录白拉一次网络K线。
                # 真正的"是否已满整窗口"权威判定在 _calc_return_from_df(按K线行序, 不足即 None)。
                min_calendar_days = -(-horizon_days * 7 // 5)  # ceil(horizon * 1.4)
                if days_elapsed < min_calendar_days:
                    continue
                
                try:
                    actual_return = self._fetch_actual_return(
                        symbol, pred_date, horizon_days)
                except Exception as e:
                    errors.append(f"{symbol}({s.get('name','')}): {e}")
                    continue
                
                if actual_return is None:
                    continue
                
                predicted_ret = s.get("predicted_return", 0)
                predicted_direction = 1 if predicted_ret > 0 else (-1 if predicted_ret < 0 else 0)
                actual_direction = 1 if actual_return > 0 else (-1 if actual_return < 0 else 0)
                correct = (predicted_direction == actual_direction)
                
                # P2(对齐 rank_ic_monitor.fill_realized): 保留全精度写入, 避免 round(4)
                # 制造伪并列改变下游 Spearman 排名; 精度只在展示层裁剪(下方日志 %.2f)。
                # 只改精度, 不改方向判定/realized_correct/阈值 → 校准闭环语义不变。
                daily_records[di]["stocks"][si]["realized_return"] = actual_return
                daily_records[di]["stocks"][si]["realized_correct"] = correct
                daily_records[di]["stocks"][si]["realized_checked"] = True
                modified = True
                
                checked_count += 1
                total_count += 1
                if correct:
                    correct_count += 1
                
                if checked_count <= 10 or checked_count % 20 == 0:
                    emoji = "✅" if correct else "❌"
                    print(f"  {emoji} {symbol} {s.get('name','')}: "
                          f"预测{predicted_ret:+.2%} vs 实际{actual_return:+.2%} "
                          f"({horizon}, {pred_date})")
        
        if modified:
            self.calibration["daily_records"] = daily_records
            # v4.5.5 S5: 从daily_records重新累加realized_correct总数，而非仅增量
            # v4.5.5 S6(correct_predictions修复): 同时逐日写入daily_record级correct_predictions
            # v4.7.4(P0-2): 只统计非hold信号的realized_correct, hold剔除分母
            total_correct = 0
            for dr in daily_records:
                day_correct = sum(1 for s in dr.get("stocks", [])
                                  if s.get("signal", "hold") != "hold"
                                  and (s.get("realized_correct") is True or s.get("realized_correct") == 1))
                dr["correct_predictions"] = day_correct
                total_correct += day_correct
            ov = self.calibration.get("overall_stats", {})
            ov["correct_predictions"] = total_correct
            self.calibration["overall_stats"] = ov
            self._save_calibration()
        
        realized_acc = correct_count / max(total_count, 1)
        
        return {
            "checked": checked_count,
            "correct": correct_count,
            "total": total_count,
            "accuracy": round(realized_acc, 4),
            "errors": errors[:10],
        }
    
    def _fetch_actual_return(self, symbol: str, pred_date, horizon_days: int):
        """获取一只股票在预测窗口的实际收益

        v4.6.9i(审计F1-5): 主源换麦蕊 get_kline_history(前复权f) — akshare stock_zh_a_hist(东财push2his)
        实测封锁(2026-08-14), 原实现会让校准闭环全灭; akshare仅作降级。
        v4.7.6(审计遗留§4): 修复麦蕊列名未映射(t/o/h/l/c → date/close)导致恒 None,
        并让主源任一失败路径(异常/空数据/窗口外)真正回落降级链。
        v4.7.6(段2): 降级链补 mootdx(通达信, TCP 直连不封 IP); 各源统一走 _normalize_kline_df,
        避免"降级源存在但形同虚设"(本机 akshare 东财源实测 ProxyError 仍封锁)。
        """
        from datetime import timedelta
        start = (pred_date - timedelta(days=5)).strftime("%Y%m%d")
        end = (pred_date + timedelta(days=horizon_days * 3)).strftime("%Y%m%d")

        def _try(df):
            df = self._normalize_kline_df(df)
            if df is None:
                return None
            return self._calc_return_from_df(df, pred_date, horizon_days)

        # 主源: 麦蕊 (v4.7.6 修复)
        #   旧缺陷: 直接把 t/o/h/l/c 原始列交给只认 close/收盘 的 _calc_return_from_df
        #   → KeyError 被内层 except 吞掉 → 恒返回 None; 且该分支为 return(非抛错),
        #   主源失败不会回落 → check_realized_accuracy 空跑。
        try:
            from config.mairui_api_config import get_kline_history
            rows = get_kline_history(symbol, period="d", adjust="f",
                                     start_date=start, end_date=end)
            if rows and isinstance(rows, list) and len(rows) >= 2:
                import pandas as pd
                ret = _try(pd.DataFrame(rows).rename(columns=MAIRUI_KLINE_COLUMNS))
                if ret is not None:
                    return ret
        except Exception:
            pass

        # 降级链: akshare(东财 push2his, 本机实测 ProxyError 封锁) → mootdx(通达信 TCP 直连)
        #   任一步算出非 None 即结束; 逐源失败不抛出, 交由上层按"未兑现"处理。
        for loader in (self._load_akshare_daily, self._load_mootdx_daily):
            try:
                ret = _try(loader(symbol, start, end))
                if ret is not None:
                    return ret
            except Exception:
                continue
        return None

    @staticmethod
    def _normalize_kline_df(df):
        """把各源K线规整为 _calc_return_from_df 需要的形状: 统一 `date` 列(datetime.date)。
        麦蕊(date_raw/t) / akshare·东财(日期) / mootdx(日期) 口径由此收口。"""
        if df is None or len(df) < 2:
            return None
        import pandas as pd
        d = df.copy()
        date_col = next((c for c in ("date", "date_raw", "日期", "datetime", "t")
                         if c in d.columns), None)
        if date_col is None:
            return None
        d["date"] = pd.to_datetime(d[date_col], errors="coerce").dt.date
        d = d[d["date"].notna()]
        return d if len(d) >= 2 else None

    @staticmethod
    def _load_akshare_daily(symbol: str, start: str, end: str):
        import akshare as ak
        return ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                  start_date=start, end_date=end, adjust="qfq")

    @staticmethod
    def _load_mootdx_daily(symbol: str, start: str, end: str):
        from common.mootdx_adapter import get_kline
        return get_kline(symbol, start=start, end=end)

    def _calc_return_from_df(self, df, pred_date, horizon_days: int):
        """从K线DataFrame计算预测窗口实际收益

        v4.7.6: ① 收盘价列按 CLOSE_COLUMN_CANDIDATES 解析(close/收盘/c);
                ② 指标位一律按行序 —— 先 reset_index, 修正 akshare 分支未重置索引时
                   `index[0]` 非位置值的问题;
                ③ **必须有完整 horizon 个交易日的后续K线才结算**: 旧实现
                   `min(horizon_days, len(df)-pred_idx-1)` 会在数据源尚未推进满窗口时
                   静默截断, 用"不足 horizon 的窗口"冒充已兑现收益, 直接污染兑现精度/IC。
                   不足即返回 None(视为未到期, 留待下轮)。
        """
        try:
            close_col = next((c for c in CLOSE_COLUMN_CANDIDATES if c in df.columns), None)
            if close_col is None or "date" not in df.columns:
                return None
            df = df.sort_values("date").reset_index(drop=True)
            pred_rows = df[df["date"] == pred_date]
            if len(pred_rows) == 0:
                pred_rows = df[df["date"] < pred_date].tail(1)
                if len(pred_rows) == 0:
                    return None
            pred_idx = int(pred_rows.index[0])
            if len(df) - pred_idx - 1 < horizon_days:
                return None  # 未满整窗口 → 不得用截断窗口结算
            target_idx = pred_idx + horizon_days
            pred_close = float(df.loc[pred_idx, close_col])
            future_close = float(df.loc[target_idx, close_col])
            return (future_close - pred_close) / pred_close
        except Exception:
            return None
    
    def _save_calibration(self):
        """保存 prediction_calibration.json"""
        try:
            self.calibration["last_updated"] = datetime.now().isoformat()
            with open(PRED_CALIBRATION, "w") as f:
                json.dump(self.calibration, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ 保存校准数据失败: {e}")


# ═══════════════════ 快捷接口 ═══════════════════
_cf_instance = None


def get_calibration_feedback() -> CalibrationFeedback:
    """获取（或创建）校准反馈引擎单例"""
    global _cf_instance
    if _cf_instance is None:
        _cf_instance = CalibrationFeedback()
    return _cf_instance


def analyze_trends() -> List[dict]:
    """快捷分析：精度趋势"""
    return get_calibration_feedback().analyze()


def get_retrain_plan() -> dict:
    """快捷获取重训计划"""
    return get_calibration_feedback().get_retrain_plan()


def get_threshold_adjustments(symbol: str) -> dict:
    """快捷获取指定标的的阈值调整"""
    return get_calibration_feedback().get_threshold_adjustments(symbol)


def verify_daily_record_integrity() -> dict:
    """快捷接口：验证 daily_record 完整性"""
    return get_calibration_feedback().verify_daily_record_integrity()


# ═══════════════════ 自测 ═══════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 校准反馈闭环引擎测试")
    print("=" * 60)
    
    engine = CalibrationFeedback()
    plan = engine.get_retrain_plan()
    
    print(f"\n📊 {plan['summary']}")
    print(f"   总数: {plan['total']}")
    
    for p in plan["priorities"]:
        emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        e = emoji.get(p["priority"], "⚪")
        print(f"  {e} {p['symbol']} {p['name']:8s} 精度={p['current_acc']:.1%} → {p['reason']}")
    
    # 写 adaptive_params
    engine.write_to_adaptive_params()
    print(f"\n✅ 已写入 adaptive_params.yaml")
    
    # 验证阈值调整
    for p in plan["priorities"][:3]:
        adj = engine.get_threshold_adjustments(p["symbol"])
        print(f"  📐 {p['symbol']}: hard_floor_boost={adj['hard_floor_boost']:.0%}, trend={adj['accuracy_trend']}")
    
    # v4.5.7: daily_record完整性验证
    print("\n🔍 daily_record完整性验证...")
    integrity = engine.verify_daily_record_integrity()
    emoji = "✅" if integrity["valid"] else "⚠️"
    print(f"  {emoji} {integrity['message']}")
    
    print("\n✅ 校准反馈引擎测试完成")
