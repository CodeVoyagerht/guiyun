"""本机目录后端：WPS 云盘、OneDrive、坚果云、百度网盘同步空间这类。

**配置的根目录就是这个后端的命名空间根**：`E:\\素材` 下的一切，在云端形状里
写作 `/xxx`。把根设成盘符本身（`E:\\`）就等于在整理整个盘。

为什么不用 `/e/素材/...` 那种盘符折叠写法：那样一来「库根 `/摄影库`」到底指
`C:\\摄影库` 还是 `<配置根>\\摄影库` 就有两种解释，scan 看到的位置和 plan 算出的
位置会对不上——同一份配置搬一次就错一次。一个根、一种解释，错不了。

顺带也保住了本机后端的唯一优势：移动都在同一个盘里，`os.rename` 是常数时间。
"""

from __future__ import annotations

import os
import shutil

from .base import Backend, BackendError, Entry, join


def _norm(raw: str) -> str:
    return os.path.abspath(os.path.expanduser(raw))


class LocalBackend(Backend):
    kind = "local"

    def __init__(self, root: str):
        if not root:
            raise BackendError("local 后端要配 backend_local_root")
        self.root = _norm(root)
        if not os.path.isdir(self.root):
            raise BackendError(f"本机根目录不存在：{self.root}")

    # ── 路径映射 ──────────────────────────────────────────────────────────
    def _inside(self, fs_path: str) -> bool:
        root = self.root.rstrip("\\/")
        cand = os.path.normcase(fs_path)
        base = os.path.normcase(root)
        return cand == base or cand.startswith(base + os.sep)

    def to_fs(self, path: str) -> str:
        """云端形状 /未整理/2024 → <根>\\未整理\\2024。跑出根的一律拒绝。"""
        if not path.startswith("/"):
            raise BackendError(f"路径必须以 / 开头：{path}")
        segs = [s for s in path.split("/") if s]
        if any(s in (".", "..") for s in segs):
            raise BackendError(f"路径里不许有 . / ..：{path}")
        fs = _norm(os.path.join(self.root, *segs) if segs else self.root)
        if not self._inside(fs):
            raise BackendError(f"路径跑到根目录外面了：{path} → {fs}")
        return fs

    def to_cloud(self, fs_path: str) -> str:
        fs = _norm(fs_path)
        if not self._inside(fs):
            raise BackendError(f"本机路径不在根目录下：{fs}")
        rel = os.path.relpath(fs, self.root)
        if rel == ".":
            return "/"
        return "/" + rel.replace("\\", "/")

    # ── 读 ────────────────────────────────────────────────────────────────
    def list_dir(self, path: str) -> list[Entry]:
        fs_path = self.to_fs(path)
        if not os.path.isdir(fs_path):
            raise BackendError(f"不是目录：{path}")
        out: list[Entry] = []
        try:
            with os.scandir(fs_path) as it:
                for de in it:
                    try:
                        st = de.stat()
                    except OSError:
                        continue
                    is_dir = de.is_dir()
                    out.append(
                        Entry(
                            path=join(path, de.name),
                            name=de.name,
                            is_dir=is_dir,
                            size=0 if is_dir else st.st_size,
                            mtime=st.st_mtime,
                        )
                    )
        except OSError as exc:
            raise BackendError(f"列目录失败 {path}：{exc}") from exc
        out.sort(key=lambda e: e.name)
        return out

    def stat(self, path: str) -> Entry | None:
        fs_path = self.to_fs(path)
        if not os.path.exists(fs_path):
            return None
        st = os.stat(fs_path)
        is_dir = os.path.isdir(fs_path)
        return Entry(
            path=path,
            name=os.path.basename(fs_path.rstrip("\\/")) or "/",
            is_dir=is_dir,
            size=0 if is_dir else st.st_size,
            mtime=st.st_mtime,
        )

    def usage(self) -> dict:
        total = used = None
        try:
            du = shutil.disk_usage(self.root)
            total, used = du.total, du.used
        except OSError:
            pass
        return {"used": used, "total": total, "raw": ""}

    # ── 写 ────────────────────────────────────────────────────────────────
    def mkdir(self, path: str) -> None:
        fs_path = self.to_fs(path)
        try:
            os.makedirs(fs_path, exist_ok=True)
        except OSError as exc:
            raise BackendError(f"建目录失败 {path}：{exc}") from exc

    def move(self, src: str, dst: str) -> None:
        s, d = self.to_fs(src), self.to_fs(dst)
        if not os.path.exists(s):
            raise BackendError(f"源不存在：{src}")
        if os.path.exists(d):
            raise BackendError(f"目标已存在：{dst}")
        try:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.rename(s, d)
        except OSError:
            # 只在根目录跨盘时才可能走到这里（根可以是 \\\\server\\share 这种）
            try:
                shutil.move(s, d)
            except OSError as exc2:
                raise BackendError(f"移动失败 {src} → {dst}：{exc2}") from exc2

    def copy(self, src: str, dst: str) -> None:
        s, d = self.to_fs(src), self.to_fs(dst)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.exists(d):
            raise BackendError(f"目标已存在：{dst}")
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)

    def delete(self, path: str, *, expect_empty: bool = False) -> None:
        fs_path = self.to_fs(path)
        if os.path.isdir(fs_path):
            if not expect_empty:
                raise BackendError(
                    f"删目录要显式声明它是空的（expect_empty=True）：{path}"
                )
            try:
                os.rmdir(fs_path)      # 非空会直接报错，绝不递归删
            except OSError as exc:
                raise BackendError(f"目录不是空的，不动它：{path}（{exc}）") from exc
        elif os.path.exists(fs_path):
            os.remove(fs_path)
        else:
            raise BackendError(f"不存在：{path}")

    # ── 诊断 ──────────────────────────────────────────────────────────────
    def check(self) -> dict:
        if not os.path.isdir(self.root):
            return {"ok": False, "detail": f"根目录不存在：{self.root}"}
        try:
            entries = self.list_dir("/")
        except BackendError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": f"{self.root} 可读，根下有 {len(entries)} 项"}
