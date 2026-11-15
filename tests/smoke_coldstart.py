"""冷启动冒烟：完全模拟一个新用户拿到这个仓库之后会做的每一步。

跟 tests/e2e.py 的区别：e2e 直接在进程里调 API、造配置；这里**只走命令行**，
配置是 `guiyun init` 生成、再用文本编辑改出来的（就是人在编辑器里干的事）。
验的是「模板能改、配置能读、命令能跑、结果对」。

    py tests/smoke_coldstart.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Windows 有 TEMP，类 Unix 只有 TMPDIR；两个都没有时 tempfile 会退回 /tmp。
TMP = os.path.join(tempfile.gettempdir(), "guiyun-coldstart")


def run(args, home, *, expect=0):
    env = dict(os.environ, GUIYUN_HOME=home, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "guiyun", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if expect is not None and proc.returncode != expect:
        print(out)
        raise SystemExit(f"✗ {' '.join(args)} 退出码 {proc.returncode}，期望 {expect}")
    return out


def say(text):
    print(text)


def touch(path: str, size: int):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    stamp = time.mktime((2025, 3, 1, 12, 0, 0, 0, 0, -1))   # mtime 陷阱：全是上传日
    os.utime(path, (stamp, stamp))


def _wipe(path: str, tries: int = 5) -> None:
    """清掉上一轮留下的临时库。

    Windows 上 sqlite 句柄偶尔比进程晚一步释放，`rmtree` 会抛
    PermissionError(32)；重试几次再放弃，别让清理把测试本身弄红。
    """
    if not os.path.isdir(path):
        return
    for _ in range(tries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(0.4)
    shutil.rmtree(path, ignore_errors=True)


def main() -> int:
    _wipe(TMP)
    home = os.path.join(TMP, "home")
    drive = os.path.join(TMP, "drive")

    # ── 造一个「网盘」：一个乱目录 ──────────────────────────────────────
    src_pile = os.path.join(drive, "未整理", "2024-05-18 厦门鼓浪屿 航拍")
    dst_pile = os.path.join(drive, "摄影库", "区域", "厦门", "厦门鼓浪屿", "2024",
                            "2024-05-18_05-24", "航拍", "2024-05-18 厦门鼓浪屿 航拍")
    touch(os.path.join(src_pile, "DJI_0001.JPG"), 2048)
    touch(os.path.join(src_pile, "DJI_0001.DNG"), 4096)
    touch(os.path.join(drive, "未整理", "泉州西街", "IMG_0007.JPG"), 1024)
    touch(os.path.join(drive, "未整理", "杂项", "随便一张.jpg"), 512)
    say(f"造好了假网盘：{drive}")

    # ── ① init ────────────────────────────────────────────────────────
    os.makedirs(home, exist_ok=True)
    say("\n① guiyun init")
    out = run(["init"], home)
    assert "配置写在" in out, out
    cfg_path = os.path.join(home, "guiyun.toml")
    assert os.path.isfile(cfg_path), "init 没写出配置"
    say("  ✓ 配置生成了")

    # ── ② 人改配置（编辑器里的活，这里用文本替换模拟）──────────────────
    say("\n② 改配置：换 local 后端 + 指到清单目录")
    text = open(cfg_path, encoding="utf-8").read()
    text = text.replace('kind = "baidu"', 'kind = "local"', 1)
    original = text
    for line in text.splitlines():
        if line.startswith('root = ""'):
            # 先按人最可能的方式粘路径：原样反斜杠塞进双引号里 —— 这是坏的。
            # 这里故意用 Windows 字样而不是本机路径：TOML 把 `\素` 当非法转义，
            # 这个坑跟平台无关（CI 的 Linux 上没有反斜杠，用真路径就复现不出来）。
            text = text.replace(line, r'root = "E:\素材\drive"', 1)
            break
    open(cfg_path, "w", encoding="utf-8").write(text)

    # 这个坑必须被接住，而且要说人话（Windows 路径里的 \ 在双引号里是转义符）
    out = run(["status"], home, expect=1)
    assert "反斜杠" in out and "单引号" in out, "配置写坏时没给出能照着改的提示\n" + out
    say("  ✓ 路径粘错时给的提示是能照着改的一句人话")

    # 地点清单：跟本地归藏共用同一份 places.toml（不在 guiyun.toml 里列项目了）
    taxdir = os.path.join(TMP, "taxonomy")
    os.makedirs(taxdir, exist_ok=True)
    with open(os.path.join(taxdir, "places.toml"), "w", encoding="utf-8") as handle:
        handle.write(
            '[[place]]\n'
            'name = "厦门鼓浪屿"\n'
            'province = "福建"\n'
            'city = "厦门"\n'
            'aliases = ["鼓浪屿"]\n\n'
            '[[place]]\n'
            'name = "泉州西街"\n'
            'province = "福建"\n'
            'city = "泉州"\n'
            'aliases = ["西街"]\n'
        )
    with open(os.path.join(taxdir, "activities.toml"), "w", encoding="utf-8") as handle:
        handle.write(
            '[[activity]]\n'
            'name = "鼓浪屿音乐节"\n'
            'aliases = ["音乐节"]\n'
        )

    text = original.replace(
        [ln for ln in original.splitlines() if ln.startswith('root = ""')][0],
        f"root = '{drive}'",           # 单引号：TOML 字面量字符串，路径直接粘
        1,
    )
    text = text.replace('source = ""', f"source = '{taxdir}'", 1)
    open(cfg_path, "w", encoding="utf-8").write(text)
    say("  ✓ 改成单引号、清单指到 taxonomy 目录后配置能读了")

    # ── ③ status ──────────────────────────────────────────────────────
    say("\n③ guiyun status")
    out = run(["status"], home)
    assert "2 个地点" in out, "清单里的地点没被读到\n" + out
    say("  ✓ 清单读进来了（2 个地点），后端是 local")

    # ── ④ scan ────────────────────────────────────────────────────────
    say("\n④ guiyun scan")
    out = run(["scan"], home)
    say("  " + out.strip().splitlines()[-1])
    assert "4" <= out or "个文件" in out, out

    # ── ⑤ plan ────────────────────────────────────────────────────────
    say("\n⑤ guiyun plan")
    out = run(["plan"], home)
    assert "2024-05-18_05-24/" in out, out
    plan = json.loads(run(["plan", "--json"], home))
    dsts = {item["src"]: item for item in plan["items"]}
    auto = [i for i in dsts.values() if i["status"] == "auto"]
    assert len(auto) == 1, [i["src"] for i in auto]
    assert auto[0]["dst"] == (
        "/摄影库/区域/厦门鼓浪屿/2024/2024-05-18_05-24/航拍/2024-05-18 厦门鼓浪屿 航拍"
    ), auto[0]["dst"]
    pending = {i["src"]: i["dst"] for i in dsts.values() if i["status"] == "pending"}
    # dst 永远是「目标目录 + 搬的单位名」，不是只到目录 —— 漏了末段名字就少一截
    assert pending.get("/未整理/泉州西街") == "/摄影库/待定/未知时间/地拍/泉州西街", pending
    # 拍摄类型也是兜底的：`随便一张.jpg` 既没机型也没前缀，落到「其他」而不是
    # 「地拍」——默认值不该冒充结论（decide._blank_default_shot 干的活）。
    assert pending.get("/未整理/杂项") == "/摄影库/待定/未知时间/其他/杂项", pending
    say("  ✓ 厦门鼓浪屿 2024-05-18 整堆可搬（周期层）；泉州西街进待定（只有上传时间，不编日子）")

    # ── ⑥ review 钉住泉州西街，再 plan ───────────────────────────────
    say("\n⑥ guiyun review --set 钉住泉州西街")
    out = run(["review", "--set", "/未整理/泉州西街", "--place", "泉州西街",
               "--shot", "地拍", "--date", "2024-03-15"], home)
    assert "可自动搬" in out, out
    plan = json.loads(run(["plan", "--json"], home))
    dst = {i["src"]: i for i in plan["items"]}["/未整理/泉州西街"]
    assert dst["status"] == "auto", dst
    assert dst["dst"] == "/摄影库/区域/泉州/泉州西街/2024/2024-03-09_03-15/地拍/泉州西街", dst["dst"]
    say("  ✓ 钉住后立刻能搬，落位对（有两个市之后，市层才出现）")

    # ── ⑦ explain ─────────────────────────────────────────────────────
    say("\n⑦ guiyun explain")
    out = run(["explain", "/未整理/杂项"], home)
    assert "为什么不动" in out or "待定" in out, out
    say("  ✓ 说清了为什么不动")

    # ── ⑧ apply --dry-run 不动盘 ──────────────────────────────────────
    say("\n⑧ guiyun apply --dry-run")
    run(["apply", "--dry-run"], home)
    assert os.path.isfile(os.path.join(src_pile, "DJI_0001.JPG"))
    say("  ✓ dry-run 一个文件都没动")

    # ── ⑨ apply --yes ─────────────────────────────────────────────────
    say("\n⑨ guiyun apply --yes")
    out = run(["apply", "--yes"], home)
    assert os.path.isfile(os.path.join(dst_pile, "DJI_0001.JPG")), "整堆没搬到位\n" + out
    assert os.path.isfile(os.path.join(dst_pile, "DJI_0001.DNG")), "DNG 没跟着走"
    say("  ✓ 整堆搬到位，RAW 跟着 JPG 一起走")

    # ── ⑩ clean + undo ────────────────────────────────────────────────
    say("\n⑩ guiyun clean --yes → undo")
    run(["clean", "--yes"], home)
    assert not os.path.isdir(src_pile)
    out = run(["status"], home)
    m = re.search(r"\b(\d{8}-\d{6})\b", out)      # run_id 形如 20261115-210712
    assert m, "status 里找不到 run_id\n" + out
    run_id = m.group(1)
    run(["undo", run_id], home)
    assert os.path.isfile(os.path.join(src_pile, "DJI_0001.JPG")), "先 clean 后 undo 没还原回去"
    say(f"  ✓ clean 清了空壳，undo {run_id} 把它搬回来了（源目录自动重建）")

    say("\n✓ 冷启动全流程通过：init → 改配置 → status → scan → plan → review → "
        "explain → apply → clean → undo")
    say(f"（临时目录：{TMP}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
