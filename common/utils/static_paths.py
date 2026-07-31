"""
静态文件路径工具

功能：
1. 统一解析静态文件根目录（优先配置/环境变量 STATIC_DIR）
2. 提供 /static/uploads/... URL 到本地文件路径的转换方法
3. 避免源码模式下因各服务 cwd 不同导致 static 相对路径指向不同目录
"""
from __future__ import annotations

import os
from pathlib import Path

from common.core.config import get_settings

# 项目根目录：common/utils/static_paths.py -> common/utils -> common -> 项目根
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_FALLBACK_STATIC_ROOT = _PROJECT_ROOT / "xianyu_auto_reply" / "static"


def get_static_root() -> Path:
    """获取静态文件根目录。

    优先级：
    1. 配置项 static_dir（由 pydantic 从环境变量或 .env 文件加载）
    2. 环境变量 STATIC_DIR（兜底）
    3. 未配置时回退到 xianyu_auto_reply/static

    相对路径统一基于「项目根目录」解析，而非当前工作目录（cwd）。
    原因：源码运行时 backend-web / websocket / scheduler 的 cwd 不同，
    若基于 cwd 解析 relative static_dir，会导致各服务指向不同目录。
    """
    static_value = ""
    try:
        static_value = (get_settings().static_dir or "").strip()
    except Exception:
        static_value = ""
    if not static_value:
        static_value = os.environ.get("STATIC_DIR", "").strip()
    if not static_value:
        return _FALLBACK_STATIC_ROOT

    root = Path(static_value)
    if not root.is_absolute():
        root = _PROJECT_ROOT / root
    return root


def resolve_static_url_to_path(static_url: str) -> Path | None:
    """将 /static/... URL 解析为本地静态文件路径。"""
    if not static_url:
        return None
    normalized = static_url.strip()
    if not (
        normalized.startswith("/static/")
        or normalized.startswith("static/")
    ):
        return None

    relative_path = normalized.lstrip("/").replace("static/", "", 1)
    return get_static_root() / relative_path


__all__ = [
    "get_static_root",
    "resolve_static_url_to_path",
]
