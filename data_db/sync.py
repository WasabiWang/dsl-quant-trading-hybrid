"""
增量同步引擎 — 全市场A股日线数据
设计参考 free-stockdb 的 updater.cpp: manifest + SHA-256 + 增量

流程:
  1. 读取同步状态 → 计算需要更新的日期
  2. 从 akshare 逐批拉取
  3. SHA-256 校验后写入 Parquet
  4. 写同步状态文件（等价于 manifest.txt）
"""
import os, json, hashlib, time, socket, signal
import pandas as pd
import akshare as ak
import baostock as bs
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    DATA_ROOT, DAILY_DIR, META_DIR, SYNC_DIR,
    INITIAL_YEARS, MAX_RETRIES, SYNC_WORKERS,
)
from .store import write_kline, read_kline, get_local_data_range

# ======================================================================
# 同步状态管理（等价于 free-stockdb 的 manifest.txt）
# ======================================================================

SYNC_STATE_FILE = os.path.join(SYNC_DIR, "sync_state.json")
STOCK_LIST_FILE = os.path.join(META_DIR, "stock_list.parquet")
SECTOR_FILE = os.path.join(META_DIR, "sector_map.parquet")
MANIFEST_FILE = os.path.join(SYNC_DIR, "manifest.json")


def _load_sync_state() -> dict:
    default = {
        "last_full_sync": None,       # YYYY-MM-DD
        "last_incremental_sync": None,  # YYYY-MM-DD
        "stocks_synced": 0,
        "stocks_total": 0,
        "errors": [],
    }
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, "r") as f:
                return {**default, **json.load(f)}
        except Exception:
            pass
    return default


def _save_sync_state(state: dict):
    os.makedirs(SYNC_DIR, exist_ok=True)
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _update_manifest(code: str, df: pd.DataFrame):
    """更新单个股票的 manifest 条目（SHA-256 + 记录数 + 日期范围）"""
    if not os.path.exists(MANIFEST_FILE):
        manifest = {}
    else:
        try:
            with open(MANIFEST_FILE, "r") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    code_key = f"{code}"
    if df is not None and not df.empty:
        df_str = df.to_json(orient="records")
        sha = hashlib.sha256(df_str.encode()).hexdigest()
        dates = df["date"].dropna().unique()
        manifest[code_key] = {
            "sha256": sha,
            "rows": len(df),
            "date_min": str(sorted(dates)[0]) if len(dates) else None,
            "date_max": str(sorted(dates)[-1]) if len(dates) else None,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    os.makedirs(os.path.dirname(MANIFEST_FILE), exist_ok=True)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


# ======================================================================
# 股票列表同步
# ======================================================================

def sync_stock_list() -> pd.DataFrame:
    """同步全A股股票代码和名称列表"""
    try:
        df = ak.stock_info_a_code_name()
        df.columns = [c.lower().strip() for c in df.columns]
        if "code" in df.columns and "name" in df.columns:
            # 加后缀标准化
            df["suffix_code"] = df["code"].apply(
                lambda c: f"{c}.SH" if str(c).startswith(("6", "9"))
                else f"{c}.SZ" if str(c).startswith(("0", "3", "2"))
                else f"{c}.BJ"
            )
        df.to_parquet(STOCK_LIST_FILE, compression="zstd")
        print(f"  ✅ 股票列表同步: {len(df)} 只")
        return df
    except Exception as e:
        print(f"  ❌ 股票列表同步失败: {e}")
        if os.path.exists(STOCK_LIST_FILE):
            return pd.read_parquet(STOCK_LIST_FILE)
        return pd.DataFrame()


def load_stock_list() -> pd.DataFrame:
    """加载本地股票列表"""
    if os.path.exists(STOCK_LIST_FILE):
        return pd.read_parquet(STOCK_LIST_FILE)
    return pd.DataFrame()


class _BaostockTimeout(Exception):
    """baostock 单次操作硬超时(SIGALRM 触发)。CLOSE_WAIT 下 baostock 内部 recv
    死循环永不返回, socket 超时与行级看门狗均无法中断, 只能靠信号强杀 (2026-08-11 实测)"""
    pass


def _baostock_alarm_handler(signum, frame):
    raise _BaostockTimeout("baostock 操作超时(60s SIGALRM 硬性保护)")


# ======================================================================
# 单只股票数据同步
# ======================================================================

def _sync_single_stock(code: str, name: str = "",
                       start: Optional[str] = None,
                       end: Optional[str] = None,
                       force: bool = False) -> dict:
    """同步单只股票的历史日线

    Args:
        code: 纯数字股票代码
        name: 股票名称（仅日志）
        start: 起始日期，None=根据已有数据计算
        end: 结束日期，None=今天
        force: 是否强制重新下载

    Returns:
        {"code": code, "status": "ok"|"skip"|"error", "rows": int, "msg": str}
    """
    result = {"code": code, "status": "skip", "rows": 0, "msg": ""}

    try:
        # 确定日期范围
        if end is None:
            end = datetime.now().strftime("%Y-%m-%d")
        if start is None:
            if not force:
                local_range = get_local_data_range(code)
                if local_range:
                    # 已有数据 → 从最后日期+1天开始增量
                    last_date = local_range[1]
                    next_date = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                    if next_date >= end:
                        return result  # 已是最新，跳过
                    start = next_date
                else:
                    start = (datetime.now() - timedelta(days=INITIAL_YEARS * 365)).strftime("%Y-%m-%d")
            else:
                start = (datetime.now() - timedelta(days=INITIAL_YEARS * 365)).strftime("%Y-%m-%d")

        # 从 baostock 下载（比 akshare 稳定，不受东财封锁影响）
        # socket 超时保护: baostock 无内置超时，服务端偶发不响应会无限挂起（2026-08-10 实测 CLOSE_WAIT 卡死）
        socket.setdefaulttimeout(30)
        market = "sh" if str(code).startswith(("6", "9")) else "sz"
        bs_code = f"{market}.{code}"

        df = None
        # SIGALRM 硬超时: baostock 内部 recv 死循环(CLOSE_WAIT)无法被 socket 超时/
        # 行级看门狗中断, 只能信号强杀。每只股票 60s 封顶 (2026-08-11 实测修复)
        _old_alarm_handler = signal.signal(signal.SIGALRM, _baostock_alarm_handler)
        try:
            for attempt in range(MAX_RETRIES):
                signal.alarm(60)  # 硬性保护: 单次尝试 60s 封顶
                try:
                    lg = bs.login()
                    if lg.error_code != "0":
                        raise ConnectionError(f"baostock 登录失败: {lg.error_msg}")

                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,code,open,high,low,close,volume,amount,pctChg",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="2",  # 前复权
                    )
                    rows = []
                    # CLOSE_WAIT 卡死保护 (2026-08-11): 服务端关闭连接后 recv 立即返回 EOF,
                    # rs.next() 可能空转返回 True 造成 100% CPU 死循环; socket 超时对此无效。
                    # 用"有新行才刷新计时"的停滞看门狗兜底(通常比 SIGALRM 更早触发)。
                    _stall_limit = 30.0  # 秒, 无新数据行即判定卡死
                    _last_row_ts = time.time()
                    while (rs.error_code == "0") & rs.next():
                        _row = rs.get_row_data()
                        if not _row:
                            if time.time() - _last_row_ts > _stall_limit:
                                raise ConnectionError("baostock 数据流卡死(rs.next 空转, 疑似 CLOSE_WAIT)")
                            continue
                        rows.append(_row)
                        _last_row_ts = time.time()
                    bs.logout()
                    signal.alarm(0)  # 成功: 取消闹钟

                    if not rows:
                        result["msg"] = "无数据返回"
                        return result

                    df = pd.DataFrame(rows, columns=rs.fields)
                    break

                except Exception as e:
                    try:
                        bs.logout()
                    except Exception:
                        pass
                    signal.alarm(0)  # 失败: 取消闹钟, 准备重试/返回
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(2 ** attempt)
                        continue
                    result["status"] = "error"
                    result["msg"] = f"下载失败({attempt+1}次): {e}"
                    return result
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_alarm_handler)

        # 标准化：baostock 的列名已经是英文
        df.columns = [c.lower().strip() for c in df.columns]

        # 数值列转换
        for col in ["open", "high", "low", "close", "volume", "amount", "pctchg"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

        # baostock还有换手率
        if "turn" in df.columns:
            df["turn"] = pd.to_numeric(df["turn"], errors="coerce").fillna(0.0)

        # baostock 的 code 列是 "sh.600519" 格式，不需要

        # 写入本地
        ok = write_kline(df, code, append=(not force))
        if ok:
            rows = len(df)
            result["status"] = "ok"
            result["rows"] = rows
            result["msg"] = f"{rows}行({start}~{end})"
            # 写 manifest
            _update_manifest(code, read_kline(code))
        else:
            result["status"] = "error"
            result["msg"] = "写入失败"

    except Exception as e:
        result["status"] = "error"
        result["msg"] = str(e)

    return result


# ======================================================================
# 批量同步
# ======================================================================

def sync_daily(codes: Optional[List[str]] = None,
               workers: int = 1,  # baostock 非线程安全，强制顺序
               force: bool = False) -> dict:
    """增量同步 — 只更新缺失/变更的数据

    Args:
        codes: 股票代码列表，None=同步全市场
        workers: 并行数
        force: 强制全量重下载

    Returns:
        {"total": N, "ok": N, "skip": N, "error": N, "details": [...]}
    """
    state = _load_sync_state()
    end = datetime.now().strftime("%Y-%m-%d")
    today = end

    # 如果是非交易日或已有当日增量，跳过
    if not force and state.get("last_incremental_sync") == today:
        print(f"  ⏭️  今日({today})已同步过，跳过（force=True可强制）")
        return {"total": 0, "ok": 0, "skip": 0, "error": 0, "details": []}

    # 获取股票列表
    if codes is None:
        stock_df = load_stock_list()
        if stock_df.empty:
            stock_df = sync_stock_list()
        codes = stock_df["code"].tolist() if "code" in stock_df.columns else []
        names = dict(zip(stock_df["code"], stock_df["name"])) if "name" in stock_df.columns else {}
    else:
        names = {c: "" for c in codes}

    if not codes:
        print("  ❌ 无股票列表，请先 sync_stock_list()")
        return {"total": 0, "ok": 0, "skip": 0, "error": 0, "details": []}

    print(f"  📥 同步 {len(codes)} 只股票...")

    results = []
    done = 0
    total = len(codes)

    # baostock 非线程安全，顺序执行
    for code in codes:
        r = _sync_single_stock(code, names.get(code, ""), None, end, force)
        results.append(r)
        done += 1
        if r["status"] == "ok":
            print(f"  [{done}/{total}] ✅ {code} {names.get(code,'')}: +{r['rows']}行 ({r.get('msg','')})")
        elif r["status"] == "error":
            print(f"  [{done}/{total}] ❌ {code}: {r['msg']}")

    # 统计
    ok_count = sum(1 for r in results if r["status"] == "ok")
    skip_count = sum(1 for r in results if r["status"] == "skip")
    error_count = sum(1 for r in results if r["status"] == "error")

    summary = {
        "total": len(results),
        "ok": ok_count,
        "skip": skip_count,
        "error": error_count,
        "errors": [r for r in results if r["status"] == "error"],
        "details": results,
    }

    # 更新同步状态
    state["last_incremental_sync"] = today
    state["stocks_synced"] = len([r for r in results if r["status"] in ("ok", "skip")])
    state["stocks_total"] = len(results)
    state["errors"] = [r["msg"] for r in results if r["status"] == "error"][:20]
    _save_sync_state(state)

    print(f"\n  📊 结果: 新增{ok_count} / 跳过{skip_count} / 失败{error_count} / 共{len(results)}")
    return summary


# ======================================================================
# 全量首次同步（分批次，适合初次部署）
# ======================================================================

def full_sync(codes: Optional[List[str]] = None, batch_size: int = 500) -> dict:
    """全量首次同步，按每批 batch_size 只逐步推进

    Args:
        codes: 股票列表，None=全市场
        batch_size: 每批股票数

    Returns:
        汇总 dict
    """
    state = _load_sync_state()
    start_time = datetime.now()

    if codes is None:
        stock_df = sync_stock_list()
        if "code" in stock_df.columns:
            codes = stock_df["code"].tolist()
        else:
            return {"error": "无法获取股票列表"}

    total_all = len(codes)
    print(f"📦 首次全量同步: {total_all} 只股票，每批{batch_size}只")
    print(f"   预计首次约需 {total_all // 20} 秒（~{total_all//20//60:.0f}分钟）")

    cumulative = {"total": 0, "ok": 0, "skip": 0, "error": 0}
    for i in range(0, total_all, batch_size):
        batch = codes[i:i + batch_size]
        print(f"\n--- 批次 {i//batch_size + 1}/{(total_all-1)//batch_size + 1} ({len(batch)}只) ---")
        summary = sync_daily(codes=batch, force=True, workers=SYNC_WORKERS)
        for k in cumulative:
            cumulative[k] += summary.get(k, 0)

    elapsed = (datetime.now() - start_time).total_seconds()
    state["last_full_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_sync_state(state)

    print(f"\n{'='*50}")
    print(f"🎉 全量同步完成! {elapsed:.0f}秒 ({elapsed/60:.1f}分钟)")
    print(f"   成功: {cumulative['ok']} / 跳过: {cumulative['skip']} / 失败: {cumulative['error']} / 总数: {len(codes)}")
    print(f"{'='*50}")
    return cumulative


# ======================================================================
# 完整性校验（等价于 free-stockdb verify_synced_files）
# ======================================================================

def verify_integrity() -> dict:
    """校验所有本地 Parquet 文件的完整性

    检查: 文件存在性、SHA-256、日期连续性

    Returns:
        {"total": N, "ok": N, "failed": N, "details": [...]}
    """
    if not os.path.exists(MANIFEST_FILE):
        print("  ⚠️  无 manifest 文件，请先运行 sync")
        return {"total": 0, "ok": 0, "failed": 0, "details": []}

    with open(MANIFEST_FILE, "r") as f:
        manifest = json.load(f)

    results = []
    ok = 0
    failed = 0

    for code, expected in manifest.items():
        df = read_kline(code)
        if df is None or df.empty:
            results.append({"code": code, "status": "failed", "reason": "文件不存在或为空"})
            failed += 1
            continue

        # SHA-256 校验
        df_str = df.to_json(orient="records")
        actual_sha = hashlib.sha256(df_str.encode()).hexdigest()
        if actual_sha != expected.get("sha256"):
            results.append({"code": code, "status": "failed", "reason": "SHA-256不匹配"})
            failed += 1
            continue

        results.append({"code": code, "status": "ok",
                        "rows": len(df), "range": f"{expected.get('date_min')}~{expected.get('date_max')}"})
        ok += 1

    print(f"  校验: {ok}通过 / {failed}失败 / {len(manifest)}总数")
    return {"total": len(manifest), "ok": ok, "failed": failed, "details": results}


# ======================================================================
# CLI 入口
# ======================================================================

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "stock-list":
        sync_stock_list()
    elif cmd == "daily":
        codes = sys.argv[2].split(",") if len(sys.argv) > 2 else None
        force = "--force" in sys.argv
        sync_daily(codes=codes, force=force)
    elif cmd == "full":
        codes = sys.argv[2].split(",") if len(sys.argv) > 2 else None
        full_sync(codes=codes)
    elif cmd == "verify":
        verify_integrity()
    elif cmd == "status":
        state = _load_sync_state()
        for k, v in state.items():
            if k != "errors":
                print(f"  {k}: {v}")
        if state.get("errors"):
            print(f"  errors ({len(state['errors'])}): {state['errors'][:5]}")
        from .store import get_local_stock_count
        print(f"  local_stocks: {get_local_stock_count()}")
    else:
        print("""
用法: python -m data_db.sync <command> [options]

Commands:
  stock-list        同步A股代码列表
  daily             增量同步（默认全市场，可指定 codes）
    [code1,code2]    只同步指定股票
    --force          强制全量重下载
  full              首次全量同步
    [code1,code2]    只同步指定股票
  verify            校验本地数据完整性
  status            查看同步状态
""")
