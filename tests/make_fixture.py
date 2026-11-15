"""造一棵「乱网盘」出来，用来跑通 scan → plan → apply → undo。

不用真登录也能验证全部逻辑：把 backend 设成 local，根目录指到这里。
跑法：python tests/make_fixture.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / ".fixture"
DRIVE = ROOT / "drive"
HOME = ROOT / "home"
TAXONOMY = HERE / "fixture-taxonomy"

# (相对路径, 字节数)
# 名字是这棵树唯一的证据来源，所以刻意做得很乱：日期有的在目录名、有的在文件名、
# 有的只有年份、有的干脆没有；拍摄类型有的靠关键字、有的只能靠同名 .SRT 遥测。
FILES = [
    # 一整堆：日期+地点+园区区域都在目录名上，航拍靠同名 SRT 认出来
    ("未整理/2024-05-18 泡泡塘 水世界/DJI_20240518093012_0001_D.JPG", 3_000_000),
    ("未整理/2024-05-18 泡泡塘 水世界/DJI_20240518093012_0001_D.DNG", 24_000_000),
    ("未整理/2024-05-18 泡泡塘 水世界/DJI_20240518093012_0001_D.SRT", 2_000),
    # 同一天同一地点，另一个园区区域，拍摄类型写在目录名上
    ("未整理/2024-05-18 泡泡塘 花海 延时/DJI_0005.MP4", 60_000_000),
    # 日期在上一级（紧凑写法），地点和"航拍"在下一级
    ("未整理/20240519/洛阳老君山 航拍/DJI_0003.MP4", 40_000_000),
    # 活动：名字认得，日期一点线索都没有 → 应该进待定
    ("未整理/开封 第39届菊花节/DJI_0004.MP4", 45_000_000),
    ("未整理/开封 第39届菊花节/DJI_0004.SRT", 1_800),
    # 抖音拍摄类：自成一枝，但拍摄类型没把握 → 留在待定
    ("未整理/2024-08-03 抖音竖屏/DY_0007.JPG", 4_000_000),
    # 只有年份的旧归档 → 落 年/ 这一层，不编周
    ("未整理/老素材2019年/开封古城 地拍/DSC_0001.JPG", 7_000_000),
    # 散文件：文件名有日期，认不出地点 → 待定
    ("未整理/IMG_20240601_120000.jpg", 6_000_000),
    # 截图：是图片但不是素材，同样进待定（但至少要出现在方案里，别悄悄丢掉）
    ("未整理/截图/Screenshot_2024-06-02.png", 500_000),
    # 缩略图：比 min_size 小，不进方案
    ("未整理/缩略图/THM_001.jpg", 10),
]

EMPTY_DIRS = ["未整理/空文件夹"]


def main() -> int:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    DRIVE.mkdir(parents=True)
    HOME.mkdir(parents=True)
    for rel, size in FILES:
        p = DRIVE / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as fh:
            fh.truncate(size)
    for rel in EMPTY_DIRS:
        (DRIVE / rel).mkdir(parents=True, exist_ok=True)

    # 时间戳故意设成"上传时间"的样子：如果谁按 mtime 分日期，方案会立刻露馅
    stamp = time.mktime((2025, 3, 1, 12, 0, 0, 0, 0, -1))
    for p in DRIVE.rglob("*"):
        try:
            os.utime(p, (stamp, stamp))
        except OSError:
            pass

    total = sum(s for _, s in FILES)
    print(f"✓ 造好了 {DRIVE}")
    print(f"  {len(FILES)} 个文件 / {total / 1024**2:.1f} MB，另加 {len(EMPTY_DIRS)} 个空目录")
    print(f"  配置家目录（空的，等 init）：{HOME}")
    print(f"  清单目录：[taxonomy] source = {TAXONOMY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
