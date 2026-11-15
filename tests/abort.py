"""熔断那一刻，台账里到底留下了什么。

这条不走真网盘也不走 CLI：用一个「搬到第 2 条就报风控」的假后端，
直接喂给 apply()，然后翻 ops 表和 runs.stats，看三件事有没有记下来：

1. 停在哪一条（ops 那一行的 err 里有没有「整批在这里停下」）
2. 后面还剩几条没动（untouched）
3. 那条「半截」记录会不会被 undo 当成搬过（ops.ok 必须是 0）

跑：python tests/abort.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guiyun.apply import apply  # noqa: E402
from guiyun.backends.base import Backend, RateLimitError  # noqa: E402
from guiyun.config import Config  # noqa: E402
from guiyun.index import Index  # noqa: E402
from guiyun.plan import Item, Plan  # noqa: E402

ok_count = 0
fail_count = 0


def check(cond: bool, label: str) -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"  \u2713 {label}")
    else:
        fail_count += 1
        print(f"  \u2717 {label}")


class FlakyBackend(Backend):
    """第 1 条顺利搬走，第 2 条报风控，第 3 条永远轮不到。"""

    kind = "fake"

    def __init__(self) -> None:
        self.moves: list[tuple[str, str]] = []
        self.made: list[str] = []

    def list_dir(self, path):
        return []

    def stat(self, path):
        return None

    def exists(self, path):
        return True

    def usage(self):
        return {"ok": True}

    def check(self):
        return {"ok": True, "detail": ""}

    def mkdir(self, path: str) -> None:
        self.made.append(path)

    def move(self, src: str, dst: str) -> None:
        if len(self.moves) >= 1:
            raise RateLimitError("遇到错误, 远端服务器返回错误, 代码: 132, 消息: 安全验证")
        self.moves.append((src, dst))

    def copy(self, src, dst):
        raise NotImplementedError

    def delete(self, path, *, expect_empty=False):
        raise NotImplementedError

    def close(self):
        pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guiyun-abort-"))
    cfg = Config(path=tmp / "guiyun.toml")
    idx = Index(tmp / "index.sqlite")

    items = [
        Item(
            src=f"/未整理/堆{i}",
            name=f"堆{i}",
            is_dir=True,
            size=1024,
            n_files=3,
            dst=f"/摄影库/区域/某地/2024/2024-05-18_05-24/航拍/堆{i}",
            target_dir=f"/摄影库/区域/某地/2024/2024-05-18_05-24/航拍",
        )
        for i in (1, 2, 3, 4)
    ]
    plan = Plan(created=0.0, library_root="/摄影库", items=items)
    backend = FlakyBackend()

    print("== 熔断当场的结论")
    out = apply(cfg, plan, backend, idx, dry_run=False)
    check(out.aborted is not None, "整批标了中止")
    check(out.moved == 1, f"只搬走了 1 条（实际 {out.moved}）")
    check(out.untouched == 2, f"记下后面还有 2 条没轮到（实际 {out.untouched}）")
    check(out.ok is False, "整批不算成功")
    check("还有 2 条没轮到" in out.line(), f"摘要里有「还有 2 条没轮到」：{out.line()}")
    check("已中止" in out.line(), "摘要里点明了已中止")

    print("== 台账：停在哪、剩多少")
    rows = list(idx.db.execute("SELECT * FROM ops WHERE run_id = ? ORDER BY seq", (out.run_id,)))
    check(len(rows) == 2, f"只记了两行（第 1 条成功 + 第 2 条中止），实际 {len(rows)}")
    first, second = rows[0], rows[1]
    check(first["ok"] == 1, "第 1 条记成成功")
    check(second["ok"] == 0, "第 2 条没记成成功")
    check("整批在这里停下" in (second["err"] or ""), f"第 2 行点明了整批停下：{second['err']}")
    check("还有 2 条没动" in (second["err"] or ""), f"第 2 行点明了还剩几条：{second['err']}")
    check("132" in (second["err"] or ""), "原始风控文案没丢")

    print("== undo 不会被「半截」记录骗到")
    undoable = [r for r in idx.run_ops(out.run_id) if r["kind"] == "move"]
    check(len(undoable) == 1, f"undo 只认那 1 条真搬过的（实际 {len(undoable)}）")
    check(undoable[0]["src"] == "/未整理/堆1", "认得是第 1 条")

    print("== runs 那一行也带着这些数")
    run = idx.runs(1)[0]
    stats = json.loads(run["stats"] or "{}")
    check(stats.get("untouched") == 2, f"stats.untouched = {stats.get('untouched')}")
    check(bool(stats.get("aborted")), "stats.aborted 有话说")
    check(run["ok"] == 0, "这一批整体记成失败")

    print("== status 在没有索引时也要报这条流水账")
    # 索引空（还没扫 / 刚被清）时 status 以前直接 return，用户看不到
    # 「上次停在哪、还剩几条」——熔断之后正是最需要看到它的时候。
    import io
    from contextlib import redirect_stdout

    from guiyun import cli as cli_mod

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_mod._say_recent_runs(idx)
    shown = buf.getvalue()
    check("还有 2 条没轮到" in shown, f"status 里能看到剩余条数：{shown.strip()}")
    check("整批停在这里" in shown, "status 里能看到停在哪、被什么挡住")

    print()
    if fail_count:
        print(f"\u2717 {fail_count} 项没通过（通过 {ok_count} 项）")
        return 1
    print(f"\u2713 熔断的台账全对：停在哪一条、后面剩几条、undo 认不认，都记下来了（{ok_count} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
