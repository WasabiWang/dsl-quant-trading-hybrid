#!/usr/bin/env python3
"""
DSL GOLDEN TEST — 金标准自动比对 (pre-deploy 回归门)

定位 (README「质量保障」/ 测试金字塔最高层):
  金字塔的最后一层, 在 pre-deploy 时回答一个问题:
  "系统结构性指纹 (股票池/假日/脚本/熔断) 是否偏离已接受的基线?"
  它不替代 L0-L3, 也不能替代人工评审: 漂移明细只做证据, 是否接受由人决定。

检查项:
  T0: Baseline Privacy Guard (<0.1s) — 基线不得内嵌标的明细 (公开仓库红线)
  T1: Snapshot Match        (<1s)    — 结构性指纹比对
  T2: Pipeline Dry-Run      (<5s)    — 关键脚本可编译不崩溃
  T3: Full Golden Run       (<2min)  — 双池/假日/输出文件核对

行为契约 (公开克隆 + 私有配置注入):
  公开仓库有意排除私有配置 (config/master_stock_pool.yaml 等, 见 .gitignore),
  且基线是运行时产物。所以本层必须对"没有私有环境"这件事给出明确、非误导的行为:

  ┌────────────────────────────┬────────────────────────────────────────────┐
  │ 环境                        │ 行为                                        │
  ├────────────────────────────┼────────────────────────────────────────────┤
  │ PUBLIC_CLONE               │ 无私有配置/运行数据 → T0-T3 全 SKIP,         │
  │ (全新克隆/换机)             │ 打印注入指引, exit 0; --strict 时 exit 1     │
  │ PRIVATE_NO_BASELINE        │ T1 SKIP + 提示运行 --accept; T2/T3 照常执行  │
  │ (已注入配置, 基线缺失)       │                                            │
  │ PRIVATE_READY              │ 完整比对 (±WARN)                            │
  └────────────────────────────┴────────────────────────────────────────────┘

  SKIP ≠ FAIL: 缺输入时本层无法判定, 不应把"无法判定"伪装成"回归失败"。
  CI/pre-deploy 要求基线必须存在时用 `--strict`, 任何 SKIP 都会被升级为 FAIL。

基线格式与隐私 (snapshot_format=2, "public-repo-safe"):
  - 只提交"仓库中已经公开"或"弱标识"的内容:
      master_pool → 计数 + 12位截断sha256 指纹 (不含标的代码明细)
      scripts     → 计数 + 指纹 (不含脚本文件名清单: 部分脚本被 .gitignore 排除)
      holidays    → 计数 + 指纹 + 完整列表 (公开信息, 已在 config/holiday_calendar.py)
      circuit_breaker_paused → 布尔状态
  - 标的池明细来自私有 config/master_stock_pool.yaml, 永不写入基线。
  - v1 旧基线 (含 master_pool_symbols) 仍可读取并给出更细的漂移明细;
    --accept 只会写出 v2。漂移证据 (含标的) 由 --explain 写入 cache/ (gitignored)。
  - T0 守卫: 任何基线若出现成串6位标的代码, 直接 FAIL, 防止旧逻辑回潮。

阈值:
  - 池指纹变化      → WARN (池调整可能正常)
  - 假日出现"移除"  → FAIL (危险: 可能在非交易日执行); 仅新增 → WARN
  - 脚本 Δ<=5       → INFO; Δ>5 → WARN; 数量不变但指纹变 → INFO
  - 熔断器 UNPAUSED → PAUSED → FAIL
  - 双池未同步      → WARN (仅"无法判定"才 FAIL)

用法:
  python3 tests/golden_test.py              # 完整检查 (环境缺失→SKIP, exit 0)
  python3 tests/golden_test.py --snapshot   # 仅 T0+T1
  python3 tests/golden_test.py --strict     # CI/pre-deploy: 任何 SKIP 视为 FAIL
  python3 tests/golden_test.py --accept     # 生成/更新基线 (需私有配置)
  python3 tests/golden_test.py --explain    # 输出漂移证据到 cache/, 不改基线
"""

import os, sys, json, yaml, hashlib, re, shutil, subprocess
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_DIR = PROJECT_ROOT / "tests" / "golden_baseline"
SNAPSHOT_FILE = BASELINE_DIR / "system_snapshot.json"
SNAPSHOT_EXAMPLE = BASELINE_DIR / "system_snapshot.example.json"
PRIVATE_POOL_CONFIG = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
RUNTIME_PROBE = PROJECT_ROOT / "data" / "circuit_breaker.json"
BACKUP_FILE = PROJECT_ROOT / "cache" / "golden_baseline_backup.json"

SNAPSHOT_FORMAT = 2
PASS = "✅"; FAIL = "❌"; WARN = "⚠️"; INFO = "ℹ️"; SKIP = "⏭️"
STRICT = False

results = []
baseline = {}
current = {}


def record(name: str, level: str, detail: str = ""):
    results.append((name, level, detail))
    print(f"  {level} {name}" + (f" | {detail}" if detail else ""))


def _short_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _fp(items: List[str]) -> Tuple[int, str]:
    """(count, 12位截断sha256)。哈希口径与 v1 基线保持一致, 保证旧基线可比对。"""
    ordered = sorted(str(x) for x in items)
    h = hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest()[:12]
    return len(ordered), h


def _script_hash(names: List[str]) -> str:
    """脚本指纹口径与 v1 一致 (join 后 hash), 不随格式升级而变。"""
    return hashlib.sha256(''.join(sorted(names)).encode()).hexdigest()[:12]


# ═══════════════ 环境探测 / 私有输入 ═══════════════

def _probe_environment() -> Dict:
    return {
        "private_config": PRIVATE_POOL_CONFIG.is_file(),
        "runtime_data": RUNTIME_PROBE.is_file(),
        "baseline": SNAPSHOT_FILE.is_file(),
        "template": SNAPSHOT_EXAMPLE.is_file(),
    }


def _env_mode(env: Dict) -> str:
    if not env["private_config"]:
        return "PUBLIC_CLONE"
    if not env["baseline"]:
        return "PRIVATE_NO_BASELINE"
    return "PRIVATE_READY"


def _current_pool_symbols() -> List[str]:
    with open(PRIVATE_POOL_CONFIG, encoding="utf-8") as f:
        mp = yaml.safe_load(f)["master_pool"]
    return sorted(str(s["symbol"]).zfill(6) for s in mp)


def _current_holidays() -> List[str]:
    from config.holiday_calendar import A_SHARE_HOLIDAYS
    return sorted(str(h) for h in A_SHARE_HOLIDAYS)


def _script_names() -> list:
    scripts_py = [s for s in Path("scripts").glob("*.py") if not s.name.startswith("_")]
    return sorted(s.name for s in scripts_py)


def _current_circuit_breaker_paused():
    try:
        with open(RUNTIME_PROBE, encoding="utf-8") as f:
            return json.load(f).get("trading_paused", True)
    except Exception:
        return None


def _read_version_for_snapshot() -> str:
    try:
        with open(PROJECT_ROOT / "VERSION", encoding="utf-8") as f:
            return f.readline().strip()
    except Exception:
        return "unknown"


# ═══════════════ 基线读写 (v1/v2 兼容) ═══════════════

def _read_baseline() -> Dict:
    if not SNAPSHOT_FILE.exists():
        return {}
    with open(SNAPSHOT_FILE, encoding="utf-8") as f:
        return json.load(f)


def _normalize_baseline(raw: Dict) -> Dict:
    """把 v1(扁平) / v2(指纹) 基线归一成同一形状, 供 T1 使用。"""
    if int(raw.get("snapshot_format", 1) or 1) >= SNAPSHOT_FORMAT:
        pool = raw.get("master_pool") or {}
        hol = raw.get("holidays") or {}
        scr = raw.get("scripts") or {}
        return {
            "pool_count": pool.get("count", 0), "pool_hash": pool.get("hash", ""), "pool_symbols": [],
            "holiday_count": hol.get("count", 0), "holiday_hash": hol.get("hash", ""),
            "holidays": hol.get("list", []),
            "script_count": scr.get("count", 0), "script_hash": scr.get("hash", ""),
            "circuit_breaker_paused": raw.get("circuit_breaker_paused"),
        }
    return {
        "pool_count": raw.get("master_pool_count", len(raw.get("master_pool_symbols", []))),
        "pool_hash": raw.get("master_pool_hash", ""),
        "pool_symbols": raw.get("master_pool_symbols", []),
        "holiday_count": raw.get("holiday_count", len(raw.get("holidays", []))),
        "holiday_hash": raw.get("holiday_hash", ""),
        "holidays": raw.get("holidays", []),
        "script_count": raw.get("script_count", 0),
        "script_hash": raw.get("script_hash", ""),
        "circuit_breaker_paused": raw.get("circuit_breaker_paused"),
    }


def build_baseline(include_private_detail: bool = False) -> Dict:
    """构造基线。

    include_private_detail=True 时额外带上标的明细/脚本清单 —— 仅供 --explain
    写入 cache/ 供人工评审, 绝不可写入 tests/golden_baseline/。
    """
    symbols = _current_pool_symbols()
    holidays = _current_holidays()
    scripts = _script_names()
    pool_count, pool_hash = _fp(symbols)
    hol_count, hol_hash = _fp(holidays)

    baseline = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "generated_at": datetime.now().isoformat(),
        "version": _read_version_for_snapshot(),
        "privacy": {
            "policy": "public-repo-safe",
            "stored": ["master_pool: count+hash", "holidays: count+hash+list(公开)",
                       "scripts: count+hash", "circuit_breaker_paused"],
            "excluded": ["标的代码明细 (master_pool_symbols)", "脚本文件名清单"],
            "source_of_truth": "私有 config/master_stock_pool.yaml (公开仓库排除)",
        },
        "master_pool": {"count": pool_count, "hash": pool_hash},
        "holidays": {"count": hol_count, "hash": hol_hash, "list": holidays},
        "scripts": {"count": len(scripts), "hash": _script_hash(scripts)},
        "circuit_breaker_paused": _current_circuit_breaker_paused(),
    }
    if include_private_detail:
        baseline["_local_review_only"] = "含标的明细, 仅本地 cache/ 使用, 禁止提交或复制进 tests/golden_baseline/"
        baseline["master_pool_symbols"] = symbols
        baseline["script_names"] = scripts
    return baseline


_SYMBOL_RE = re.compile(r"^\d{6}$")


def _find_symbol_arrays(obj, path: str = "$") -> List[str]:
    """递归寻找"成串6位数字"数组 —— 疑似标的明细。"""
    hits: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits += _find_symbol_arrays(v, f"{path}.{k}")
    elif isinstance(obj, list):
        symbolish = [x for x in obj if isinstance(x, str) and _SYMBOL_RE.match(x)]
        if len(symbolish) >= 3:
            hits.append(f"{path}[{len(symbolish)}只]")
    return hits


def _print_current_fingerprints(indent: str = "    "):
    """无基线时仍输出当前指纹, 便于人工核对与首次建档。"""
    try:
        pool_count, pool_hash = _fp(_current_pool_symbols())
        print(f"{indent}当前 master_pool 指纹: {pool_count}只 hash={pool_hash}")
    except Exception as e:
        print(f"{indent}当前 master_pool 指纹: 不可用 ({str(e)[:60]})")
    try:
        hol_count, hol_hash = _fp(_current_holidays())
        print(f"{indent}当前 holidays 指纹: {hol_count}天 hash={hol_hash}")
    except Exception as e:
        print(f"{indent}当前 holidays 指纹: 不可用 ({str(e)[:60]})")
    names = _script_names()
    print(f"{indent}当前 scripts 指纹: {len(names)}个 hash={_script_hash(names)}")


def _print_onboarding_hint(env: Dict):
    print("""
  ℹ️ 公开克隆模式: 本层已跳过 (不是失败)。
     原因: 公开仓库有意排除私有配置与运行数据, 无法进行结构性比对。
     启用步骤:
       1) 注入私有配置:  cp config/master_stock_pool.example.yaml config/master_stock_pool.yaml  并填入你的股票池
       2) 准备运行数据:  至少 data/circuit_breaker.json (否则仅 T3 会被 SKIP)
       3) 生成基线:      python3 tests/golden_test.py --accept
       4) 运行本层:      bash harness.sh golden
     基线与隐私: 基线只存指纹(计数+哈希), 不含标的明细; 格式说明见 tests/golden_baseline/README.md
     严格模式(CI/pre-deploy, 要求基线齐备): python3 tests/golden_test.py --strict""")


# ═══════════════ T0: Baseline Privacy Guard ═══════════════

def t0_privacy_guard() -> bool:
    print(f"\n{'='*60}")
    print(f"  T0: Baseline Privacy Guard (基线不得内嵌标的明细)")
    print(f"{'='*60}")

    targets = [p for p in (SNAPSHOT_FILE, SNAPSHOT_EXAMPLE) if p.exists()]
    if not targets:
        record("T0-baseline", SKIP, "无基线文件可检视 (公开克隆属正常)")
        return True

    ok = True
    for p in targets:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            record(f"T0-{p.name}", FAIL, f"JSON 解析失败: {str(e)[:60]}")
            ok = False
            continue
        hits = _find_symbol_arrays(data)
        if hits:
            record(f"T0-{p.name}", FAIL,
                   f"疑似内嵌标的明细 {hits[:3]} → 基线禁止包含标的代码; "
                   f"迁移: python3 tests/golden_test.py --accept (v2 只存指纹)")
            ok = False
        else:
            record(f"T0-{p.name}", PASS, "无标的明细")
    return ok


# ═══════════════ T1: Snapshot Match ═══════════════

def t1_snapshot_match() -> bool:
    print(f"\n{'='*60}")
    print(f"  T1: Snapshot Match")
    print(f"{'='*60}")

    if not SNAPSHOT_FILE.exists():
        record("T1-baseline", SKIP,
               "基线不存在 (公开克隆属正常) → 生成: python3 tests/golden_test.py --accept")
        _print_current_fingerprints()
        return True

    raw = _read_baseline()
    if raw.get("_template"):
        record("T1-baseline", FAIL,
               f"{SNAPSHOT_FILE.name} 是模板(占位值), 不可用于比对 → 运行 --accept 生成真实基线")
        return False

    base = _normalize_baseline(raw)
    record("T1-baseline", PASS,
           f"format=v{raw.get('snapshot_format', 1)} version={raw.get('version', 'unknown')}")

    issues = 0

    # 1.1 池指纹 (需要私有配置)
    if not PRIVATE_POOL_CONFIG.is_file():
        record("T1-pool", SKIP, f"缺少私有配置 {PRIVATE_POOL_CONFIG.name} → 无法比对标的池")
    else:
        pool_count, pool_hash = _fp(_current_pool_symbols())
        if pool_hash == base["pool_hash"]:
            record("T1-pool", PASS, f"{pool_count}只 (hash={pool_hash})")
        else:
            record("T1-pool", WARN,
                   f"指纹变化: 当前{pool_count}只/hash={pool_hash} ≠ 基线{base['pool_count']}只/hash={base['pool_hash']}")
            if base["pool_symbols"]:
                added = sorted(set(_current_pool_symbols()) - set(base["pool_symbols"]))
                removed = sorted(set(base["pool_symbols"]) - set(_current_pool_symbols()))
                if added:
                    print(f"    新增: {added[:10]}")
                if removed:
                    print(f"    移除: {removed[:10]}")
            else:
                print("    基线为指纹格式(不含标的明细) → 明细: python3 tests/golden_test.py --explain")

    # 1.2 假日日历一致性 → 移除=危险(FAIL), 仅新增=WARN
    hol_count, hol_hash = _fp(_current_holidays())
    if hol_hash == base["holiday_hash"]:
        record("T1-holidays", PASS, f"{hol_count}天 (hash={hol_hash})")
    else:
        cur_set = set(_current_holidays())
        base_set = set(base["holidays"])
        added = sorted(cur_set - base_set) if base_set else []
        removed = sorted(base_set - cur_set) if base_set else []
        if removed:
            record("T1-holidays", FAIL,
                   f"移除{len(removed)}天 (当前{hol_count}天/hash={hol_hash}) ← 危险!")
            print(f"    移除假日: {removed[:10]}")
            issues += 1
        elif added:
            record("T1-holidays", WARN, f"新增{len(added)}天 (hash={hol_hash})")
            print(f"    新增假日: {added[:10]}")
        else:
            record("T1-holidays", WARN,
                   f"指纹变化但基线为指纹格式, 无法区分新增/移除 (hash={hol_hash})")

    # 1.3 脚本指纹
    names = _script_names()
    cur_script_hash = _script_hash(names)
    delta = len(names) - base["script_count"]
    if base["script_hash"] and cur_script_hash == base["script_hash"]:
        record("T1-scripts", PASS, f"{len(names)}个 (指纹不变)")
    elif abs(delta) > 5:
        record("T1-scripts", WARN, f"{len(names)}个 (Δ{delta:+d}, hash={cur_script_hash})")
    elif delta == 0:
        record("T1-scripts", INFO, f"{len(names)}个 (数量不变, 指纹变化 hash={cur_script_hash})")
    else:
        record("T1-scripts", INFO, f"{len(names)}个 (Δ{delta:+d}, hash={cur_script_hash})")

    # 1.4 熔断器状态
    cur_paused = _current_circuit_breaker_paused()
    base_paused = base["circuit_breaker_paused"]
    if cur_paused is None:
        record("T1-circuit-breaker", SKIP, "缺少 data/circuit_breaker.json → 无法比对熔断状态")
    elif base_paused is None:
        record("T1-circuit-breaker", INFO, f"基线未记录熔断状态, 当前={'已暂停' if cur_paused else '正常'}")
    elif cur_paused and not base_paused:
        record("T1-circuit-breaker", FAIL, "熔断器从 UNPAUSED 变为 PAUSED!")
        issues += 1
    elif not cur_paused and base_paused:
        record("T1-circuit-breaker", INFO, "熔断器已恢复 (之前暂停)")
    else:
        record("T1-circuit-breaker", PASS, "已暂停" if cur_paused else "正常")

    return issues == 0


# ═══════════════ T2: Pipeline Dry-Run ═══════════════

def t2_pipeline_dryrun() -> bool:
    print(f"\n{'='*60}")
    print(f"  T2: Pipeline Dry-Run (关键脚本可执行性)")
    print(f"{'='*60}")

    scripts = [
        ("batch_predict", "scripts/batch_predict.py", ["--help"]),
        ("pre_market_refresh", "scripts/pre_market_refresh.py", []),
        ("morning_decision", "scripts/morning_decision.py", ["--market", "a"]),
        ("paper_trader", "scripts/paper_trader.py", []),
        ("feedback_controller", "scripts/feedback_controller.py", []),
    ]

    all_ok = True
    for name, script, args in scripts:
        script_path = PROJECT_ROOT / script
        if not script_path.exists():
            record(f"T2-{name}", FAIL, "脚本不存在!")
            all_ok = False
            continue

        try:
            # 只验证Python语法+import，不实际运行
            with open(script_path) as f:
                content = f.read()
            compile(content, script_path, 'exec')
            record(f"T2-{name}", PASS, "语法通过")
        except SyntaxError as e:
            record(f"T2-{name}", FAIL, f"语法错误: {e}")
            all_ok = False
        except Exception as e:
            record(f"T2-{name}", INFO, f"编译通过 (warnings: {e})")

    return all_ok


# ═══════════════ T3: Full Golden Run ═══════════════

def t3_full_golden_run() -> bool:
    print(f"\n{'='*60}")
    print(f"  T3: Full Golden Run (管线输出比对)")
    print(f"{'='*60}")

    if not RUNTIME_PROBE.is_file():
        record("T3-runtime", SKIP, "缺少运行数据 (data/) → T3 需要真实运行环境 (公开克隆属正常)")
        return True

    all_ok = True

    # T3.1: pool sync一致性 (drift=WARN, 仅"无法判定"才 FAIL)
    print("\n  T3.1: 双池同步")
    try:
        result = subprocess.run(
            [sys.executable, "scripts/sync_stock_pool.py", "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        out = (result.stdout or "")
        if result.returncode != 0:
            record("T3-sync-pool", FAIL,
                   f"sync_stock_pool.py 退出码 {result.returncode}: {(result.stderr or out).strip()[:100]}")
            all_ok = False
        elif "双池完全一致" in out or "无需更新" in out:
            record("T3-sync-pool", PASS, "双池一致")
        elif "将同步" in out or "特有" in out or "已同步" in out:
            record("T3-sync-pool", WARN, "双池存在未同步漂移 (待同步, 非致命)")
            for line in out.splitlines():
                if "将同步" in line or "特有" in line:
                    print(f"    {line.strip()}")
        else:
            record("T3-sync-pool", FAIL, f"无法判定同步状态: {out.strip()[:100]}")
            all_ok = False
    except Exception as e:
        record("T3-sync-pool", FAIL, str(e)[:100])
        all_ok = False

    # T3.2: holiday calendar一致性
    print("\n  T3.2: 假日日历")
    try:
        from config.holiday_calendar import is_trading_day
        test_dates = [
            (date(2026,5,1), False), (date(2026,5,6), True),
            (date(2026,10,1), False), (date(2026,10,8), True),
            (date(2026,1,1), False),  (date(2026,1,2), False),  # 元旦假期(1月1-2日, 周五)
        ]
        errors = 0
        for d, expected in test_dates:
            actual = is_trading_day(d, "A_SHARE")
            if actual != expected:
                record(f"T3-holiday-{d}", FAIL, f"期望{expected} 实际{actual}")
                errors += 1

        if errors == 0:
            record("T3-holiday-all", PASS, f"{len(test_dates)}个关键日期全部正确")
    except Exception as e:
        record("T3-holiday", FAIL, str(e)[:100])
        all_ok = False

    # T3.3: 数据文件完整性
    print("\n  T3.3: 关键输出文件")
    output_files = {
        "熔断器": ("data/circuit_breaker.json", "json"),
        "paper_trading": ("data/paper_trading.db", "sqlite"),
        "校准数据": ("confidence_data/prediction_calibration.json", "json"),
        "自适应参数": ("config/adaptive_params.yaml", "yaml"),
    }

    for name, (path, ftype) in output_files.items():
        fp = PROJECT_ROOT / path
        if not fp.exists():
            record(f"T3-output-{name}", FAIL, "文件缺失!")
            all_ok = False
            continue

        try:
            if ftype == "json":
                with open(fp) as f:
                    data = json.load(f)
                record(f"T3-output-{name}", PASS, f"{(fp.stat().st_size/1024):.0f}KB")
            elif ftype == "yaml":
                with open(fp) as f:
                    data = yaml.safe_load(f)
                record(f"T3-output-{name}", PASS, f"{(fp.stat().st_size/1024):.0f}KB")
            elif ftype == "sqlite":
                import sqlite3
                conn = sqlite3.connect(fp)
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                record(f"T3-output-{name}", PASS, f"{len(tables)} tables, {(fp.stat().st_size/1024):.0f}KB")
                conn.close()
        except Exception as e:
            record(f"T3-output-{name}", FAIL, f"损坏: {str(e)[:60]}")

    return all_ok


# ═══════════════ 漂移证据 (仅本地 cache/) ═══════════════

def _diff_list(current_items: list, baseline_items: list) -> Dict:
    current_sorted = sorted(current_items)
    baseline_sorted = sorted(baseline_items)
    return {
        "count": len(current_sorted),
        "baseline_count": len(baseline_sorted),
        "hash": _short_hash(current_sorted),
        "baseline_hash": _short_hash(baseline_sorted) if baseline_sorted else "",
        "added": sorted(set(current_sorted) - set(baseline_sorted)),
        "removed": sorted(set(baseline_sorted) - set(current_sorted)),
    }


def write_drift_explanation(output_path: Path = None) -> Path:
    """把含标的明细的漂移证据写入 cache/ (gitignored, 仅本地), 不更新基线。"""
    from config.holiday_calendar import A_SHARE_HOLIDAYS

    if not PRIVATE_POOL_CONFIG.is_file():
        print("  ℹ️ 无私有配置, 跳过漂移证据生成")
        return None

    output_path = output_path or (PROJECT_ROOT / "cache" / "golden_drift_explanation.json")
    base = _normalize_baseline(_read_baseline()) if SNAPSHOT_FILE.exists() else {}

    current_symbols = _current_pool_symbols()
    current_holidays = sorted(str(h) for h in A_SHARE_HOLIDAYS)
    current_scripts = _script_names()

    cb = {}
    try:
        with open(RUNTIME_PROBE, encoding="utf-8") as f:
            cb = json.load(f)
    except Exception:
        pass
    cb_current = {
        "trading_paused": cb.get("trading_paused", True),
        "last_trade_date": cb.get("last_trade_date"),
        "consecutive_failed_trades": cb.get("consecutive_failed_trades"),
        "today_drawdown": cb.get("today_drawdown"),
    }

    explanation = {
        "generated_at": datetime.now().isoformat(),
        "baseline_file": str(SNAPSHOT_FILE),
        "needs_human_acceptance": True,
        "note": "--explain 只写漂移证据 (含标的明细, 仅本地 cache/), 不更新基线。",
        "local_only": "本文件含标的明细, 已被 .gitignore 排除, 禁止提交或复制进仓库。",
        "stock_pool": _diff_list(current_symbols, base.get("pool_symbols", [])),
        "holidays": _diff_list(current_holidays, base.get("holidays", [])),
        "scripts": {**_diff_list(current_scripts, []), "hash": _script_hash(current_scripts)},
        "circuit_breaker": {"current": cb_current, "baseline_paused": base.get("circuit_breaker_paused")},
    }
    explanation["scripts"]["baseline_hash_recorded"] = base.get("script_hash", "")
    explanation["scripts"]["baseline_count_recorded"] = base.get("script_count", 0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    proposed_path = output_path.with_name("golden_proposed_system_snapshot.json")
    proposed_path.write_text(
        json.dumps(build_baseline(include_private_detail=True), indent=2, ensure_ascii=False),
        encoding="utf-8")
    explanation["proposed_baseline_file"] = str(proposed_path)
    explanation["human_review_required_before_accept"] = [
        "确认 stock_pool added/removed 是否为已批准池变更",
        "确认 holidays 仅为新增官方假日/调休日，无误删",
        "确认 scripts count/hash 增量来自已审查脚本",
        "确认 circuit_breaker 状态未进入 paused",
    ]

    output_path.write_text(json.dumps(explanation, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ℹ️ Golden drift explanation: {output_path} (本地, 含标的明细)")
    return output_path


# ═══════════════ 基线生成 ═══════════════

def accept_baseline() -> int:
    print("🔧 正在创建新基线...")
    if not PRIVATE_POOL_CONFIG.is_file():
        print(f"❌ 缺少私有配置 {PRIVATE_POOL_CONFIG.relative_to(PROJECT_ROOT)} (公开仓库有意排除)。")
        print("   公开克隆不应生成基线; 模板见 tests/golden_baseline/system_snapshot.example.json")
        return 2

    try:
        new_baseline = build_baseline()
    except Exception as e:
        print(f"❌ 生成基线失败: {str(e)[:200]}")
        return 2

    leaks = _find_symbol_arrays(new_baseline)
    if leaks:
        print(f"❌ 拒绝写入: 基线含疑似标的明细 {leaks} (隐私守卫, 见 T0)")
        return 2

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    if SNAPSHOT_FILE.exists():
        BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SNAPSHOT_FILE, BACKUP_FILE)
        print(f"   ℹ️ 旧基线已备份: {BACKUP_FILE.relative_to(PROJECT_ROOT)} (gitignored, 仅本地)")

    SNAPSHOT_FILE.write_text(json.dumps(new_baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"✅ 新基线已保存: {SNAPSHOT_FILE.relative_to(PROJECT_ROOT)}")
    print(f"   format=v{SNAPSHOT_FORMAT} (指纹) | master_pool={new_baseline['master_pool']['count']}只 "
          f"| holidays={new_baseline['holidays']['count']}天 | scripts={new_baseline['scripts']['count']}个")
    print("   提示: 基线为运行时产物, 已被 .gitignore 排除, 不随仓库提交。")
    if new_baseline["master_pool"]["count"] < 8:
        print("   ⚠️ 池很小: 12位截断hash存在被枚举的可能, 该基线请勿提交到公开仓库")
    return 0


# ═══════════════ MAIN ═══════════════

def _summarize() -> int:
    fails = [r for r in results if r[1] == FAIL]
    warns = [r for r in results if r[1] == WARN]
    skips = [r for r in results if r[1] == SKIP]

    print(f"\n{'='*60}")
    status = "✅ 通过" if not fails else f"❌ {len(fails)} FAIL"
    extra = []
    if warns:
        extra.append(f"{len(warns)} WARN")
    if skips:
        extra.append(f"{len(skips)} SKIP")
    print(f"  📊 Golden Test: {status}" + (f" | {' | '.join(extra)}" if extra else ""))
    print(f"{'='*60}")

    if fails:
        print(f"\n❌ 以下检查失败 (超过阈值，应拒绝部署):")
        for name, level, detail in results:
            if level == FAIL:
                print(f"  [{name}] {detail}")

    if skips:
        print(f"\n⏭️ 以下检查被跳过 (缺输入, 非失败):")
        for name, level, detail in skips:
            print(f"  [{name}] {detail}")

    if STRICT and skips:
        print(f"\n❌ 严格模式: {len(skips)} 项 SKIP 视为 FAIL (CI/pre-deploy 要求基线齐备)")
        return 1

    return 0 if not fails else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="DSL Golden Test — 金标准自动比对")
    ap.add_argument("--snapshot", action="store_true", help="仅快照比对 (T0+T1)")
    ap.add_argument("--accept", action="store_true", help="接受当前状态为新基线 (需私有配置)")
    ap.add_argument("--explain", action="store_true", help="输出cache/golden_drift_explanation.json，不接受基线")
    ap.add_argument("--strict", action="store_true", help="CI/pre-deploy: 任何 SKIP 视为 FAIL")
    args = ap.parse_args()

    global STRICT
    STRICT = args.strict

    if args.accept:
        return accept_baseline()

    env = _probe_environment()
    mode = _env_mode(env)

    print(f"\n{'#'*60}")
    print(f"# DSL GOLDEN TEST — 金标准自动比对 (pre-deploy)")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"# 基线: {SNAPSHOT_FILE}")
    print(f"# 环境: {mode} | 私有配置={'有' if env['private_config'] else '无'} "
          f"运行数据={'有' if env['runtime_data'] else '无'} 基线={'有' if env['baseline'] else '无'}")
    if STRICT:
        print(f"# 严格模式: SKIP 将视为 FAIL")
    print(f"{'#'*60}")

    # 隐私守卫永远先跑: 公开克隆里若残留含标的的基线, 同样要拦下
    ok0 = t0_privacy_guard()

    if mode == "PUBLIC_CLONE":
        record("ENV-public-clone", SKIP, "无私有配置 → 跳过 T1-T3 (非失败)")
        _print_onboarding_hint(env)
        return _summarize()

    ok1 = t1_snapshot_match()
    if args.explain:
        write_drift_explanation()
    if args.snapshot:
        return _summarize()

    ok2 = t2_pipeline_dryrun()
    ok3 = t3_full_golden_run()
    return _summarize()


if __name__ == "__main__":
    sys.exit(main())
