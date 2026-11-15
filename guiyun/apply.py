"""执行方案：真搬，并且每搬一条就记一条。

两条不许商量的规矩：
1. **先写日志再算成功**。动手之前先落一行「未完成」，搬成功后再把它改成成功；
   中途断掉时，台账里留下的就是那行「未完成」，看得出它搬到哪儿去了。
   （`undo` 只认成功行，所以这行"半截"不会被当成搬过。）
2. **风控即熔断**。百度对异常调用量的反应是封号，不是报错。遇到 132/112 立刻
   停整批，把已完成的条数、停在哪里、后面还剩几条都记清楚，不重试、不硬刚。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .backends.base import (
    AuthError,
    Backend,
    BackendError,
    RateLimitError,
    join,
    split_ext,
)
from .config import Config
from .index import Index
from .plan import Item, Plan


@dataclass
class Outcome:
    run_id: str
    moved: int = 0
    failed: int = 0
    skipped: int = 0
    gone: int = 0
    renamed: int = 0
    untouched: int = 0
    aborted: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        # gone 不算失败：源本来就不在了（上一轮搬过 / 用户自己挪过），
        # 当成失败会让整批 exit 1，逼着人以后一律忽略失败提示。
        # untouched 也不算失败：它是"没轮到"，不是"做砸了"。
        return self.aborted is None and self.failed == 0

    def line(self) -> str:
        bits = [f"搬了 {self.moved} 条"]
        if self.renamed:
            bits.append(f"其中 {self.renamed} 条撞名改了名")
        if self.gone:
            bits.append(f"已不在原处 {self.gone} 条")
        if self.skipped:
            bits.append(f"跳过 {self.skipped}")
        if self.failed:
            bits.append(f"失败 {self.failed}")
        if self.untouched:
            bits.append(f"还有 {self.untouched} 条没轮到")
        bits.append(f"耗时 {self.elapsed:.1f}s")
        out = "，".join(bits)
        if self.aborted:
            out += f"\n**已中止**：{self.aborted}"
        return out


def _now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


# 「这东西不在了」的后端话术。local 说「源不存在」，baidu 说「路径不存在」——
# 后者的完整文案还带着「或者路径里有 * ? [ ] 这类字符被当成了通配符」，
# 那种情况是名字本身有问题，不能当"不在了"放过去。
_GONE_HINTS = ("源不存在", "路径不存在")

# 动手之前先落这一行，搬成功后再覆盖成成功。进程崩在「已经搬走、还没记账」
# 之间时，台账里剩下的就是这句话——比什么都没有强。
_PENDING = "未完成（进程在这里中断过就是这一条）"


def _is_gone(err: str) -> bool:
    text = err or ""
    if "通配符" in text:
        return False
    return any(h in text for h in _GONE_HINTS)


def _suffixed(dst: str, n: int, is_dir: bool) -> str:
    """`…/航拍/开封古城` → `…/航拍/开封古城 (2)`；文件名保住扩展名。"""
    parent, _, name = dst.rpartition("/")
    if is_dir or "." not in name:
        new = f"{name} ({n})"
    else:
        stem, ext = split_ext(name)
        new = f"{stem} ({n}).{ext}"
    return f"{parent}/{new}" if parent else f"/{new}"


def _abort_note(err: str, untouched: int) -> str:
    """熔断那一行的台账文案。

    只记「风控: …」的话，事后翻台账只会看到一条普通错误，看不出**整批停在这里**、
    更看不出后面还有多少条没轮到。这两个数只有当场知道，所以当场写下来。
    """
    tail = f"｜整批在这里停下，后面还有 {untouched} 条没动" if untouched else "｜整批在这里停下"
    return f"{err}{tail}"


def apply(
    cfg: Config,
    plan: Plan,
    backend: Backend,
    idx: Index,
    dry_run: bool = False,
    limit: int = 0,
    on_progress=None,
) -> Outcome:
    run_id = _now_id()
    todo = [i for i in plan.items if i.status != "skip"]
    if limit and limit > 0:
        todo = todo[:limit]
    out = Outcome(run_id=run_id, skipped=len(plan.items) - len(todo))

    t0 = time.time()
    if not dry_run:
        idx.start_run(run_id)

    seq = 0
    try:
        for item in todo:
            seq += 1
            if on_progress:
                on_progress(seq, len(todo), item)

            if dry_run:
                out.moved += 1
                continue

            # 目录先补齐。mkdir 是幂等的，本来就在也不会报错。
            try:
                backend.mkdir(item.target_dir)
            except RateLimitError as exc:
                out.aborted = f"mkdir {item.target_dir} 触发风控：{exc}"
                out.untouched = len(todo) - seq
                idx.log_op(
                    run_id,
                    seq,
                    "mkdir",
                    item.target_dir,
                    "",
                    False,
                    _abort_note(out.aborted, out.untouched),
                )
                break
            except AuthError as exc:
                out.aborted = f"登录态掉了：{exc}"
                out.untouched = len(todo) - seq
                idx.log_op(
                    run_id,
                    seq,
                    "mkdir",
                    item.target_dir,
                    "",
                    False,
                    _abort_note(out.aborted, out.untouched),
                )
                break
            except BackendError as exc:
                out.failed += 1
                out.errors.append((item.src, f"建目录失败：{exc}"))
                idx.log_op(run_id, seq, "mkdir", item.target_dir, "", False, str(exc))
                continue

            err = ""
            actual = item.dst
            tries = 0
            renamed = 0
            while True:
                try:
                    # 先落一行「未完成」再动手：搬成功会把它覆盖成成功，
                    # 中途断掉就留着这行，事后能看出它要搬到哪儿去。
                    idx.log_op(run_id, seq, "move", item.src, actual, False, _PENDING)
                    backend.move(item.src, actual)
                    err = ""
                    break
                except RateLimitError as exc:
                    err = f"风控：{exc}"
                    out.aborted = f"{item.src} 触发风控，整批停在这里。{exc}"
                    break
                except AuthError as exc:
                    # 先把这条记进台账再停整批：直接 raise 出去的话，
                    # 已经搬不动了的这一条不会出现在任何记录里。
                    err = f"登录态掉了：{exc}"
                    out.aborted = err
                    break
                except BackendError as exc:
                    err = str(exc)
                    if "目标已存在" in err:
                        # 方案是零 API 的预览，它看不见库根底下已经有什么
                        # （scan 按设计跳过库根）。所以撞名在真搬的这一刻解决：
                        # 换个名字再搬，别让 plan 说"可搬"、apply 逐条报错。
                        if cfg.on_conflict == "skip":
                            err = f"{err}（on_conflict = skip：这一条没搬）"
                            break
                        renamed += 1
                        if renamed > 9:
                            err = f"{err}（已经试了 9 个名字都撞）"
                            break
                        actual = _suffixed(item.dst, renamed + 1, item.is_dir)
                        continue
                    if _is_gone(err) or tries >= cfg.apply_retry:
                        break
                    tries += 1
                    time.sleep(1.0 + tries)

            changed = bool(not err and actual != item.dst)
            if changed:
                item.dst = actual          # 台账要记真搬去的地方，不是方案里的
            if out.aborted:
                out.untouched = len(todo) - seq
                idx.log_op(
                    run_id,
                    seq,
                    "move",
                    item.src,
                    actual,
                    False,
                    _abort_note(err or out.aborted, out.untouched),
                )
                break
            if err and _is_gone(err):
                # 源已经不在了：上一轮跑过、或者用户自己搬过，不是失败。
                out.gone += 1
                idx.log_op(run_id, seq, "move", item.src, actual, False, f"已不在原处：{err}")
            elif err:
                out.failed += 1
                out.errors.append((item.src, err))
                idx.log_op(run_id, seq, "move", item.src, actual, False, err)
            else:
                out.moved += 1
                if changed:
                    out.renamed += 1
                idx.log_op(run_id, seq, "move", item.src, actual, True, None)

            time.sleep(cfg.apply_throttle)
    except AuthError as exc:
        # 兜底的网：真正处理登录失效的地方在循环里（那里会记账再停）。
        # 走到这儿说明错误是从没被包住的地方冒出来的，至少把"停在哪、还剩多少"记下来。
        out.aborted = f"登录态掉了：{exc}"
        if not out.untouched:
            out.untouched = max(len(todo) - seq, 0)
    finally:
        out.elapsed = time.time() - t0
        if not dry_run:
            idx.finish_run(
                run_id,
                out.ok,
                {
                    "moved": out.moved,
                    "failed": out.failed,
                    "skipped": out.skipped,
                    "gone": out.gone,
                    "untouched": out.untouched,
                    "aborted": out.aborted or "",
                    "seconds": round(out.elapsed, 1),
                },
            )
            # 搬完之后索引对不上了。不自动重扫（那是几十上百次请求），
            # 只打个标记，下次 plan/report 会提醒。
            if out.moved:
                idx.set_meta("stale", int(time.time()))
    return out


def undo(cfg: Config, idx: Index, backend: Backend, run_id: str, dry_run: bool = False) -> Outcome:
    """把一次 apply 反向搬回去。只信 runs/ops 里记下来的东西。"""
    ops = [o for o in idx.run_ops(run_id) if o["kind"] == "move" and o["ok"]]
    ops.reverse()
    new_id = _now_id() + "-undo"
    out = Outcome(run_id=new_id)
    t0 = time.time()
    if not dry_run:
        idx.start_run(new_id)

    seq = 0
    try:
        for op in ops:
            seq += 1
            src, dst = op["src"], op["dst"]
            if dry_run:
                out.moved += 1
                continue
            try:
                # 搬回原地：要建的是**原位置**的父目录。源目录可能已经被
                # `guiyun clean` 清成空壳删掉了，这里不补齐就会搬不回去。
                backend.mkdir(src.rsplit("/", 1)[0] or "/")
                backend.move(dst, src)
                out.moved += 1
                idx.log_op(new_id, seq, "move", dst, src, True, None)
            except RateLimitError as exc:
                out.aborted = f"回滚触发风控：{exc}"
                idx.log_op(new_id, seq, "move", dst, src, False, str(exc))
                break
            except AuthError as exc:
                out.aborted = f"登录态掉了：{exc}"
                idx.log_op(new_id, seq, "move", dst, src, False, out.aborted)
                break
            except BackendError as exc:
                if _is_gone(str(exc)):
                    # 要搬回去的东西已经不在原位了（用户自己又挪过？）。
                    # 数成失败没意义——undo 的目标是"能搬回去的都搬回去"。
                    out.gone += 1
                    idx.log_op(new_id, seq, "move", dst, src, False, f"已不在原处：{exc}")
                else:
                    out.failed += 1
                    out.errors.append((dst, str(exc)))
                    idx.log_op(new_id, seq, "move", dst, src, False, str(exc))
            time.sleep(cfg.apply_throttle)
    finally:
        out.elapsed = time.time() - t0
        if not dry_run:
            idx.finish_run(
                new_id,
                out.ok,
                {
                    "moved": out.moved,
                    "failed": out.failed,
                    "aborted": out.aborted or "",
                    "seconds": round(out.elapsed, 1),
                    "undo_of": run_id,
                },
            )
            if out.moved:
                idx.set_meta("stale", int(time.time()))
    return out
