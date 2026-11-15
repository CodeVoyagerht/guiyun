#!/usr/bin/env python3
"""把 guiyun（归云）技能装到各家的 skill 目录。

各家读的位置不一样（而且 Codex 与 Gemini 更认 ``.agents/skills``），所以做法是
**一份真源、多处落地**：本目录永远是唯一真源，安装只是一次复制（默认）或软链。

    python skills/guiyun/scripts/install.py                 # 装到本机所有已知位置
    python skills/guiyun/scripts/install.py --target dsh    # 只装 DeepSeek Harness
    python skills/guiyun/scripts/install.py --link          # 用软链（仅 POSIX / 开了开发者模式的 Windows）
    python skills/guiyun/scripts/install.py --dir D:/skills # 装到自定义目录（父级）
    python skills/guiyun/scripts/install.py --dry-run       # 只看会写哪

设计取舍：Windows 上默认**复制**而不是软链。软链要管理员或开发者模式，
一个"装不上"的技能比"装了两份、要记得重新同步"的技能更糟。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)          # .../skills/guiyun
SKILL_NAME = os.path.basename(SKILL_DIR)
HOME = os.path.expanduser("~")

# (键, 说明, 目标父目录)。同一份技能可能被多处读，重复装不冲突。
TARGETS = [
    ("dsh", "DeepSeek Harness（配合 preset/ 里的「归云」预设）",
     os.path.join(HOME, ".dsh", "skills")),
    ("agents", "通用 .agents（Cursor / Codex / Gemini 原生识别）",
     os.path.join(HOME, ".agents", "skills")),
    ("claude", "Claude Code", os.path.join(HOME, ".claude", "skills")),
    ("cursor", "Cursor", os.path.join(HOME, ".cursor", "skills")),
    ("codex", "Codex", os.path.join(HOME, ".codex", "skills")),
    ("gemini", "Gemini CLI", os.path.join(HOME, ".gemini", "skills")),
]


def _is_our_skill(path: str) -> bool:
    """目标目录是不是**这个**技能的一份安装。

    覆盖前必须问这一句：用户可能把 ``--dir`` 指到自己的代码目录，而那里正好有个
    叫 ``guiyun`` 的项目。``rmtree`` 一个同名目录是不可撤销的，宁可拒绝。
    """
    skill_md = os.path.join(path, "SKILL.md")
    if not os.path.isfile(skill_md):
        return False
    try:
        with open(skill_md, encoding="utf-8") as handle:
            text = handle.read(2048)
    except OSError:
        return False
    match = re.search(r"^name:\s*(.+?)\s*$", text, re.M)
    if not match:
        return False
    return match.group(1).strip().strip("\"'") == SKILL_NAME


def _install_one(dest_root: str, *, link: bool, dry: bool,
                 force: bool, force_any: bool = False) -> str:
    dest = os.path.join(dest_root, SKILL_NAME)
    if os.path.exists(dest):
        if not force:
            return f"跳过（已存在）: {dest}"
        if not dry:
            if not force_any and not _is_our_skill(dest):
                return (f"拒绝覆盖: {dest}（里面没有 SKILL.md，或 name 不是 "
                        f"{SKILL_NAME}）——确认是你想删的目录后加 --force-any")
            try:
                shutil.rmtree(dest)
            except OSError as exc:
                return f"删不掉旧目录（{exc}）: {dest}"
    if dry:
        return f"会写入: {dest}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if link:
        try:
            os.symlink(SKILL_DIR, dest, target_is_directory=True)
            return f"已软链: {dest} → {SKILL_DIR}"
        except OSError as exc:            # 权限不足就诚实退回复制
            print(f"软链失败（{exc}），改为复制。", file=sys.stderr)
    shutil.copytree(SKILL_DIR, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".tmp"))
    return f"已复制: {dest}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安装 guiyun（归云）技能")
    ap.add_argument("--target", default="all",
                    help="all | " + " | ".join(k for k, _, _ in TARGETS) + " | dir")
    ap.add_argument("--dir", help="--target dir 时的父目录")
    ap.add_argument("--link", action="store_true", help="用软链代替复制")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的安装")
    ap.add_argument("--force-any", action="store_true",
                    help="连同名但不是技能的目录也删（危险，先自己确认）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(os.path.join(SKILL_DIR, "SKILL.md")):
        print(f"这里不是技能目录: {SKILL_DIR}", file=sys.stderr)
        return 1

    if args.target == "dir":
        if not args.dir:
            print("--target dir 需要同时给 --dir", file=sys.stderr)
            return 2
        roots = [("custom", "自定义目录", os.path.abspath(args.dir))]
    elif args.target == "all":
        roots = TARGETS
    else:
        roots = [t for t in TARGETS if t[0] == args.target]
        if not roots:
            print(f"未知 target: {args.target}", file=sys.stderr)
            return 2

    print(f"真源: {SKILL_DIR}")
    count = 0
    for _key, label, root in roots:
        print(f"\n{label}")
        print("  " + _install_one(root, link=args.link, dry=args.dry_run,
                                  force=args.force, force_any=args.force_any))
        count += 1
    print(f"\n{count} 个位置处理完毕"
          + ("（dry-run，未真正写入）" if args.dry_run else "") + "。")
    print("\n命令行本体另装：pip install -e <仓库根目录>  → 之后可直接跑 guiyun status")
    print("百度网盘还要一份 BaiduPCS-Go：仓库 .tools/bpc/ 里带了一个，"
          "或在 ~/.guiyun/guiyun.toml 的 [backend.baidu] cli 指路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
