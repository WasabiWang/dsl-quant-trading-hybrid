#!/usr/bin/env python3
"""Deterministic warmup task for the dashboard pipeline.

This replaces the old cron payload that only said "连接池预热" and therefore
had no script-level progress evidence when the OpenClaw agent wrapper timed out.
"""

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from common.progress_tracker import ProgressTracker


def main() -> int:
    tracker = ProgressTracker("pool_warmup", total_steps=2)
    url = os.environ.get("DSL_POOL_WARMUP_URL", "http://127.0.0.1:18082/health")
    timeout = float(os.environ.get("DSL_POOL_WARMUP_TIMEOUT_SECONDS", "10"))

    try:
        tracker.step(1, "检查连接池健康端点", url=url)
        req = urllib.request.Request(url, headers={"User-Agent": "dsl-pool-warmup/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 0)
            body = resp.read(200).decode("utf-8", errors="replace")

        if status < 200 or status >= 300:
            raise RuntimeError(f"warmup endpoint returned HTTP {status}: {body[:120]}")

        tracker.step(2, "连接池健康端点已响应", http_status=status)
        tracker.complete("连接池预热完成", url=url, http_status=status)
        print(f"pool_warmup ok: {url} HTTP {status}")
        return 0
    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
        tracker.fail(str(exc))
        print(f"pool_warmup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
