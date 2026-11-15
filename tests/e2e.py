"""端到端自检：造 fixture → scan → plan → apply → undo，每一步都断言。

不碰真网盘，只用 local 后端。跑法：python tests/e2e.py
"""

from __future__ import annotations

import io
import re
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = HERE.parent
sys.path.insert(0, str(PROJ))

import make_fixture  # noqa: E402
from guiyun import cli, plan as plan_mod  # noqa: E402
from guiyun.config import load  # noqa: E402
from guiyun.index import Index  # noqa: E402

DRIVE = PROJ / ".fixture" / "drive"
HOME = PROJ / ".fixture" / "home-e2e"
TAXONOMY = HERE / "fixture-taxonomy"

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

WEEK = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")

FAILS: list[str] = []
LOG: list[str] = []


def check(ok: bool, what: str) -> None:
    print(f"  {'✓' if ok else '✗'} {what}")
    if not ok:
        FAILS.append(what)


def run(*argv: str) -> int:
    """跑一次 CLI，输出先收着——真出问题再翻出来看。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["--home", str(HOME), *argv])
    LOG.append(f"$ guiyun {' '.join(argv)}\n{buf.getvalue()}")
    return rc


def parts(dst: str) -> list[str]:
    return [p for p in dst.split("/") if p]


def main() -> int:
    print("① 造 fixture")
    make_fixture.main()

    if HOME.exists():
        shutil.rmtree(HOME)
    HOME.mkdir(parents=True)
    (HOME / "guiyun.toml").write_text(
        CONFIG.format(
            root=str(DRIVE).replace("\\", "\\\\"),
            tax=str(TAXONOMY).replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    cfg = load(HOME)

    print("② scan")
    check(run("scan") == 0, "scan 退出码 0")
    check(cfg.index_path.exists(), "索引库文件生成了")

    print("③ plan")
    check(run("plan") == 0, "plan 退出码 0")
    plan = plan_mod.load(cfg)
    assert plan is not None

    by_src = {i.src: i for i in plan.items}
    check(len(plan.items) == 8, f"共 8 条计划（实际 {len(plan.items)}）")
    check(len(plan.auto) == 4, f"4 条定得下来（实际 {len(plan.auto)}）")
    check(len(plan.pending) == 4, f"4 条进待定（实际 {len(plan.pending)}）")

    # 区域枝：省折叠（只有河南）→ 市层出现（郑州/洛阳/开封三个市）→ 地点 → 园区区域
    dji = by_src["/未整理/2024-05-18 泡泡塘 水世界"]
    check(dji.is_dir, "整堆的按整目录搬，不拆成单文件")
    check(
        dji.dst == "/摄影库/区域/郑州/泡泡塘森林溪谷/水世界/2024/2024-05-18_05-24/航拍/2024-05-18 泡泡塘 水世界",
        f"落位：{dji.dst}",
    )
    check("河南" not in parts(dji.dst), "只有一个省，省层折叠掉了")
    check(
        "遥测" in " ".join(dji.reasons) or "SRT" in " ".join(dji.reasons),
        "同名 .SRT 遥测把拍摄类型定成了航拍",
    )

    flower = by_src["/未整理/2024-05-18 泡泡塘 花海 延时"]
    check("/延时/" in flower.dst, f"目录名写了延时，就落延时枝：{flower.dst}")

    lao = by_src["/未整理/20240519/洛阳老君山 航拍"]
    check("/区域/洛阳/洛阳老君山/2024/" in lao.dst, f"地点与市都对：{lao.dst}")
    check(any(WEEK.match(p) for p in parts(lao.dst)), "时间是「年 / 周六→周五周期」两层")

    old = by_src["/未整理/老素材2019年/开封古城 地拍"]
    check(old.status == "auto", "只有年份也能自己搬")
    check(
        old.dst == "/摄影库/区域/开封/开封古城/2019/地拍/开封古城 地拍",
        f"只认到年份就落年层，不编周：{old.dst}",
    )

    event = by_src["/未整理/开封 第39届菊花节"]
    check(event.status == "pending", "没有可信拍摄日期的活动进待定")
    check(
        event.dst.startswith("/摄影库/活动/开封菊花文化节/第39届/未知时间/"),
        f"活动枝按届次 + 未知时间：{event.dst}",
    )
    check("/航拍/" in event.dst, "活动里的拍摄类型也照判（SRT 遥测）")

    douyin = by_src["/未整理/2024-08-03 抖音竖屏"]
    check(douyin.status == "pending", "抖音但拍摄类型没把握 → 待定")
    check(
        douyin.dst.startswith("/摄影库/抖音/2024/2024-08-03_08-09/待定/"),
        f"抖音自成一枝，待定留在枝里：{douyin.dst}",
    )

    lone = by_src["/未整理/IMG_20240601_120000.jpg"]
    check(lone.status == "pending", "认不出地点的散文件不进区域枝")
    check("/待定/" in lone.dst, f"落待定桶：{lone.dst}")

    check(not [i for i in plan.items if "THM_001" in i.src], "小于 min_size 的缩略图不进方案")
    check(not [i for i in plan.items if "空文件夹" in i.src], "空目录不进方案")

    print("③5 人工钉住 / 解释")
    check(run("explain", "/未整理/IMG_20240601_120000.jpg") == 0, "explain 能解释一条")
    check(
        run("review", "--set", "/未整理/IMG_20240601_120000.jpg",
            "--place", "开封古城", "--shot", "地拍", "--date", "2024-06-01") == 0,
        "review --set 钉住一条（地点 + 拍摄类型 + 日期）",
    )
    plan = plan_mod.load(cfg)
    pinned = {i.src: i for i in plan.items}["/未整理/IMG_20240601_120000.jpg"]
    check(pinned.status == "auto", "钉住之后它就能自己搬了")
    check(
        pinned.dst == "/摄影库/区域/开封/开封古城/2024/2024-06-01_06-07/地拍/IMG_20240601_120000.jpg",
        f"钉住的日期真的落进了路径：{pinned.dst}",
    )
    check(len(plan.auto) == 5 and len(plan.pending) == 3, "可搬 5 条、待定 3 条")
    check(run("review") == 0, "review 列待定不炸")

    print("④ apply --dry-run（必须一条都没动）")
    check(run("apply", "--dry-run") == 0, "dry-run 退出码 0")
    check((DRIVE / "未整理" / "开封 第39届菊花节" / "DJI_0004.MP4").exists(), "dry-run 之后原地没动")

    print("⑤ apply --yes")
    check(run("apply", "--yes") == 0, "apply 退出码 0")
    lib = DRIVE / "摄影库"
    check(
        (lib / "区域" / "郑州" / "泡泡塘森林溪谷" / "水世界" / "2024" / "2024-05-18_05-24"
         / "航拍" / "2024-05-18 泡泡塘 水世界" / "DJI_20240518093012_0001_D.SRT").exists(),
        "整目录搬时 .SRT 也跟着走了（结构不散）",
    )
    check(
        (lib / "区域" / "开封" / "开封古城" / "2019" / "地拍" / "开封古城 地拍" / "DSC_0001.JPG").exists(),
        "只有年份的那堆落到了年层",
    )
    check(
        (lib / "抖音" / "2024" / "2024-08-03_08-09" / "待定" / "2024-08-03 抖音竖屏").exists(),
        "抖音待定的整堆也搬进了抖音枝",
    )
    left_behind = sorted(p for p in (DRIVE / "未整理").rglob("*") if p.is_file())
    check(
        [p.name for p in left_behind] == ["THM_001.jpg"],
        f"源目录只该剩缩略图（实际 {[p.name for p in left_behind]}）",
    )

    print("⑤5 clean（整理完剩下的空壳）")
    check(run("clean") == 2, "没加 --yes 时 clean 不动手，只数个数")
    check(run("clean", "--dry-run") == 0, "clean --dry-run 退出码 0")
    check((DRIVE / "未整理" / "20240519").exists(), "dry-run 之后空壳还在")
    check(run("clean", "--yes") == 0, "clean --yes 退出码 0")
    check(not (DRIVE / "未整理" / "20240519").exists(), "搬空的目录被清掉了")
    check((DRIVE / "未整理" / "缩略图" / "THM_001.jpg").exists(), "里面有文件的目录一个都没碰")
    check((DRIVE / "未整理").exists(), "扫根自己不会被删")

    print("⑥ status / 台账")
    check(run("status") == 0, "status 退出码 0")
    idx = Index(cfg.index_path)
    runs = idx.runs(5)
    check(bool(runs) and runs[0]["ok"] == 1, "整理记录写进台账了")
    check(
        len(idx.run_ops(runs[0]["id"])) == len(plan.items),
        f"台账里 {len(plan.items)} 条移动都记了（可搬的进枝，待定的进待定桶）",
    )
    undo_id = runs[0]["id"]

    print("⑦ undo")
    check(run("undo", undo_id) == 0, "undo 退出码 0")
    check(
        (DRIVE / "未整理" / "IMG_20240601_120000.jpg").exists(),
        "散文件搬回去了",
    )
    check(
        (DRIVE / "未整理" / "20240519" / "洛阳老君山 航拍" / "DJI_0003.MP4").exists(),
        "嵌套目录一样能还原",
    )
    still = [p for p in (DRIVE / "摄影库").rglob("*") if p.is_file()]
    check(not still, f"摄影库下一个文件都不剩（剩了 {len(still)} 个）")

    print("⑧ review --clear（撤掉人工判定，回到机器推断）")
    check(run("review", "--clear", "/未整理/IMG_20240601_120000.jpg") == 0, "clear 退出码 0")
    plan = plan_mod.load(cfg)
    back = {i.src: i for i in plan.items}["/未整理/IMG_20240601_120000.jpg"]
    check(back.status == "pending", "撤掉之后它又回到待定")

    print("⑨ 日期归一（人填的日期别猜错）")
    from guiyun import manual

    check(manual.normalize_date("2024-05-18") == ("2024-05-18", "exact"), "2024-05-18")
    check(manual.normalize_date("20240518") == ("2024-05-18", "exact"), "20240518")
    check(manual.normalize_date("2024.5.18") == ("2024-05-18", "exact"), "2024.5.18")
    check(manual.normalize_date("2019") == ("2019-01-01", "year"), "2019 只认到年")
    try:
        manual.normalize_date("上个月那批")
        check(False, "看不懂的日期应该报错")
    except ValueError:
        check(True, "看不懂的日期直接报错，不瞎猜")

    print()
    if FAILS:
        print(f"✗ {len(FAILS)} 项没过：")
        for f in FAILS:
            print(f"  - {f}")
        print("\n—— CLI 输出 ——")
        for block in LOG:
            print(block)
        return 1
    print("✓ 全部通过：scan → plan → apply → undo 一条链没有掉链子")
    return 0


if __name__ == "__main__":
    sys.exit(main())
