"""百度网盘后端 —— 通过 BaiduPCS-Go 的 CLI 干活。

这个文件里每一条解析逻辑都不是猜的，是照着 BaiduPCS-Go v4.0.2 的源码写的，
因为 CLI 有三个大坑，踩了才知道：

1. **没有 --json**。`ls` 输出的是 tablewriter 渲染的人类表格，只能解析文本。
   好消息是 BaiduPCS-Go 的 pcstable.NewTable 设了
   `SetAutoWrapText(false)` / `SetBorder(false)` / `SetHeaderLine(false)` /
   `SetColumnSeparator("")`：没有边框、没有竖线、**长文件名不会折行**。
   坏消息是列之间只有空格。所以解析锚点不能用分隔符，只能用**日期列的固定
   格式** `YYYY-MM-DD HH:MM:SS`——左边一定是 `序号 大小`，右边一定是文件名。

2. **退出码永远是 0**。所有错误都是 `fmt.Println(err)` 然后 return，进程照样
   成功退出。所以成功与否只能看输出里的字面量（"操作成功"/"失败"）。这是本
   文件里所有 `_expect_ok` 存在的原因。

3. **mv 有两种语义**，靠目标是否存在来区分（cp_mv.go）：
     目标存在且是目录 → 移进去，且**保留原文件名**
     目标不存在       → 当成重命名，直接 `opera=move` 到那个完整路径
   而 Rename 和 Move 走的是同一个 API（prepare.go: `case OperationMove,
   OperationRename: method = "move"`），所以**一次 mv 就能同时改名和换目录**。
   结论（本后端的行为约定）：把 dst 当最终完整路径，父目录先建好、确认 dst
   自己不存在，然后 `mv src dst`。
   必须确认 dst 不存在——否则 dst 万一是个目录，mv 会静默地把 src 塞进去，
   变成一次错位移动。这就是 `move()` 里那次查存在性的理由。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

from .base import (
    AuthError,
    Backend,
    BackendError,
    Entry,
    RateLimitError,
    join,
)

CST = timezone(timedelta(hours=8))

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 日期列固定格式：pcstime.FormatTime → "%d-%02d-%02d %02d:%02d:%02d"（东八区）
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
# 大小列：converter.ConvertFileSize(size, 2) → "12B" / "3.45KB" / "1.20MB" / "-"
_SIZE_RE = re.compile(r"^([\d.]+)([KMGTP]?B)$")
_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}

# 错误码 → 我们的异常类型（来自 pcserrorinfo.go 的 findPCSErr 和 panerrorinfo.go 的 FindPanErr）
_AUTH_CODES = {31045, 3, -4, -5, -6, -11, 9019}
_RATE_CODES = {132, 112}
_AUTH_WORDS = ("未登录", "请重新登录", "登录信息有误", "登录状态过期", "帐号无效", "accesstoken未设置")
_RATE_WORDS = ("安全验证", "请求过于频繁", "稍后再试", "操作频繁", "try later")


def parse_size(text: str) -> int:
    """把 "3.45KB" 这类字符串还原成字节数。认不出返回 0。"""
    text = (text or "").strip()
    if not text or text == "-":
        return 0
    m = _SIZE_RE.match(text.upper())
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2), 1))
    except ValueError:
        return 0


class BaiduBackend(Backend):
    """百度网盘后端。root 固定为网盘根 "/"，云端路径天然绝对。"""

    kind = "baidu"

    def __init__(self, cli: str, timeout: float = 180.0, config_dir: str | None = None):
        if not cli:
            raise BackendError(
                "没配 BaiduPCS-Go 的路径：[backend.baidu] cli 是空的。\n"
                "去 https://github.com/qjfoidnh/BaiduPCS-Go/releases 下 windows-x64 版本，"
                "解压后把 BaiduPCS-Go.exe 的完整路径填进去（单引号包住，反斜杠不用转义）。"
            )
        # 允许两种写法：绝对路径（多数情况），或裸命令名走 PATH。
        # 只认"文件存在"会把装在 PATH 上的用户挡在门外，那太没必要了。
        looks_like_path = os.sep in cli or (os.altsep and os.altsep in cli)
        if looks_like_path:
            if not os.path.isfile(cli):
                raise BackendError(
                    f"这个路径下没有 BaiduPCS-Go：{cli}\n"
                    "检查一下 [backend.baidu] cli 是不是写错了盘符或目录名；"
                    "路径建议用单引号包住（'D:\\BaiduNetdisk\\BaiduPCS-Go.exe'），反斜杠就不用写两遍。"
                )
        elif shutil.which(cli) is None:
            raise BackendError(
                f"PATH 上找不到 {cli}，也不是一个存在的路径。\n"
                "两个办法任选：① 把 [backend.baidu] cli 改成本体的完整路径；"
                "② 把 BaiduPCS-Go.exe 所在目录加进 PATH。"
            )
        self.cli = cli
        self.timeout = timeout
        self.config_dir = config_dir
        # 目录列表缓存。键是目录路径，值是 {子项名: Entry}。
        # 每次 mkdir/move/copy/delete 成功都会增量更新它，所以它不会明显过期；
        # 这么做是为了把"N 次移动"从 3N 次 API 调用压到 1+N 次（风控面前，调用
        # 次数就是风险本身）。
        self._ls_cache: dict[str, dict[str, Entry]] = {}

    # ── 进程调用 ──────────────────────────────────────────────────────────
    def _run(self, args: list[str]) -> str:
        env = dict(os.environ)
        env["BAIDUPCS_GO_VERBOSE"] = "0"
        if self.config_dir:
            env["BAIDUPCS_GO_CONFIG_DIR"] = self.config_dir
        try:
            proc = subprocess.run(
                [self.cli, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
                env=env,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise BackendError(
                f"BaiduPCS-Go {' '.join(args)} 超过 {int(self.timeout)} 秒没有响应。" "网络慢或者网盘在抽风，稍后再试。"
            ) from None
        except OSError as exc:
            raise BackendError(f"启动 BaiduPCS-Go 失败：{exc}") from None
        # Go 直接吐 UTF-8 字节，别用系统代码页去解，否则中文路径全是乱码
        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        self._raise_for(out)
        return out

    @staticmethod
    def _raise_for(out: str) -> None:
        """从输出文本里判断是不是出错了。

        BaiduPCS-Go 失败也返回 0，所以只能看文本。
        """
        if not out.strip():
            raise BackendError("BaiduPCS-Go 没有任何输出，可能没登录或者程序坏了。跑一次 `guiyun doctor`。")
        code = re.search(r"代码:\s*(-?\d+)", out)
        if code:
            num = int(code.group(1))
            if num in _AUTH_CODES:
                raise AuthError(f"百度网盘登录态失效了（代码 {num}）。跑 `guiyun login` 重新扫码。")
            if num in _RATE_CODES:
                raise RateLimitError(
                    f"百度网盘要求安全验证（代码 {num}）。立即停止，别重试——"
                    "继续调接口只会让账号被盯得更紧。过几小时再来。"
                )
            raise BackendError(f"百度网盘返回错误，代码 {num}：{out.strip()[-300:]}")
        for word in _AUTH_WORDS:
            if word in out:
                raise AuthError(f"百度网盘未登录或登录态失效（{word}）。跑 `guiyun login` 重新扫码。")
        for word in _RATE_WORDS:
            if word in out:
                raise RateLimitError(f"疑似被百度风控拦下（{word}）。立即停止整批操作，过几小时再来。")
        if "未匹配到路径" in out:
            raise BackendError("路径不存在，或者路径里有 * ? [ ] 这类字符被当成了通配符。")

    def _expect_ok(self, out: str, keys: tuple[str, ...], what: str) -> None:
        """确认输出里出现了成功标志，否则报错。"""
        if any(k in out for k in keys):
            return
        if "失败" in out:
            raise BackendError(f"{what}失败：{out.strip()[-400:]}")
        raise BackendError(f"{what}结果不明，输出里既没有成功也没有失败：{out.strip()[-400:]}")

    # ── 读 ────────────────────────────────────────────────────────────────
    def list_dir(self, path: str) -> list[Entry]:
        out = self._run(["ls", path or "/"])
        entries = self._parse_ls(out, path or "/")
        self._ls_cache[path or "/"] = {e.name: e for e in entries}
        return entries

    def _cached_dir(self, path: str) -> dict[str, Entry]:
        if path not in self._ls_cache:
            self.list_dir(path)
        return self._ls_cache[path]

    def stat(self, path: str) -> Entry | None:
        path = path or "/"
        if path == "/":
            return Entry(path="/", name="", is_dir=True)
        parent, _, name = path.rstrip("/").rpartition("/")
        parent = parent or "/"
        return self._cached_dir(parent).get(name)

    def usage(self) -> dict:
        try:
            out = self._run(["quota"])
        except BackendError as exc:
            return {"used": None, "total": None, "raw": str(exc)}
        m = re.search(r"总空间:\s*(\S+?),\s*已用空间:\s*(\S+?),", out)
        if not m:
            return {"used": None, "total": None, "raw": out.strip()}
        return {"used": parse_size(m.group(2)), "total": parse_size(m.group(1)), "raw": out.strip()}

    @staticmethod
    def _parse_ls(out: str, path: str) -> list[Entry]:
        """解析 `ls` 的无边框表格。

        结构（源码 ls_search.go + pcstable.go）：
            <空行>
            当前目录: /xxx
            ----
              #   文件大小   修改日期             文件(目录)
              0    4.00MB    2024-05-18 12:33:44  a.jpg
              1         -    2024-05-18 12:33:44  sub/
                 总: 4.00MB                        文件总数: 1, 目录总数: 1
            ----
        行数 >= 50 时表格后面还会再打一次"当前目录"，一并忽略。
        """
        entries: list[Entry] = []
        started = False
        for raw in out.splitlines():
            line = raw.rstrip()
            if line.strip() == "----":
                if started:
                    break
                started = True
                continue
            if not started:
                continue
            m = _TS_RE.search(line)
            if not m:
                continue  # 表头、合计行、二次"当前目录"都落在这里
            left = line[: m.start()].strip()
            name = line[m.end() :].strip()
            if not name:
                continue
            parts = left.split()
            if len(parts) < 2:
                continue
            size_text = parts[-1]
            name, is_dir = (name[:-1], True) if name.endswith("/") else (name, False)
            try:
                mtime = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)), tzinfo=CST,
                ).timestamp()
            except ValueError:
                mtime = None
            entries.append(
                Entry(
                    path=join(path, name),
                    name=name,
                    is_dir=is_dir,
                    size=0 if is_dir else parse_size(size_text),
                    mtime=mtime,
                )
            )
        return entries

    # ── 写 ────────────────────────────────────────────────────────────────
    def mkdir(self, path: str) -> None:
        """建目录，缺哪层补哪层（云端 mkdir 只能建一层）。"""
        path = path.rstrip("/") or "/"
        if path == "/":
            return
        segs = [s for s in path.split("/") if s]
        cur = ""
        for seg in segs:
            parent = cur or "/"
            cur = join(cur, seg)
            if seg in self._cached_dir(parent):
                continue
            out = self._run(["mkdir", cur])
            self._expect_ok(out, ("创建目录成功",), f"创建目录 {cur} ")
            self._ls_cache[parent][seg] = Entry(path=cur, name=seg, is_dir=True)
            self._ls_cache.pop(cur, None)

    def move(self, src: str, dst: str) -> None:
        if src == dst:
            return
        parent = dst.rsplit("/", 1)[0] or "/"
        self.mkdir(parent)
        if (dst.rsplit("/", 1)[-1] or "") in self._cached_dir(parent):
            # 目标已存在。此时直接 mv 会让 BaiduPCS-Go 把 src 塞进那个目录，
            # 变成一次静默的错位移动，所以必须由上层换名字。
            raise BackendError(f"目标已存在，不能直接移动过去：{dst}（上层应该先改名）")
        out = self._run(["mv", src, dst])
        self._expect_ok(out, ("移动成功", "重命名成功"), f"移动 {src} → {dst} ")
        self._after_move(src, dst)

    def copy(self, src: str, dst: str) -> None:
        parent = dst.rsplit("/", 1)[0] or "/"
        self.mkdir(parent)
        if (dst.rsplit("/", 1)[-1] or "") in self._cached_dir(parent):
            raise BackendError(f"目标已存在，不能直接复制过去：{dst}")
        out = self._run(["cp", src, dst])
        self._expect_ok(out, ("拷贝成功", "复制成功"), f"复制 {src} → {dst} ")
        if src in self._ls_cache and parent not in self._ls_cache:
            return
        entry = self.stat(src)
        if entry and parent in self._ls_cache:
            self._ls_cache[parent][entry.name] = Entry(
                path=dst, name=dst.rsplit("/", 1)[-1], is_dir=entry.is_dir, size=entry.size, mtime=entry.mtime
            )

    def delete(self, path: str, *, expect_empty: bool = False) -> None:
        # 云端 rm 一个非空目录 = 递归删整棵子树（进回收站，但结构没了）。
        # 所以删目录必须由调用方显式声明它是空的。
        if not expect_empty and self._is_cached_dir(path):
            raise BackendError(
                f"删目录要显式声明它是空的（expect_empty=True）：{path}"
            )
        out = self._run(["rm", path])
        self._expect_ok(out, ("已删除",), f"删除 {path} ")
        parent, _, name = path.rpartition("/")
        self._ls_cache.get(parent or "/", {}).pop(name, None)
        self._ls_cache.pop(path, None)

    def _is_cached_dir(self, path: str) -> bool:
        """是不是目录。缓存里有就不用问网盘；没有才付一次往返。"""
        parent, _, name = path.rpartition("/")
        entry = self._ls_cache.get(parent or "/", {}).get(name)
        if entry is not None:
            return entry.is_dir
        try:
            info = self.stat(path)
        except BackendError:
            return False
        return bool(info and info.is_dir)

    def _after_move(self, src: str, dst: str) -> None:
        """成功后同步缓存：源目录少一项，目标目录多一项。"""
        sparent, _, sname = src.rpartition("/")
        sparent = sparent or "/"
        newparent, _, newname = dst.rpartition("/")
        newparent = newparent or "/"
        entry = self._ls_cache.get(sparent, {}).pop(sname, None)
        self._ls_cache.pop(src, None)
        self._ls_cache.pop(dst, None)
        if newparent in self._ls_cache:
            if entry is not None:
                self._ls_cache[newparent][newname] = Entry(
                    path=dst, name=newname, is_dir=entry.is_dir, size=entry.size, mtime=entry.mtime
                )
            else:
                # 源没在缓存里（比如整目录移动），留空等下次 ls 补
                self._ls_cache.pop(newparent, None)

    # ── 诊断 ──────────────────────────────────────────────────────────────
    def check(self) -> dict:
        """确认「这个进程现在能不能真的读写网盘」。

        坑：BaiduPCS-Go 的 `who` 在**没登录时也退出 0**，只打一行
        `当前帐号 uid: 0, 用户名: , 性别: , 年龄: 0.0`。
        光看它「没报错」就会把「没登录」判成「已登录」，然后 apply 搬到一半才炸。
        所以这里必须看 uid 和用户名这两个字段本身，不能看有没有错误信息。
        """
        try:
            out = self._run(["who"])
        except (AuthError, BackendError) as exc:
            return {"ok": False, "detail": str(exc)}

        uid_m = re.search(r"uid:\s*(\d+)", out)
        uid = int(uid_m.group(1)) if uid_m else 0
        # 用户名不能写成 (\S+)：没登录时原文是 `用户名: , 性别:`，会把逗号当名字抓出来
        name_m = re.search(r"用户名:\s*([^,\s]+)", out)
        who = name_m.group(1).strip() if name_m else ""

        if uid == 0 or not who:
            return {
                "ok": False,
                "detail": "还没登录（who 报 uid 0、用户名为空）。先跑 `guiyun login`，"
                          "或直接在 BaiduPCS-Go 里登录一次。",
            }

        u = self.usage()
        if u.get("total"):
            detail = f"已登录 {who}；空间 {u['used'] / 1024**3:.1f} / {u['total'] / 1024**3:.1f} GB"
        else:
            detail = f"已登录 {who}"
        return {"ok": True, "detail": detail}

    def close(self) -> None:
        self._ls_cache.clear()


def warmup(cli: str, config_dir: str | None = None) -> BaiduBackend:
    """建后端并把首页列一次，确认真的通。给 doctor / scan 开头用。"""
    backend = BaiduBackend(cli, config_dir=config_dir)
    time.sleep(0)  # 留个位置：以后要在这里加调用间隔
    backend.list_dir("/")
    return backend
