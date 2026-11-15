"""walk 的记账本：读不了的目录、超出层数的目录，都得报出来。

以前这两件事都是静默的——用户看到「索引好了：N 项」，以为整棵树都在，
其实有一批目录根本没进去。这个测试盯的就是这两行提示。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guiyun.backends.local import LocalBackend  # noqa: E402
from guiyun.index import walk  # noqa: E402

fail = 0


def check(ok, msg: str) -> None:
    global fail
    print(("  ✓ " if ok else "  ✗ ") + msg)
    if not ok:
        fail += 1


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guiyun-walk-"))
    try:
        drive = tmp / "drive"
        # /浅/a.jpg 和一条 6 层深的链，最后一层放个文件
        (drive / "浅").mkdir(parents=True)
        (drive / "浅" / "a.jpg").write_bytes(b"x" * 10)
        deep = drive / "l1" / "l2" / "l3" / "l4" / "l5" / "l6"
        deep.mkdir(parents=True)
        (deep / "深处.jpg").write_bytes(b"x" * 10)

        backend = LocalBackend(str(drive))
        report: dict = {}

        entries, truncated = walk(backend, ["/"], 3, 100_000, [], None, report)
        names = [e.name for e in entries]

        check(not truncated, "没撞条数保险丝")
        check("a.jpg" in names, "浅的目录能扫到")
        check("l1" in names and "l2" in names, "前三层的目录本身进来了")
        check("深处.jpg" not in names, "第 6 层的文件没进来（超出 max_depth）")
        check(bool(report.get("deep")), f"报了被层数卡住的目录：{report.get('deep')}")
        check(all(p.startswith("/l") for p in report["deep"]), "报的是那条深链，不是别处")

        # 层数给够，同一棵树就该全扫到
        report2: dict = {}
        entries2, _ = walk(backend, ["/"], 8, 100_000, [], None, report2)
        check("深处.jpg" in [e.name for e in entries2], "max_depth 调大后能扫到底")
        check(not report2.get("deep"), "这时不该再报被卡住的目录")

        # 读不了的目录：删掉正在遍历的目录会抛，这里用不存在的根模拟
        report3: dict = {}
        entries3, _ = walk(backend, ["/不存在"], 3, 100_000, [], None, report3)
        check(bool(report3.get("errors")), f"读不了的目录被记下来了：{report3.get('errors')[:1]}")
        check(entries3 == [], "读不了就是空结果，不编东西")
        backend.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fail:
        print(f"\n✗ {fail} 项没过")
        return 1
    print("\n✓ 记账本没漏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
