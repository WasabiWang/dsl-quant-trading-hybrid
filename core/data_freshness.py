#!/usr/bin/env python3
"""Shared freshness checks for trading input data.

The execution rule is intentionally conservative: stale or unreadable ML
predictions must never open new BUY exposure, but SELL/reduce-only actions stay
allowed so risk controls can still reduce positions.
"""

import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Optional, Union, Dict, Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DAILY_PREDICT = PROJECT_ROOT / "cache" / "daily_predict.json"


def _coerce_datetime(value: Optional[Union[str, datetime, date]]) -> datetime:
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return datetime.strptime(text[:10], "%Y-%m-%d")


def _parse_predict_datetime(data: Dict[str, Any], path: Path) -> Optional[datetime]:
    for key in ("predict_time", "generated_at"):
        value = data.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            pass
    predict_date = data.get("predict_date")
    if predict_date:
        try:
            return datetime.strptime(str(predict_date)[:10], "%Y-%m-%d")
        except Exception:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def _latest_trading_day_on_or_before(day: date) -> date:
    try:
        from config.holiday_calendar import is_trading_day
        cur = day
        for _ in range(14):
            if is_trading_day(check_date=cur, market="A_SHARE"):
                return cur
            cur -= timedelta(days=1)
    except Exception:
        pass
    cur = day
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def _next_trading_day_on_or_after(day: date) -> date:
    try:
        from config.holiday_calendar import is_trading_day
        cur = day
        for _ in range(14):
            if is_trading_day(check_date=cur, market="A_SHARE"):
                return cur
            cur += timedelta(days=1)
    except Exception:
        pass
    cur = day
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _is_trading_day(day: date) -> bool:
    try:
        from config.holiday_calendar import is_trading_day
        return bool(is_trading_day(check_date=day, market="A_SHARE"))
    except Exception:
        return day.weekday() < 5


def assess_daily_predict_freshness(path: Optional[Union[str, Path]] = None,
                                   now: Optional[Union[str, datetime, date]] = None,
                                   max_age_hours: float = 24) -> Dict[str, Any]:
    """Assess whether daily_predict.json is safe for BUY decisions.

    Returns a stable dict:
      status: fresh | decayed | stale | missing | invalid
      allow_buy: bool
      allow_sell: bool
      age_hours, predict_date, predict_time, reason
    """
    check_time = _coerce_datetime(now)
    fpath = Path(path) if path is not None else DEFAULT_DAILY_PREDICT

    base = {
        "status": "missing",
        "allow_buy": False,
        "allow_sell": True,
        "age_hours": None,
        "predict_date": "",
        "predict_time": "",
        "reason": "",
    }

    if not fpath.exists():
        base["reason"] = f"{fpath} 不存在"
        return base

    try:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        base.update({"status": "invalid", "reason": f"无法解析 {fpath}: {exc}"})
        return base

    if not isinstance(data, dict):
        base.update({"status": "invalid", "reason": f"{fpath} 顶层不是dict"})
        return base

    pred_dt = _parse_predict_datetime(data, fpath)
    predict_date = str(data.get("predict_date") or (pred_dt.date().isoformat() if pred_dt else ""))
    predict_time = str(data.get("predict_time") or data.get("generated_at") or "")
    if pred_dt is None:
        base.update({
            "status": "invalid",
            "predict_date": predict_date,
            "predict_time": predict_time,
            "reason": "缺少可识别的 predict_time/predict_date",
        })
        return base

    age_hours = round((check_time - pred_dt).total_seconds() / 3600, 2)
    latest_trading_day = _latest_trading_day_on_or_before(check_time.date())
    next_trading_day = _next_trading_day_on_or_after(check_time.date() + timedelta(days=1))
    try:
        target_day = datetime.strptime(predict_date[:10], "%Y-%m-%d").date()
    except Exception:
        target_day = pred_dt.date()
    today_is_trading = _is_trading_day(check_time.date())

    common = {
        "age_hours": age_hours,
        "predict_date": predict_date,
        "predict_time": predict_time,
        "latest_trading_day": latest_trading_day.isoformat(),
        "next_trading_day": next_trading_day.isoformat(),
        "today_is_trading_day": today_is_trading,
    }

    if age_hours < 0:
        return {
            **base, **common,
            "status": "invalid",
            "reason": f"预测时间来自未来: age_hours={age_hours}",
        }

    if target_day == check_time.date() and age_hours <= max_age_hours:
        return {
            **base, **common,
            "status": "fresh",
            "allow_buy": True,
            "reason": f"预测为当天且 {age_hours:.1f}h <= {max_age_hours}h",
        }

    # v4.6.2 fix: 预测目标为下一交易日是最常见标准场景 (batch_predict产出的预测)
    if target_day == next_trading_day and age_hours <= max_age_hours:
        today_label = "交易日" if today_is_trading else "非交易日"
        return {
            **base, **common,
            "status": "fresh",
            "allow_buy": True,
            "reason": f"{today_label}, 预测目标为下一交易日 {next_trading_day.isoformat()}, {age_hours:.1f}h <= {max_age_hours}h",
        }

    if not today_is_trading and target_day == latest_trading_day:
        return {
            **base, **common,
            "status": "decayed",
            "allow_buy": True,
            "reason": f"非交易日, 使用最近交易日 {latest_trading_day.isoformat()} 预测并降权",
        }

    if age_hours <= max_age_hours and target_day == latest_trading_day:
        return {
            **base, **common,
            "status": "decayed",
            "allow_buy": True,
            "reason": f"最近交易日预测, 但非当天: {target_day.isoformat()}",
        }

    return {
        **base, **common,
        "status": "stale",
        "reason": (
            f"预测过期: predict_date={target_day.isoformat()}, "
            f"latest_trading_day={latest_trading_day.isoformat()}, age={age_hours:.1f}h"
        ),
    }
