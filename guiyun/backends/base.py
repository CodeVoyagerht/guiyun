"""后端抽象层。

归云要同时伺候两种"网盘"：真云端（百度网盘）和本机同步目录（WPS 云盘、
OneDrive、坚果云这类）。上层所有逻辑——索引、规则、plan、apply、undo——
只认这里定义的 Entry 和 Backend，不知道底下是谁。

约定：
- 路径一律用正斜杠、以 "/" 开头的绝对路径表示。云端天然如此；本机目录
  由 local.py 把盘符折叠成 /c/... 这种形状，避免上层写两套判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class BackendError(RuntimeError):
    """后端操作失败。消息必须是人能读懂的中文，直接给用户看。"""


class AuthError(BackendError):
    """没登录 / 登录态失效。这是唯一需要用户立刻动手的一类错误。"""


class RateLimitError(BackendError):
    """被风控拦了。

    这类错误必须熔断整批操作，不能重试硬刚——百度对异常调用量的反应是
    封禁账号，代价远大于整理本身。
    """


@dataclass(slots=True)
class Entry:
    """云端或本地的一个文件/文件夹条目。"""

    path: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: float | None = None
    md5: str | None = None

    @property
    def parent(self) -> str:
        if "/" not in self.path:
            return "/"
        head = self.path.rsplit("/", 1)[0]
        return head or "/"

    @property
    def ext(self) -> str:
        if self.is_dir or "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()

    @property
    def depth(self) -> int:
        return len([p for p in self.path.split("/") if p])


def join(base: str, *parts: str) -> str:
    """拼云端路径。base 必须以 / 开头。"""
    segs = [p for p in (base or "").split("/") if p]
    for part in parts:
        for seg in str(part).split("/"):
            if seg:
                segs.append(seg)
    return "/" + "/".join(segs)


def split_ext(name: str) -> tuple[str, str]:
    if "." not in name:
        return name, ""
    stem, _, ext = name.rpartition(".")
    return stem, ext


class Backend:
    """后端要实现的全部原语。故意做小：上层不需要更多。"""

    kind = "base"

    # ── 读 ────────────────────────────────────────────────────────────────
    def list_dir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def stat(self, path: str) -> Entry | None:
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        return self.stat(path) is not None

    def usage(self) -> dict:
        """{"used": int|None, "total": int|None, "raw": str}"""
        raise NotImplementedError

    # ── 写（真动网盘，只许 apply/undo 调） ────────────────────────────────
    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def move(self, src: str, dst: str) -> None:
        """把 src 移到 dst。

        两种语义要分清楚，这里统一成一种：**dst 永远是最终完整路径**。
        如果 dst 是已存在的目录、且调用方想要"移进去"，调用方自己拼
        `join(dst, name)`——不让后端去猜，猜错就是一次误移动。
        """
        raise NotImplementedError

    def copy(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def delete(self, path: str, *, expect_empty: bool = False) -> None:
        """删一个东西。

        删**目录**必须显式 `expect_empty=True` 声明"我已经确认它是空的"——
        云端 `rm` 一个非空目录是递归删整棵子树，本地 `rmtree` 同理，都不可逆。
        后端自己也会再核一遍（本地靠 `rmdir` 的非空报错，云端靠调用方的话）。
        默认 `False` 不是麻烦，是让"删目录"必须被写出来一次。
        """
        raise NotImplementedError

    # ── 诊断 ──────────────────────────────────────────────────────────────
    def check(self) -> dict:
        """返回 {"ok": bool, "detail": str}，给 doctor 用。"""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
