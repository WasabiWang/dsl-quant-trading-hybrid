#!/usr/bin/env python3
"""Audit stock-pool concentration, tier sync, prediction quality, and correlation."""

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_WINDOWS = (126, 252, 504)


def _load_yaml(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _code(value) -> str:
    return str(value).zfill(6)


def _board(symbol: str) -> str:
    symbol = _code(symbol)
    if symbol.startswith("688"):
        return "科创板"
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith(("000", "001", "002", "003")):
        return "深主板/中小板"
    if symbol.startswith(("600", "601", "603", "605")):
        return "沪主板"
    return "其他"


def load_master_pool():
    pool = _load_yaml(PROJECT_ROOT / "config" / "master_stock_pool.yaml").get("master_pool", [])
    rows = []
    for item in pool:
        if not item.get("symbol"):
            continue
        row = dict(item)
        row["symbol"] = _code(row["symbol"])
        row["tier"] = row.get("tier", "core")
        row["sector"] = row.get("sector", "")
        rows.append(row)
    return rows


def load_stock_pool():
    data = _load_yaml(PROJECT_ROOT / "config" / "stock_pool.yaml")
    rows = []
    allocations = {}
    for tier, cfg in (data.get("tiers") or {}).items():
        allocations[tier] = float(cfg.get("allocation", 0) or 0)
        for item in cfg.get("stocks", []) or []:
            rows.append({
                "symbol": _code(item.get("code", "")),
                "name": item.get("name", ""),
                "sector": item.get("sector", ""),
                "tier": tier,
            })
    return rows, allocations


def load_policy():
    path = PROJECT_ROOT / "config" / "pool_structure_policy.yaml"
    if not path.exists():
        return {}
    return _load_yaml(path)


def active_symbols(master, policy):
    observation = set(policy.get("observation_tiers") or ["cyclical", "flex"])
    return {row["symbol"] for row in master if row.get("tier") not in observation}


def theme_hits(master):
    keywords = {
        "半导体/芯片": ["半导体", "芯片", "SOC", "存储", "MCU", "封测", "Chiplet", "GPU", "AIPC", "汽车芯片", "国产芯片"],
        "AI/端侧/智能": ["DeepSeek", "AI", "端侧", "智能眼镜", "AIGC", "具身智能", "大模型"],
        "新能源车/锂电/光伏": ["电池", "锂", "固态电池", "宁德时代", "比亚迪", "电解液", "光伏"],
        "白酒/消费": ["白酒", "饮料乳品", "食品饮料"],
        "金融": ["银行", "证券", "保险", "金融"],
    }
    hits = {}
    for theme, words in keywords.items():
        symbols = []
        for row in master:
            text = f"{row.get('sector', '')} {row.get('concept', '')} {row.get('name', '')}"
            if any(word in text for word in words):
                symbols.append(row["symbol"])
        hits[theme] = symbols
    return hits


def prediction_quality(master, active):
    path = PROJECT_ROOT / "cache" / "daily_predict.json"
    data = _load_json(path)
    predictions = data.get("predictions", [])
    if not isinstance(predictions, list):
        predictions = []
    meta = {row["symbol"]: row for row in master}
    rows = []
    for pred in predictions:
        if not isinstance(pred, dict) or not pred.get("symbol"):
            continue
        symbol = _code(pred["symbol"])
        if symbol not in meta:
            continue
        acc = pred.get("direction_accuracy", pred.get("accuracy"))
        rows.append({
            "symbol": symbol,
            "name": meta[symbol].get("name", symbol),
            "sector": meta[symbol].get("sector", ""),
            "tier": meta[symbol].get("tier", ""),
            "active": symbol in active,
            "signal": pred.get("signal"),
            "confidence_level": pred.get("confidence_level"),
            "direction_accuracy": float(acc) if isinstance(acc, (int, float)) else None,
            "confidence": pred.get("confidence"),
        })
    return rows, {
        "predict_time": data.get("predict_time"),
        "predict_date": data.get("predict_date"),
        "total_stocks": data.get("total_stocks"),
        "high_confidence": data.get("high_confidence"),
        "low_confidence": data.get("low_confidence"),
        "suspended": data.get("suspended"),
    }


def _kline_records(path: Path):
    data = _load_json(path)
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = rows.get("klines") or rows.get("items") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if isinstance(row, dict):
            date = row.get("date") or row.get("d") or row.get("day") or row.get("trade_date") or row.get("time")
            close = row.get("close") or row.get("c") or row.get("收盘") or row.get("close_price")
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            date, close = row[0], row[4]
        elif isinstance(row, str):
            parts = row.split(",")
            date = parts[0] if parts else None
            close = parts[2] if len(parts) > 2 else None
        else:
            continue
        try:
            out.append((str(date)[:10], float(close)))
        except Exception:
            continue
    return out


def returns_frame(symbols):
    frames = []
    missing = []
    for symbol in symbols:
        paths = list((PROJECT_ROOT / "cache").glob(f"kline_*{symbol}_*_day.json"))
        if not paths:
            missing.append(symbol)
            continue
        best = None
        best_n = -1
        best_end = ""
        for path in paths:
            rows = _kline_records(path)
            match = re.search(r"_(\d{4}-\d{2}-\d{2})_day", path.name)
            end = match.group(1) if match else path.name
            if len(rows) > best_n or (len(rows) == best_n and end > best_end):
                best = path
                best_n = len(rows)
                best_end = end
        if best is None or best_n <= 1:
            missing.append(symbol)
            continue
        df = pd.DataFrame(_kline_records(best), columns=["date", "close"])
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna().drop_duplicates("date").sort_values("date")
        df[symbol] = df["close"].pct_change()
        frames.append(df[["date", symbol]])

    if not frames:
        return pd.DataFrame(), missing
    merged = frames[0]
    for frame in frames[1:]:
        merged = merged.merge(frame, on="date", how="outer")
    return merged.sort_values("date").set_index("date"), missing


def correlation_summary(master, symbols, windows=DEFAULT_WINDOWS):
    ret, missing = returns_frame(symbols)
    if ret.empty:
        return {"missing": missing, "windows": {}}
    valid = ret[ret.notna().sum(axis=1) >= max(2, min(30, len(symbols)))]
    names = {row["symbol"]: row.get("name", row["symbol"]) for row in master}
    sectors = {row["symbol"]: row.get("sector", "") for row in master}
    output = {"missing": missing, "windows": {}}
    for window in windows:
        last = valid.tail(window)
        if len(last) < 60:
            continue
        corr = last.corr(min_periods=max(30, min(120, window // 2)))
        pairs = []
        for i, left in enumerate(symbols):
            for right in symbols[i + 1:]:
                if left not in corr.index or right not in corr.columns:
                    continue
                value = corr.loc[left, right]
                if not math.isnan(value):
                    pairs.append({
                        "corr": round(float(value), 4),
                        "left": left,
                        "left_name": names.get(left, left),
                        "left_sector": sectors.get(left, ""),
                        "right": right,
                        "right_name": names.get(right, right),
                        "right_sector": sectors.get(right, ""),
                    })
        vols = (last.std(skipna=True) * math.sqrt(252)).dropna()
        output["windows"][str(window)] = {
            "date_start": str(last.index.min().date()),
            "date_end": str(last.index.max().date()),
            "avg_pair_corr": round(sum(p["corr"] for p in pairs) / max(len(pairs), 1), 4),
            "pairs_ge_0_6": sum(p["corr"] >= 0.6 for p in pairs),
            "pairs_ge_0_7": sum(p["corr"] >= 0.7 for p in pairs),
            "top_pairs": sorted(pairs, key=lambda item: item["corr"], reverse=True)[:10],
            "avg_ann_vol": round(float(vols.mean()), 4) if not vols.empty else None,
            "median_ann_vol": round(float(vols.median()), 4) if not vols.empty else None,
            "top_vol": [
                {"symbol": symbol, "name": names.get(symbol, symbol), "ann_vol": round(float(value), 4)}
                for symbol, value in vols.sort_values(ascending=False).head(10).items()
            ],
        }
    return output


def run_audit(include_correlation=True):
    master = load_master_pool()
    stock_rows, allocations = load_stock_pool()
    policy = load_policy()
    active = active_symbols(master, policy)
    limits = policy.get("limits") or {}
    max_active_sector_ratio = float(limits.get("max_active_sector_ratio", 0.25))
    max_theme_ratio = float(limits.get("max_theme_ratio", 0.35))
    low_accuracy_buy_floor = float(limits.get("low_accuracy_buy_floor", 0.50))
    low_sector_accuracy_buy_floor = float(limits.get("low_sector_accuracy_buy_floor", 0.50))

    master_by_symbol = {row["symbol"]: row for row in master}
    stock_by_symbol = {row["symbol"]: row for row in stock_rows}
    issues = []
    warnings = []
    passes = []

    master_symbols = set(master_by_symbol)
    stock_symbols = set(stock_by_symbol)
    if master_symbols != stock_symbols:
        issues.append({
            "code": "pool_symbol_mismatch",
            "detail": {
                "master_only": sorted(master_symbols - stock_symbols),
                "stock_only": sorted(stock_symbols - master_symbols),
            },
        })
    else:
        passes.append("master_stock_pool and stock_pool symbols match")

    tier_sector_mismatches = []
    for symbol in sorted(master_symbols & stock_symbols):
        master_row = master_by_symbol[symbol]
        stock_row = stock_by_symbol[symbol]
        if master_row.get("tier") != stock_row.get("tier") or master_row.get("sector", "") != stock_row.get("sector", ""):
            tier_sector_mismatches.append({
                "symbol": symbol,
                "master_tier": master_row.get("tier"),
                "stock_tier": stock_row.get("tier"),
                "master_sector": master_row.get("sector", ""),
                "stock_sector": stock_row.get("sector", ""),
            })
    if tier_sector_mismatches:
        issues.append({"code": "pool_tier_or_sector_mismatch", "detail": tier_sector_mismatches})
    else:
        passes.append("master/stock tier and sector fields match")

    allocation_sum = round(sum(allocations.values()), 6)
    if abs(allocation_sum - 1.0) > 0.001:
        issues.append({"code": "allocation_sum_not_100pct", "detail": allocation_sum})
    else:
        passes.append("tier allocation sums to 100%")

    active_rows = [row for row in master if row["symbol"] in active]
    sector_counts = Counter(row["sector"] for row in active_rows)
    sector_ratios = {sector: count / max(len(active_rows), 1) for sector, count in sector_counts.items()}
    sector_breaches = {
        sector: round(ratio, 4)
        for sector, ratio in sector_ratios.items()
        if ratio > max_active_sector_ratio
    }
    if sector_breaches:
        issues.append({"code": "active_sector_concentration", "detail": sector_breaches})
    else:
        passes.append(f"all active sectors <= {max_active_sector_ratio:.0%}")

    themes = theme_hits(master)
    themes_active = {
        theme: [symbol for symbol in symbols if symbol in active]
        for theme, symbols in themes.items()
    }
    theme_ratios = {theme: len(symbols) / max(len(active_rows), 1) for theme, symbols in themes_active.items()}
    theme_breaches = {
        theme: {"ratio": round(ratio, 4), "symbols": themes_active[theme]}
        for theme, ratio in theme_ratios.items()
        if ratio > max_theme_ratio
    }
    if theme_breaches:
        warnings.append({"code": "theme_concentration_active_pool", "detail": theme_breaches})
    else:
        passes.append(f"all active-pool themes <= {max_theme_ratio:.0%}")

    predictions, pred_meta = prediction_quality(master, active)
    low_acc_buy = [
        row for row in predictions
        if row["active"] and row.get("signal") == "buy"
        and row.get("direction_accuracy") is not None
        and row["direction_accuracy"] < low_accuracy_buy_floor
    ]
    if low_acc_buy:
        issues.append({"code": "low_accuracy_active_buy", "detail": low_acc_buy})
    else:
        passes.append(f"no active BUY with direction_accuracy < {low_accuracy_buy_floor:.0%}")

    by_sector = defaultdict(list)
    for row in predictions:
        if row["active"] and row.get("direction_accuracy") is not None:
            by_sector[row["sector"]].append(row)
    weak_active_sectors = {}
    for sector, rows in by_sector.items():
        avg = sum(row["direction_accuracy"] for row in rows) / len(rows)
        has_buy = any(row.get("signal") == "buy" for row in rows)
        if has_buy and avg < low_sector_accuracy_buy_floor:
            weak_active_sectors[sector] = {"avg_accuracy": round(avg, 4), "buy_symbols": [row["symbol"] for row in rows if row.get("signal") == "buy"]}
    if weak_active_sectors:
        issues.append({"code": "weak_sector_active_buy", "detail": weak_active_sectors})
    else:
        passes.append(f"no active sector BUY while sector avg accuracy < {low_sector_accuracy_buy_floor:.0%}")

    active_by_board = Counter(_board(row["symbol"]) for row in active_rows)
    tier_counts = Counter(row["tier"] for row in master)
    result = {
        "audit_time": datetime.now().isoformat(),
        "pool_count": len(master),
        "active_count": len(active_rows),
        "observation_count": len(master) - len(active_rows),
        "tier_counts": dict(tier_counts),
        "active_sector_counts": dict(sector_counts),
        "active_sector_ratios": {k: round(v, 4) for k, v in sector_ratios.items()},
        "board_counts_active": dict(active_by_board),
        "theme_counts_total": {theme: len(symbols) for theme, symbols in themes.items()},
        "theme_counts_active": {theme: len(symbols) for theme, symbols in themes_active.items()},
        "theme_ratios_active": {k: round(v, 4) for k, v in theme_ratios.items()},
        "allocations": allocations,
        "prediction_meta": pred_meta,
        "issues": issues,
        "warnings": warnings,
        "passes": passes,
    }
    if include_correlation:
        result["correlation"] = correlation_summary(master, sorted(active))
        corr_252 = result["correlation"].get("windows", {}).get("252", {})
        if corr_252:
            if corr_252.get("pairs_ge_0_7", 0) > 0:
                warnings.append({
                    "code": "active_high_correlation_pairs",
                    "detail": {
                        "window": 252,
                        "pairs_ge_0_7": corr_252.get("pairs_ge_0_7"),
                        "top_pairs": corr_252.get("top_pairs", [])[:5],
                    },
                })
            else:
                passes.append("no active 252d pair correlation >= 0.70")

    result["issue_count"] = len(issues)
    result["warning_count"] = len(warnings)
    result["pass_count"] = len(passes)
    result["verdict"] = "fail" if issues else ("warn" if warnings else "pass")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="write JSON audit output")
    parser.add_argument("--no-correlation", action="store_true", help="skip K-line correlation scan")
    parser.add_argument("--strict", action="store_true", help="exit non-zero on issues")
    args = parser.parse_args()

    result = run_audit(include_correlation=not args.no_correlation)
    print("=" * 60)
    print("Pool Structure Audit")
    print("=" * 60)
    print(f"verdict={result['verdict']} issues={result['issue_count']} warnings={result['warning_count']} passes={result['pass_count']}")
    print(f"pool={result['pool_count']} active={result['active_count']} observation={result['observation_count']}")
    print(f"active_sector_ratios={result['active_sector_ratios']}")
    if result.get("correlation", {}).get("windows", {}).get("252"):
        corr = result["correlation"]["windows"]["252"]
        print(
            "252d_corr="
            f"avg={corr['avg_pair_corr']} pairs>=0.6={corr['pairs_ge_0_6']} pairs>=0.7={corr['pairs_ge_0_7']}"
        )
    for issue in result["issues"]:
        print(f"ISSUE {issue['code']}: {issue['detail']}")
    for warning in result["warnings"]:
        if isinstance(warning, dict):
            print(f"WARN {warning['code']}: {warning['detail']}")
        else:
            print(f"WARN {warning}")
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {output_path}")
    if args.strict and result["issues"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
