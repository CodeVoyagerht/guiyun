"""规则小测：机型和日期的取证顺序、几个真实型号名。

这些东西错了不会崩，只会安静地把一整类素材放错地方——所以单独钉一遍。
跟 e2e 的区别：这里不走网盘、不建索引，纯函数级，秒级跑完。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guiyun.rules import find_camera, find_date, find_year  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, what: str) -> None:
    if cond:
        print(f"  ✓ {what}")
    else:
        FAILS.append(what)
        print(f"  ✗ {what}")


def main() -> int:
    print("① 型号名要能直接看懂（不是「DJI Air 2 S」这种）")
    check(find_camera(["某活动-Air 2S"], "DSC_0001.JPG")[0] == "DJI Air 2S", "Air 2S")
    check(find_camera([], "ILCE-7RM3_0001.ARW")[0] == "Sony ILCE 7RM3", "ILCE-7RM3")
    check(find_camera([], "ILCE-6400_0001.JPG")[0] == "Sony ILCE 6400", "ILCE-6400")
    check(find_camera(["Mavic 3 Pro 航拍"], "DJI_0001.JPG")[0] == "DJI Mavic 3 Pro", "Mavic 3 Pro")
    check(find_camera(["Mini 2 首飞"], "DJI_0007.JPG")[0] == "DJI Mini 2", "Mini 2（不是只认 3/4）")

    print("② 认不出的就别硬认")
    check(find_camera(["室内"], "X3 成品.jpg")[0] is None, "「X3」不当成 Insta360")
    check(find_camera([], "ONE X2.mp4")[0] == "Insta360", "ONE X2 仍然认成 Insta360")
    check(find_camera(["未整理"], "DSC_0001.JPG")[0] is None, "什么都没有就返回 None")

    print("③ 取证顺序：文件名 > 堆里的文件名 > 近的目录 > 远的目录")
    check(find_camera(["Mavic 3", "2024", "某活动-Air 2S"], "DSC_0001.JPG")[0] == "DJI Air 2S",
          "近的目录（Air 2S）压过远的（Mavic 3）")
    check(find_camera(["Mavic 3"], "DSC_0001.JPG", ("Air 2S",))[0] == "DJI Air 2S",
          "堆里的文件名压过目录名")
    check(find_camera(["Mavic 3"], "ILCE-7RM3.ARW")[0] == "Sony ILCE 7RM3",
          "文件名压过目录名")

    print("④ 只认出厂牌、只认 DJI_ 前缀，置信度要低")
    cam, conf, _ = find_camera(["2024-05-18-鼓浪屿-SONY"], "DSC_0001.JPG")
    check(cam == "Sony" and conf == 0.62, f"只认出牌 {cam} {conf}")
    cam, conf, _ = find_camera([], "DJI_20240518093012_0001_D.SRT")
    check(cam == "DJI" and conf == 0.58, f"只有前缀 {cam} {conf}")

    print("⑤ 日期")
    for text, want in [
        ("20240518", "2024-05-18"),
        ("2024-05-18 厦门", "2024-05-18"),
        ("2024.5.18", "2024-05-18"),
        ("2024年5月18日", "2024-05-18"),
    ]:
        got, conf = find_date(text)
        check(got == want and conf >= 0.9, f"{text} → {got} ({conf})")
    check(find_date("随便什么")[0] is None, "没日期就返回 None")
    check(find_year("老素材2019年") == "2019", "只认到年份 2019")

    if FAILS:
        print(f"\n✗ {len(FAILS)} 项没过：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("\n✓ 规则小测全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
