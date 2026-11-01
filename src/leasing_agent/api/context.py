"""运行时上下文：配置加载与内存缓存。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / 'config' / 'defaults.json'

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {}
_MTIME: float = 0.0

DEFAULT_CONFIG: Dict[str, Any] = {
    'version': '1.0',
    'purposes': {},
    'manual_checks': [],
    'roles': {},
    'redaction': {'masked_for_business': []},
}


def _read_config() -> Dict[str, Any]:
    global _CACHE, _MTIME
    with _LOCK:
        if not CONFIG_PATH.exists():
            return dict(DEFAULT_CONFIG)
        mtime = CONFIG_PATH.stat().st_mtime
        if _CACHE and mtime == _MTIME:
            return _CACHE
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return dict(DEFAULT_CONFIG)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        _CACHE = merged
        _MTIME = mtime
        return merged


def get_config() -> Dict[str, Any]:
    """读取非机密配置，文件变更后自动重载。"""
    return _read_config()
