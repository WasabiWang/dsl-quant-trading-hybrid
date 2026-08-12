"""pytest conftest — shared fixtures and configuration for DSL quant tests."""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def project_root():
    """Session-scoped fixture: inject project root into sys.path."""
    sys.path.insert(0, str(PROJECT_ROOT))
    yield PROJECT_ROOT


@pytest.fixture
def chdir_project(project_root):
    """Change working directory to project root for the duration of the test."""
    old_cwd = os.getcwd()
    os.chdir(str(project_root))
    yield project_root
    os.chdir(old_cwd)


@pytest.fixture
def temp_dir():
    """Function-scoped temporary directory, cleaned up after test."""
    path = tempfile.mkdtemp(prefix="dsl_test_")
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def mock_akshare(monkeypatch):
    """Mock akshare to avoid network calls in tests."""
    mock_ak = MagicMock()
    monkeypatch.setattr("akshare.stock_zh_a_hist", mock_ak)
    monkeypatch.setattr("akshare.stock_zh_a_daily", mock_ak)
    return mock_ak


@pytest.fixture
def mock_kline():
    """Generate synthetic OHLCV kline data for testing."""
    import pandas as pd
    import numpy as np
    from datetime import datetime, timedelta

    dates = pd.date_range(end=datetime.now(), periods=500, freq="B")
    np.random.seed(42)
    close = 10 + np.cumsum(np.random.randn(500) * 0.2)
    data = pd.DataFrame({
        "date": dates,
        "open": close + np.random.randn(500) * 0.05,
        "high": close + np.abs(np.random.randn(500) * 0.1),
        "low": close - np.abs(np.random.randn(500) * 0.1),
        "close": close,
        "volume": np.random.randint(1000000, 5000000, 500),
    })
    return data


@pytest.fixture
def sample_trade():
    """Sample trade data for paper trader tests."""
    return {
        "market": "A",
        "stock_code": "000001",
        "action": "BUY",
        "price": 12.50,
        "quantity": 100,
        "reason": "test trade",
    }


@pytest.fixture(autouse=True)
def reset_globals():
    """Reset any global state that might leak between tests."""
    yield
    # Clean up globals from test modules
    for mod in list(sys.modules.keys()):
        if mod.startswith("tests.") or mod.startswith("test_"):
            m = sys.modules.get(mod)
            if m and hasattr(m, "results"):
                m.results = []
            if m and hasattr(m, "failed"):
                m.failed = 0
