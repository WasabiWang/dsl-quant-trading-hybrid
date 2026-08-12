# Core module init file
# P1-10 fix: 运行时读取VERSION文件，保持单一真相源
import os as _os
_version_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "VERSION")
try:
    with open(_version_path, "r") as _f:
        _raw = _f.read().strip()
    __version__ = _raw.split("/")[0].strip().lstrip("v")
except Exception:
    __version__ = "4.5.18"  # fallback
