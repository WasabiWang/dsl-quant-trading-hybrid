#!/usr/bin/env python3
"""
DSL量化交易系统 - Web Dashboard Server
FastAPI后端，提供REST API + 静态文件服务 + WebSocket进度推送
"""
import json, os, sys, asyncio, time, fcntl, hmac, hashlib, base64, secrets, functools
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger("web_dashboard.server")

# 线程池：所有同步阻塞I/O (文件读写、SQLite、网络请求) 必须在此执行，禁止阻塞事件循环
_BLOCKING_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="blocking-io")

# ── 添加项目根目录 ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _read_version_info() -> dict:
    """Read VERSION as the single source of truth for API and UI fallbacks."""
    version_data = {"version": "v0.0.0", "build": "unknown", "codename": "unknown"}
    vpath = os.path.join(PROJECT_ROOT, "VERSION")
    try:
        with open(vpath, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if lines and lines[0].startswith("v"):
            version_data["version"] = lines[0]
        for line in lines[1:]:
            if line.startswith("build:"):
                version_data["build"] = line.split(":", 1)[1].strip()
            elif line.startswith("codename:"):
                version_data["codename"] = line.split(":", 1)[1].strip()
    except Exception:
        pass
    return version_data


APP_VERSION = _read_version_info()

# ── 导入数据适配器 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_adapter import (
    get_full_dashboard, get_system_status, get_stock_pool,
    get_predictions, get_calibration, get_blackswan,
    get_portfolio, get_trading_ledger, get_adaptive_params,
    get_progress, get_cron_status, get_training_summary,
    get_threshold_engine, get_pipeline_timeline, get_task_reports,
    get_pool_history, get_accuracy_trend, get_backtest_comparison,
    _load_stock_names
)

# ── FastAPI ──
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import RedirectResponse
    from starlette.middleware.base import BaseHTTPMiddleware
except ImportError:
    print("❌ 缺少FastAPI! 请运行: pip install fastapi uvicorn")
    sys.exit(1)

# ── WebSocket连接管理 ──
class ProgressManager:
    """类TradingAgents ConnectionManager，管理WebSocket连接
    v4.5.18-hotfix: 添加连接频率限制, 防止浏览器重连风暴堵死事件循环
    """
    def __init__(self):
        self.connections: set = set()
        self._lock = asyncio.Lock()
        self._last_connect_time: dict = {}  # client_ip -> last connect timestamp
        self._min_reconnect_interval: float = 2.0  # 最小重连间隔(秒)

    def _should_accept(self, client_ip: str) -> bool:
        """连接频率限制: 同一IP最短2秒重连一次"""
        now = time.time()
        last = self._last_connect_time.get(client_ip, 0)
        if now - last < self._min_reconnect_interval:
            return False
        self._last_connect_time[client_ip] = now
        return True

    async def connect(self, ws: WebSocket, client_ip: str = ""):
        if client_ip and not self._should_accept(client_ip):
            # 拒绝过快重连, 让客户端自己退避
            await ws.accept()
            await ws.close(code=1008, reason="rate_limited")
            return False
        await ws.accept()
        async with self._lock:
            self.connections.add(ws)
        return True

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.connections.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        async with self._lock:
            conns = list(self.connections)
        for ws in conns:
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self.connections -= dead

progress_manager = ProgressManager()

# ── App ──
# ── P1-3: 后台状态广播任务 ──
_bg_status_task = None

async def _broadcast_status_every_30s():
    """每30秒读取cron_status.json并广播到WebSocket客户端 (文件I/O委托到线程池)"""
    global _bg_status_task
    while True:
        try:
            status_file = os.path.join(DATA_DIR, "cron_status.json")
            if os.path.exists(status_file):
                def _read_status():
                    with open(status_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                data = await asyncio.get_event_loop().run_in_executor(
                    _BLOCKING_EXECUTOR, _read_status
                )
                jobs = data.get("jobs", data.get("cron_jobs", []))
                await progress_manager.broadcast({
                    "type": "cron_status",
                    "data": {"total": len(jobs), "jobs": jobs}
                })
        except Exception:
            pass
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动/关闭时执行"""
    os.makedirs(DATA_DIR, exist_ok=True)
    # 启动后台广播
    global _bg_status_task
    _bg_status_task = asyncio.create_task(_broadcast_status_every_30s())
    # v4.5.19: 启动时预热价格缓存（后台刷新麦蕊API）
    asyncio.create_task(_maybe_refresh_prices_bg())
    yield
    # 关闭时取消
    if _bg_status_task:
        _bg_status_task.cancel()

app = FastAPI(title="DSL量化交易系统", version=APP_VERSION["version"], lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://*.local:*",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── 静态文件 ──
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════
# Web认证
# ══════════════════════════════════════

AUTH_FILE = os.path.join(PROJECT_ROOT, "config", "web_auth.json")


def _is_loopback_request(request: Request) -> bool:
    """Only loopback/local browser requests may use the no-auth bootstrap mode."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    client_host = (request.client.host if request.client else "") or ""
    return host in {"localhost", "127.0.0.1", "::1"} or client_host in {"127.0.0.1", "::1"}


def _pub_noauth(request: Request = None):
    """Allow no-auth bootstrap only for local loopback unless explicitly overridden."""
    if os.path.exists(AUTH_FILE):
        return False
    if os.environ.get("DSL_DASHBOARD_ALLOW_NO_AUTH") == "1":
        return True
    return bool(request is not None and _is_loopback_request(request))


def _gen_session_token() -> str:
    """生成会话令牌 (timestamp.nonce.hmac)"""
    cfg = json.load(open(AUTH_FILE))
    secret = cfg.get("session_secret", "")
    if not secret:
        return ""
    ts = int(time.time())
    nonce = secrets.token_hex(8)
    msg = f"{ts}.{nonce}"
    sig = hmac.new(
        base64.b64decode(secret) if isinstance(secret, str) else secret,
        msg.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{msg}.{sig}"


def _verify_session(token: str) -> bool:
    """验证会话令牌"""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    ts_str, nonce, sig_provided = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    # 会话有效期24小时
    if time.time() - ts > 86400:
        return False
    try:
        cfg = json.load(open(AUTH_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    secret = cfg.get("session_secret", "")
    expected = hmac.new(
        base64.b64decode(secret) if isinstance(secret, str) else secret,
        f"{ts_str}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()[:16]
    return hmac.compare_digest(sig_provided, expected)


class AuthMiddleware(BaseHTTPMiddleware):
    """验证会话cookie，未授权跳转登录页"""
    async def dispatch(self, request: Request, call_next):
        # 白名单路径: 不需要登录
        path = request.url.path
        if path in ("/login", "/api/login", "/static/login.html", "/favicon.ico"):
            return await call_next(request)
        # 未设置密码时开放访问
        if _pub_noauth(request):
            return await call_next(request)
        if not os.path.exists(AUTH_FILE):
            return JSONResponse({"error": "auth_not_configured"}, status_code=503)
        # 检查会话
        token = request.cookies.get("session_token", "")
        if not _verify_session(token):
            # AJAX请求返回401，页面请求重定向
            accept = request.headers.get("accept", "")
            if "application/json" in accept or path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse(url="/login")
        return await call_next(request)


app.add_middleware(AuthMiddleware)


# ══════════════════════════════════════
# 登录路由
# ══════════════════════════════════════

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """登录页面"""
    login_path = STATIC_DIR / "login.html"
    if login_path.exists():
        text = await asyncio.get_event_loop().run_in_executor(
            _BLOCKING_EXECUTOR, functools.partial(login_path.read_text, encoding="utf-8")
        )
        return HTMLResponse(text)
    return HTMLResponse("<h1>Login</h1>")


@app.post("/api/login")
async def api_login(request: Request):
    """登录验证"""
    if _pub_noauth(request):
        return {"success": True, "message": "无需认证"}
    if not os.path.exists(AUTH_FILE):
        return JSONResponse({"success": False, "error": "认证未配置"}, status_code=503)
    try:
        body = await request.json()
        password = body.get("password", "")
    except Exception:
        return JSONResponse({"success": False, "error": "请求格式错误"}, status_code=400)
    try:
        cfg = json.load(open(AUTH_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return JSONResponse({"success": False, "error": "认证未配置"}, status_code=500)
    # 验证密码
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), cfg["salt"].encode(), cfg["iterations"]
    )
    expected = base64.b64encode(key).decode()
    if expected != cfg["hash"]:
        return JSONResponse({"success": False, "error": "密码错误"}, status_code=401)
    # 生成会话令牌
    token = _gen_session_token()
    resp = JSONResponse({"success": True, "token": token})
    # 设置cookie: HttpOnly, 24h有效期
    resp.set_cookie(
        key="session_token", value=token,
        max_age=86400, httponly=True,
        # 仅在公开域名上标记secure (通过环境变量 DASHBOARD_SECURE_HOST 配置公开域名)
        secure=os.environ.get("DASHBOARD_SECURE_HOST", "") in request.headers.get("host", ""),
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"success": True})
    resp.delete_cookie("session_token", path="/")
    return resp


# ══════════════════════════════════════
# API路由
# ══════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    """主页面"""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        text = await asyncio.get_event_loop().run_in_executor(
            _BLOCKING_EXECUTOR, functools.partial(index_path.read_text, encoding="utf-8")
        )
        return HTMLResponse(text)
    return HTMLResponse("<h1>DSL Web Dashboard</h1><p>index.html not found</p>")


@app.get("/api/version")
async def api_version():
    """版本API — 从VERSION文件动态读取"""
    return _read_version_info()


@app.get("/api/status")
async def api_status():
    """系统状态API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_system_status)


@app.get("/api/full")
async def api_full():
    """全量数据API — 委托到线程池以保护事件循环"""
    return await asyncio.get_event_loop().run_in_executor(
        _BLOCKING_EXECUTOR, get_full_dashboard
    )


@app.get("/api/predictions")
async def api_predictions():
    """预测数据API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_predictions)


@app.get("/api/pool")
async def api_pool():
    """股票池API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_stock_pool)


@app.get("/api/calibration")
async def api_calibration():
    """校准数据API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_calibration)


@app.get("/api/blackswan")
async def api_blackswan():
    """黑天鹅API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_blackswan)


@app.get("/api/rank-ic")
async def api_rank_ic():
    """v4.7.3: 截面Rank IC监控API"""
    from data_adapter import get_rank_ic
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_rank_ic)


@app.get("/api/portfolio")
async def api_portfolio():
    """持仓API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_portfolio)


@app.get("/api/trading")
async def api_trading():
    """交易记录API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_trading_ledger)


@app.get("/api/progress")
async def api_progress():
    """任务进度API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_progress)


@app.get("/api/cron")
async def api_cron():
    """Cron状态API"""
    return {"cron_jobs": await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_cron_status)}


@app.post("/api/cron/trigger/{task_id}")
async def api_cron_trigger(task_id: str):
    """v4.6.x: 触发器: 通过OpenClaw CLI手动触发单个cron任务"""
    from web_dashboard.data_adapter import trigger_cron_job
    result = await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, trigger_cron_job, task_id)
    return result


@app.get("/api/adaptive")
async def api_adaptive():
    """自适应参数API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_adaptive_params)


@app.get("/api/threshold")
async def api_threshold():
    """阈值引擎API"""
    threshold = await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_threshold_engine)
    return {"threshold_engine": threshold}


@app.get("/api/pool-history")
async def api_pool_history():
    """股票池变更历史API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_pool_history)


@app.get("/api/accuracy-trend")
async def api_accuracy_trend():
    """精度趋势API"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_accuracy_trend)


@app.get("/api/backtest-comparison")
async def api_backtest_comparison():
    """回测对比API"""
    return {"backtests": await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_backtest_comparison)}


@app.post("/api/retrain")
async def api_retrain(request: Request):
    """一键重训API: 接收股票代码列表, 返回重训任务ID"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    
    codes = body.get("codes", [])
    if not codes:
        return JSONResponse({"error": "codes required"}, status_code=400)
    
    # 写入重训任务文件, 由cron的batch_train消费
    retrain_file = os.path.join(PROJECT_ROOT, "data", "retrain_queue.json")
    timestamp = datetime.now().isoformat()
    queue = []
    if os.path.exists(retrain_file):
        try:
            with open(retrain_file, "r", encoding="utf-8") as f:
                queue = json.load(f) or []
        except:
            queue = []
    queue.append({"codes": codes, "requested_at": timestamp, "status": "queued"})
    with open(retrain_file, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    
    return {"ok": True, "codes": codes, "queued_at": timestamp, "queue_position": len(queue)}


@app.get("/api/pipeline")
async def api_pipeline():
    """全流程时序API — 委托到线程池"""
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_pipeline_timeline)


# ── 重训状态追踪 ──
_retrain_status = {}  # code -> {status, started_at, error}
_retrain_status_file = Path(PROJECT_ROOT) / "data" / "retrain_runtime_status.json"

def _load_retrain_status():
    """从文件加载重训状态，处理服务重启后的状态恢复"""
    try:
        if _retrain_status_file.exists():
            with open(_retrain_status_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _retrain_status.update(loaded)
                logger.info(f"从文件恢复了 {len(loaded)} 个重训状态记录")
    except Exception as e:
        logger.warning(f"加载重训状态文件失败: {e}")

def _save_retrain_status():
    """持久化重训状态到文件"""
    try:
        with open(_retrain_status_file, "w", encoding="utf-8") as f:
            json.dump(_retrain_status, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.warning(f"保存重训状态文件失败: {e}")

# 服务启动时恢复状态
_load_retrain_status()


@app.get("/api/retrain-status")
async def api_retrain_status(code: str = ""):
    """查询立即重训状态: running/done/error"""
    if not code:
        return JSONResponse({"error": "code required"}, status_code=400)
    info = _retrain_status.get(code, {})
    if not info:
        # 检查retrain_queue中是否有running记录
        retrain_file = os.path.join(PROJECT_ROOT, "data", "retrain_queue.json")
        if os.path.exists(retrain_file):
            try:
                with open(retrain_file, "r", encoding="utf-8") as f:
                    queue = json.load(f) or []
                for item in queue:
                    codes = item.get("codes", [])
                    if code in codes and item.get("trigger") == "manual_now":
                        status = item.get("status", "")
                        if status == "running":
                            return {"status": "running", "code": code}
                        elif status == "done":
                            return {"status": "done", "code": code}
            except:
                pass
        return {"status": "unknown", "code": code}
    return {"status": info.get("status", "unknown"), "code": code, "error": info.get("error", ""), "started_at": info.get("started_at", "")}


@app.post("/api/retrain-now")
async def api_retrain_now(request: Request):
    """立即重训: 后台调用 train_predictor_enhanced.py 训练单只股票"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    code = body.get("code", "").strip()
    if not code:
        return JSONResponse({"error": "code required"}, status_code=400)

    # 查找股票名称
    pool = get_stock_pool()
    stocks = pool.get("stocks", [])
    name = ""
    for s in stocks:
        if s.get("symbol") == code or s.get("code") == code:
            name = s.get("name", "")
            break
    if not name:
        name = code

    import subprocess, threading

    def _run_training(code, name):
        """后台线程执行训练 → 预测 → 校准更新"""
        import subprocess
        _retrain_status[code] = {"status": "training", "started_at": datetime.now().isoformat()}
        _save_retrain_status()
        try:
            script = os.path.join(PROJECT_ROOT, "scripts", "train_predictor_enhanced.py")
            venv_python = os.path.join(PROJECT_ROOT, ".venv/bin/python3")
            if not os.path.exists(venv_python):
                import shutil
                venv_python = shutil.which("python3") or "python3"

            # Step 1: 训练
            train_result = subprocess.run(
                [venv_python, script, "--codes", code],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=600
            )

            log_lines = [f"[{datetime.now().isoformat()}] {code}"]
            log_lines.append(f"  训练 exit={train_result.returncode}")
            if train_result.stdout:
                log_lines.append(f"  OUT: {train_result.stdout[-300:]}")
            if train_result.returncode != 0 and train_result.stderr:
                log_lines.append(f"  ERR: {train_result.stderr[-300:]}")

            if train_result.returncode != 0:
                _retrain_status[code] = {"status": "error", "started_at": _retrain_status[code]["started_at"], "error": "训练失败"}
                _save_retrain_status()
                with open(os.path.join(PROJECT_ROOT, "data", "retrain_now.log"), "a") as f:
                    f.write("\n".join(log_lines) + "\n")
                return

            # Step 2: 预测
            _retrain_status[code] = {"status": "predicting", "started_at": _retrain_status[code]["started_at"]}
            _save_retrain_status()
            pred_script = os.path.join(PROJECT_ROOT, "scripts", "batch_predict.py")
            if os.path.exists(pred_script):
                # v4.6.9: 手动"立即重训"加 --force, 绕过周末/节假日跳过逻辑,
                # 确保周末点击也能全链路刷新 daily_predict + 校准
                pred_result = subprocess.run(
                    [venv_python, pred_script, "--force"],
                    cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300
                )
                log_lines.append(f"  预测 exit={pred_result.returncode}")
                if pred_result.returncode != 0 and pred_result.stderr:
                    log_lines.append(f"  PRED_ERR: {pred_result.stderr[-300:]}")
                if pred_result.returncode != 0:
                    _retrain_status[code] = {"status": "error", "started_at": _retrain_status[code]["started_at"], "error": "预测失败"}
                    _save_retrain_status()
                    with open(os.path.join(PROJECT_ROOT, "data", "retrain_now.log"), "a") as f:
                        f.write("\n".join(log_lines) + "\n")
                    return

            # Step 3: 校准更新
            _retrain_status[code] = {"status": "calibrating", "started_at": _retrain_status[code]["started_at"]}
            _save_retrain_status()
            fb_result = subprocess.run(
                [venv_python, "-c",
                 "import sys; sys.path.insert(0,'.'); " +
                 "from scripts.feedback_controller import update_all; update_all()"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120
            )
            log_lines.append(f"  校准 exit={fb_result.returncode}")

            # Step 4: 更新状态
            _retrain_status[code] = {"status": "done", "started_at": _retrain_status[code]["started_at"]}
            _save_retrain_status()

            # 更新retrain_queue
            retrain_file = os.path.join(PROJECT_ROOT, "data", "retrain_queue.json")
            try:
                if os.path.exists(retrain_file):
                    with open(retrain_file, "r", encoding="utf-8") as f:
                        queue = json.load(f) or []
                    for item in queue:
                        if code in item.get("codes", []) and item.get("trigger") == "manual_now" and item.get("status") == "running":
                            item["status"] = "done"
                            item["completed_at"] = datetime.now().isoformat()
                    with open(retrain_file, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
            except:
                pass

            with open(os.path.join(PROJECT_ROOT, "data", "retrain_now.log"), "a") as f:
                f.write("\n".join(log_lines) + "\n")
        except Exception as e:
            _retrain_status[code] = {"status": "error", "started_at": _retrain_status.get(code, {}).get("started_at", ""), "error": str(e)}
            _save_retrain_status()
            with open(os.path.join(PROJECT_ROOT, "data", "retrain_now.log"), "a") as f:
                f.write(f"\n[{datetime.now().isoformat()}] {code} ERROR: {e}\n")

    # 检查 train_predictor_enhanced.py 是否支持 --codes 参数
    script = os.path.join(PROJECT_ROOT, "scripts", "train_predictor_enhanced.py")
    if not os.path.exists(script):
        return JSONResponse({"error": "train_predictor_enhanced.py not found"}, status_code=500)

    # 启动后台训练线程
    t = threading.Thread(target=_run_training, args=(code, name), daemon=True)
    t.start()

    # 同时写入retrain_queue标记为已处理（防止batch_train重复）
    retrain_file = os.path.join(PROJECT_ROOT, "data", "retrain_queue.json")
    queue = []
    if os.path.exists(retrain_file):
        try:
            with open(retrain_file, "r", encoding="utf-8") as f:
                queue = json.load(f) or []
        except:
            queue = []
    queue.append({"codes": [code], "requested_at": datetime.now().isoformat(), "status": "running", "trigger": "manual_now"})
    with open(retrain_file, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)

    return {"ok": True, "code": code, "name": name, "started_at": datetime.now().isoformat(), "message": "训练已在后台启动，约1-3分钟完成"}


@app.get("/api/task-reports/{task_id}")
async def api_task_reports(task_id: str):
    """任务历史报告API"""
    reports = await asyncio.get_event_loop().run_in_executor(
        _BLOCKING_EXECUTOR, functools.partial(get_task_reports, task_id)
    )
    return {"task_id": task_id, "reports": reports}


# ── v4.5.9: 模拟交易API ──
def _refresh_position_prices_sync():
    """[在_executor中运行] 刷新持仓现价: 麦蕊API实时行情 (二级降级: 麦蕊API → daily_predict.json)"""
    import sqlite3, json

    db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT id, stock_code FROM positions WHERE stock_code != ''").fetchall()
        if not rows:
            conn.close()
            return

        codes = [r[1] for r in rows]
        prices = {}  # code -> price

        # L1: 麦蕊API实时行情
        try:
            sys.path.insert(0, os.path.join(PROJECT_ROOT))
            from config.mairui_api_config import get_multi_stock_real, get_stock_real
            if len(codes) <= 20:
                results = get_multi_stock_real(codes)
            else:
                results = []
                for code in codes:
                    try:
                        results.append(get_stock_real(code))
                    except Exception:
                        pass
            for r in results:
                code = str(r.get("code", r.get("dm", ""))).zfill(6)
                price = r.get("current_price", r.get("p", r.get("latest", 0)))
                if not price or float(price) <= 0:
                    price = r.get("pc", 0)
                    if "ud" in r:
                        price = r.get("o", r.get("yc", 0))
                if code and price and float(price) > 0:
                    prices[code] = float(price)
        except Exception as e:
            print(f"  ⚠️ 麦蕊API不可用, 回退缓存: {e}")

        # L2: 降级到 daily_predict.json
        if not prices:
            predict_path = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
            if os.path.exists(predict_path):
                try:
                    with open(predict_path) as f:
                        data = json.load(f)
                    for p in data.get("predictions", []):
                        code = p.get("symbol", "")
                        price = p.get("latest_price", p.get("close", 0))
                        if code and price and float(price) > 0:
                            prices[code] = float(price)
                except Exception:
                    pass

        updated = 0
        for r in rows:
            code = r[1]
            if code in prices:
                conn.execute(
                    "UPDATE positions SET current_price=? WHERE id=?",
                    (prices[code], r[0])
                )
                updated += 1

        conn.commit()
        source = "麦蕊API" if updated else "缓存"
        if updated:
            global _last_price_refresh_done_at
            _last_price_refresh_done_at = time.time()
            print(f"  💰 刷新 {updated}/{len(rows)} 持仓现价 ({source})")
        conn.close()
    except Exception as e:
        print(f"  ⚠️ 刷新持仓现价失败: {e}")

async def _refresh_position_prices():
    """异步包装 — 委托到 _BLOCKING_EXECUTOR 以避免阻塞事件循环"""
    await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, _refresh_position_prices_sync)


# ── v4.5.10: ATR动态止损（复用paper_trader.py的生产逻辑） ──

def _fallback_stop_loss(code: str) -> float:
    """固定止损线fallback（板块差异化）"""
    c = str(code or "")
    if c.startswith("688"): return -0.15
    if c.startswith("300") or c.startswith("301"): return -0.15
    if c.startswith("8"): return -0.22
    return -0.08

def _compute_position_stop_losses_sync(codes: list) -> dict:
    """[在_executor中运行] 批量计算所有持仓的ATR动态止损线"""
    result = {}
    try:
        from scripts.paper_trader import PaperTrader, get_stop_loss_pct
        for code in codes:
            try:
                result[code] = get_stop_loss_pct(code)
            except Exception:
                result[code] = _fallback_stop_loss(code)
    except Exception:
        pass
    return result

async def _compute_position_stop_losses(codes: list) -> dict:
    """异步包装 — 委托到 _BLOCKING_EXECUTOR"""
    return await asyncio.get_event_loop().run_in_executor(
        _BLOCKING_EXECUTOR, _compute_position_stop_losses_sync, codes
    )

# ── v4.5.19: 价格刷新背景任务控制 ──
_price_refresh_lock = asyncio.Lock()
_last_price_refresh_ts = 0.0
_last_price_refresh_done_at = 0.0  # v4.6.8: 最近一次成功刷新价格的时间戳
_PRICE_REFRESH_INTERVAL = 120  # 最少间隔120秒才触发一次麦蕊API刷新
# ATR止损缓存（避免每次请求都触发6次麦蕊kline API调用）
_atr_stop_loss_cache = {}  # code -> float
_atr_cache_expiry = 0.0
_ATR_CACHE_TTL = 300  # ATR缓存5分钟

async def _maybe_refresh_prices_bg():
    """如果距上次刷新超过间隔，在后台触发价格刷新（不阻塞当前请求）"""
    global _last_price_refresh_ts
    now = time.time()
    if now - _last_price_refresh_ts < _PRICE_REFRESH_INTERVAL:
        return
    async with _price_refresh_lock:
        if now - _last_price_refresh_ts < _PRICE_REFRESH_INTERVAL:
            return
        _last_price_refresh_ts = now
        asyncio.create_task(_refresh_position_prices())

async def _maybe_refresh_atr_bg(codes: list):
    """后台刷新ATR止损值（麦蕊kline API很慢，禁止在请求路径上调用）"""
    global _atr_stop_loss_cache, _atr_cache_expiry
    now = time.time()
    if now - _atr_cache_expiry < _ATR_CACHE_TTL and all(c in _atr_stop_loss_cache for c in codes):
        return
    async with _price_refresh_lock:
        if now - _atr_cache_expiry < _ATR_CACHE_TTL and all(c in _atr_stop_loss_cache for c in codes):
            return
        _atr_cache_expiry = now
        new_cache = await asyncio.get_event_loop().run_in_executor(
            _BLOCKING_EXECUTOR, _compute_position_stop_losses_sync, codes
        )
        _atr_stop_loss_cache.update(new_cache)


@app.get("/api/paper-trader")
async def api_paper_trader():
    """模拟交易全量数据（v4.5.19: 即时返回缓存价格，后台异步刷新麦蕊API）
    v4.6.9h.2: 数据读取统一走 data_adapter.get_paper_trader_sync() —
    与 /api/full 的 paperTrader 字段同源, 消除两套实现不一致
    """
    db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
    if not os.path.exists(db_path):
        return {"positions": [], "history": [], "performance": {}, "ledger": {}}
    try:
        # 触发后台刷新任务（fire-and-forget：不阻塞当前请求）
        asyncio.create_task(_maybe_refresh_prices_bg())
        # 先获取codes用于触发ATR后台刷新
        import sqlite3 as _sql
        _db = _sql.connect(db_path)
        _codes = [r[0] for r in _db.execute("SELECT stock_code FROM positions WHERE stock_code != ''").fetchall()]
        _db.close()
        if _codes:
            asyncio.create_task(_maybe_refresh_atr_bg(_codes))
        # 同步读取DB中缓存的价格数据(与 /api/full 同源)
        from data_adapter import get_paper_trader_sync
        result = await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, get_paper_trader_sync)
        # v4.6.8: 价格刷新时间戳(来自server后台刷新缓存)
        if isinstance(result.get("summary"), dict):
            result["summary"]["price_refreshed_at"] = (
                datetime.fromtimestamp(_last_price_refresh_done_at).strftime("%H:%M:%S")
                if _last_price_refresh_done_at > 0 else None)
            result["summary"]["price_refresh_age_hours"] = (
                round((time.time() - _last_price_refresh_done_at) / 3600, 1)
                if _last_price_refresh_done_at > 0 else -1)
        return result
    except Exception as e:
        return {"error": str(e)[:200], "positions": [], "history": [], "performance": {}, "ledger": {}}


# ── v4.5.9: 股票筛选器API ──
@app.get("/api/screener")
async def api_screener(
    tier: Optional[str] = None,
    min_score: Optional[float] = None,
    signal: Optional[str] = None,
):
    """股票筛选器 — 融合股票池+预测+行情评分"""
    def _compute():
        pool = get_stock_pool()
        predictions = get_predictions()
        pred_map = {p.get("symbol", p.get("code", "")): p for p in predictions.get("predictions", [])}
        calib = get_calibration()
        acc_map = {s["symbol"]: s for s in calib.get("stocks", [])}

        results = []
        for s in pool.get("stocks", []):
            sym = s.get("symbol", "")
            if tier and s.get("tier", "") != tier:
                continue
            if min_score and s.get("score", 0) < min_score:
                continue
            pred = pred_map.get(sym, {})
            if signal and pred.get("signal", "").lower() != signal.lower():
                continue
            acc_data = acc_map.get(sym, {})
            results.append({
                "symbol": sym, "name": s.get("name", ""),
                "tier": s.get("tier", ""), "score": s.get("score", 0),
                "signal": pred.get("signal", "?"),
                "confidence": pred.get("confidence", 0),
                "predicted_return": pred.get("predicted_return", 0),
                "h5d_accuracy": pred.get("direction_accuracy", 0),
                "h20d_accuracy": (pred.get("h20d") or {}).get("direction_accuracy", 0) or pred.get("h20d_accuracy", 0),
                "h20d_return": (pred.get("h20d") or {}).get("predicted_return", 0) or pred.get("h20d_predicted_return", 0),
                "concepts": (s.get("concept", "") or "")[:200],
            })
        sig_order = {"buy": 0, "sell": 1, "hold": 2}
        results.sort(key=lambda r: (sig_order.get(r["signal"] or "", 9), -(r.get("score", 0))))
        return {"results": results, "total": len(results),
                "filters": {"tier": tier, "min_score": min_score, "signal": signal}}
    return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, _compute)


# ── WebSocket：实时进度推送 ──
@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    """WebSocket进度推送（类比TradingAgents的websocket_notifications）
    v4.5.18-hotfix: 添加速率限制, 防止重连风暴
    """
    client_ip = websocket.client.host if websocket.client else ""
    accepted = await progress_manager.connect(websocket, client_ip)
    if not accepted:
        return  # 被频率限制拒绝
    try:
        # 发送初始连接确认
        await websocket.send_json({
            "type": "connected",
            "data": {"message": "已连接DSL进度流"}
        })
        # 心跳+接收
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳
                try:
                    await websocket.send_json({
                        "type": "heartbeat",
                        "data": {"timestamp": datetime.now().isoformat()}
                    })
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        await progress_manager.disconnect(websocket)


# ── 进度上报API（被cron任务调用） ──
@app.get("/api/audit")
async def api_audit(source: str = "", action: str = "", hours: int = 24):
    """v4.5.9: 审计日志查询"""
    def _query():
        from common.audit import query as audit_query, get_summary
        if source or action:
            logs = audit_query(source=source or None, action=action or None, since_hours=hours)
        else:
            logs = audit_query(since_hours=hours)
        summary = get_summary(since_hours=hours)
        return {"logs": logs, "summary": summary}
    try:
        return await asyncio.get_event_loop().run_in_executor(_BLOCKING_EXECUTOR, _query)
    except Exception as e:
        return {"logs": [], "summary": {}, "error": str(e)}


@app.post("/api/progress/update")
async def api_progress_update(request: Request):
    """接收cron任务的进度更新并广播到WebSocket"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # 写入文件（带文件锁）
    progress_file = os.path.join(DATA_DIR, "task_progress.json")
    lock_path = progress_file + ".lock"
    with open(lock_path, "w") as lockf:
        fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            existing = {}
            if os.path.exists(progress_file):
                with open(progress_file, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
        except Exception:
            existing = {"tasks": []}

        tasks = existing.get("tasks", [])
        # 更新或追加
        task_id = body.get("task_id", body.get("task", ""))
        found = False
        for t in tasks:
            if t.get("task_id") == task_id or t.get("task") == task_id:
                t.update(body)
                t["last_update"] = datetime.now().isoformat()
                found = True
                break
        if not found:
            body["last_update"] = datetime.now().isoformat()
            tasks.append(body)
        existing["tasks"] = tasks
        existing["last_updated"] = datetime.now().isoformat()

        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    # fcntl.flock releases on lockf close

    # 广播到WebSocket
    await progress_manager.broadcast({
        "type": "progress",
        "data": body
    })

    return {"ok": True}


# ── Server ──
if __name__ == "__main__":
    # 用 uvicorn CLI 启动 (避免内部进程fork冲突)
    import subprocess, sys
    port = os.environ.get("DSL_DASHBOARD_PORT", "8888")
    cmd = [sys.executable, "-m", "uvicorn", "web_dashboard.server:app", "--host", "127.0.0.1", "--port", port, "--workers", "4", "--loop", "asyncio", "--log-level", "info"]
    print(f"🚀 DSL Dashboard启动: http://localhost:{port}")
    subprocess.run(cmd)
