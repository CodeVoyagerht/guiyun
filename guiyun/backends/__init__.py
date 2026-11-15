"""后端工厂。上层只写一句 `open_backend(cfg)`，不关心底下是云端还是本地镜像。"""

from __future__ import annotations

from .base import AuthError, Backend, BackendError, Entry, RateLimitError, join, split_ext


def open_backend(cfg) -> Backend:
    kind = cfg.backend_kind
    if kind == "baidu":
        from .baidu import BaiduBackend

        return BaiduBackend(cfg.backend_cli, config_dir=cfg.backend_config_dir or None)
    if kind == "local":
        from .local import LocalBackend

        return LocalBackend(cfg.backend_local_root)
    raise BackendError(f"不认识的后端类型：{kind!r}（只能是 baidu 或 local）")


__all__ = [
    "AuthError",
    "Backend",
    "BackendError",
    "Entry",
    "RateLimitError",
    "join",
    "open_backend",
    "split_ext",
]
