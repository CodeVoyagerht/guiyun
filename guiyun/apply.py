"""执行方案：真搬，并且每搬一条就记一条。

两条不许商量的规矩：
1. **先写日志再算成功**。搬完才记，中途断掉就没人知道它搬到哪了。
2. **风控即熔断**。百度对异常调用量的反应是封号，不是报错。遇到 132/112 立刻
   停整批，把已完成的条数报清楚，不重试、不硬刚。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .backends.base import AuthError, Backend, BackendError, RateLimitError, join
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
    aborted: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        # gone 不算失败：源本来就不在了（上一轮搬过 / 用户自己挪过），
        # 当成失败会让整批 exit 1，逼着人以后一律忽略失败提示。
        return self.aborted is None and self.failed == 0

    def line(self) -> str:
        bits = [f"搬了 {self.moved} 条"]
        if self.gone:
            bits.append(f"已不在原处 {self.gone} 条")
        if self.skipped:
            bits.append(f"跳过 {self.skipped}")
        if self.failed:
            bits.append(f"失败 {self.failed}")
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


def _is_gone(err: str) -> bool:
    text = err or ""
    if "通配符" in text:
        return False
    return any(h in text for h in _GONE_HINTS)


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
                idx.log_op(run_id, seq, "mkdir", item.target_dir, "", False, str(exc))
                break
            except AuthError as exc:
                out.aborted = f"登录态掉了：{exc}"
                idx.log_op(run_id, seq, "mkdir", item.target_dir, "", False, out.aborted)
                break
            except BackendError as exc:
                out.failed += 1
                out.errors.append((item.src, f"建目录失败：{exc}"))
                idx.log_op(run_id, seq, "mkdir", item.target_dir, "", False, str(exc))
                continue

            err = ""
            for attempt in range(cfg.apply_retry + 1):
                try:
                    backend.move(item.src, item.dst)
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
                    if attempt < cfg.apply_retry and not _is_gone(err):
                        time.sleep(1.0 + attempt)

            if out.aborted:
                idx.log_op(run_id, seq, "move", item.src, item.dst, False, err or out.aborted)
                break
            if err and _is_gone(err):
                # 源已经不在了：上一轮跑过、或者用户自己搬过，不是失败。
                out.gone += 1
                idx.log_op(run_id, seq, "move", item.src, item.dst, False, f"已不在原处：{err}")
            elif err:
                out.failed += 1
                out.errors.append((item.src, err))
                idx.log_op(run_id, seq, "move", item.src, item.dst, False, err)
            else:
                out.moved += 1
                idx.log_op(run_id, seq, "move", item.src, item.dst, True, None)

            time.sleep(cfg.apply_throttle)
    except AuthError as exc:
        out.aborted = f"登录态掉了：{exc}"
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
