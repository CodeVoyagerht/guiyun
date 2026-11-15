"""临时调试：造 fixture → scan → plan，把每条落位打出来看。"""

from __future__ import annotations

import shutil
import sys
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

import make_fixture
from guiyun import cli, plan as plan_mod
from guiyun.config import load

DRIVE = PROJ / ".fixture" / "drive"
HOME = PROJ / ".fixture" / "home-dbg"

CONFIG = """\
[backend]
kind = "local"

[backend.local]
root = "{root}"

[library]
root = "/摄影库"
pending_dir = "待定"

[taxonomy]
source = "{tax}"

[scan]
roots = ["/未整理"]

[layout]
time = "week"
camera = true

[match]
min_size = 100

[apply]
throttle = 0.0
retry = 1
on_conflict = "rename"
"""


def run(*argv: str) -> int:
    buf = StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--home", str(HOME), *argv])
    out = buf.getvalue()
    print(out)
    return rc


def main() -> int:
    make_fixture.main()
    if HOME.exists():
        shutil.rmtree(HOME)
    HOME.mkdir(parents=True)
    (HOME / "guiyun.toml").write_text(
        CONFIG.format(
            root=str(DRIVE).replace("\\", "\\\\"),
            tax=str(make_fixture.TAXONOMY).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    cfg = load(HOME)
    print("scan:", run("scan"))
    plan = plan_mod.build(cfg, __import__("guiyun.index", fromlist=["Index"]).Index(cfg.index_path))
    print(f"\n共 {len(plan.items)} 条：auto {len(plan.auto)} / pending {len(plan.pending)} / skip {len(plan.skipped)}")
    for it in plan.items:
        print(f"  [{it.status}] {it.src}")
        print(f"        → {it.dst}")
        if it.why:
            print(f"        ? {it.why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
