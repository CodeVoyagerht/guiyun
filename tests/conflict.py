"""回归：目标路径已经被占用时，`on_conflict = "rename"` 必须真的改名。

这是审查里最贵的一个 bug：`plan.build()` 里 `_uniq()` 的 `used` 从一开始
只装本次计划自己占的路径，没拿云端已有路径播种 —— 于是撞上云端已有同名
目标时原样返回 dst，方案说"可搬"，`apply` 到那一条必然被后端以「目标已
存在」拒掉。修法是 `used = {p: 1 for p in existing}`，这个脚本盯着它。

跑法：py tests/conflict.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

from guiyun import cli  # noqa: E402

TAXONOMY = HERE / "fixture-taxonomy"
FAILS: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'✓' if ok else '✗'} {what}")
    if not ok:
        FAILS.append(what)


def run(home: Path, *argv: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--home", str(home), *argv])
    return rc, buf.getvalue()


def touch(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    stamp = 1_740_000_000.0          # 上传时间，故意跟拍摄日期无关
    os.utime(path, (stamp, stamp))


def json_part(out: str) -> dict:
    """`plan --json` 只吐 JSON，但前面可能有后端检查的一两行提示，跳过它。"""
    return json.loads(out[out.index("{") :])


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guiyun-conflict-"))
    try:
        home = tmp / "home"
        drive = tmp / "drive"
        home.mkdir()

        src = drive / "未整理" / "2024-05-18 开封古城 航拍"
        touch(src / "DJI_0001.JPG")

        (home / "guiyun.toml").write_text(
            f"""\
[backend]
kind = "local"

[backend.local]
root = "{drive.as_posix()}"

[library]
root = "/摄影库"

[taxonomy]
source = "{TAXONOMY.as_posix()}"

[scan]
roots = ["/未整理"]

[apply]
throttle = 0.0
retry = 0
on_conflict = "rename"
""",
            encoding="utf-8",
        )

        print("① 扫一遍再算，拿到它本来要去的地方")
        rc, out = run(home, "scan")
        check(rc == 0, "scan 退出码 0")
        rc, out = run(home, "plan", "--json")
        check(rc == 0, "plan 退出码 0")

        items = json_part(out)["items"]
        first = [i for i in items if i["src"].endswith("2024-05-18 开封古城 航拍")][0]
        check(first["status"] == "auto", "第一条是可搬的")
        want = first["dst"]

        print("② 手动把目标位置占住（模拟上次整理过、这次同名）")
        (drive / want.lstrip("/")).mkdir(parents=True)
        check((drive / want.lstrip("/")).is_dir(), "目标目录已存在")

        print("③ 重扫再算：方案仍说可搬（它看不见库根，这是设计），但不能再逐条撞死")
        run(home, "scan")
        rc, out = run(home, "plan", "--json")
        items = json_part(out)["items"]
        second = [i for i in items if i["src"].endswith("2024-05-18 开封古城 航拍")][0]
        check(second["status"] == "auto", "撞名之后仍然是可搬（不是 skip，也不会被悄悄跳过）")

        print("④ apply 在真搬的那一刻解决撞名，台账记真搬去的位置")
        rc, out = run(home, "apply", "--yes")
        check(rc == 0, "apply 退出码 0")
        check("失败" not in out, "apply 一条没失败")
        check("撞名改了名" in out, f"输出里说了改名这件事：{out.strip().splitlines()[-2:]}")
        moved = drive / f"{want} (2)".lstrip("/") / "DJI_0001.JPG"
        check(moved.is_file(), f"文件在 {want} (2) 里")
        check(not (drive / want.lstrip("/") / "DJI_0001.JPG").exists(), "没有塞进已存在的那个目录")

        from guiyun.index import Index

        idx = Index(home / "index.sqlite")
        try:
            run_row = idx.runs(limit=1)[0]
            ops = [o for o in idx.run_ops(run_row["id"]) if o["kind"] == "move" and o["ok"]]
        finally:
            idx.close()
        check(len(ops) == 1, "台账里就一条成功的移动")
        check(ops[0]["dst"] == f"{want} (2)", f"台账记的是真位置：{ops[0]['dst']}")

        if FAILS:
            print(f"\n✗ {len(FAILS)} 项没过：")
            for f in FAILS:
                print(f"  - {f}")
            return 1
        print("\n✓ 撞名时会改名，不会被「目标已存在」卡死")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
