"""云端能读到的证据，只有三样：**路径、文件名、修改时间**。

EXIF 拿不到（除非把文件下载下来），所以机型只认名字里明写的型号，日期只认目录名/
文件名里写着的日期。这条限制不是缺陷，是事实；拿不到就报待定，绝不编置信度。

判定本身不在这里了——它交给归藏（`guiyun.decide`），这样云端和本地是同一套规则。
这个文件只负责「从文字里抠出日期和机型」这两件纯文本的活。
"""

from __future__ import annotations

import re
from datetime import date as _date

# ── 日期 ────────────────────────────────────────────────────────────────────
_D_YYYYMMDD = re.compile(r"(?<!\d)(20\d{2})[-_./年](\d{1,2})[-_./月](\d{1,2})(?!\d)")
_D_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_D_YYYYMM = re.compile(r"(?<!\d)(20\d{2})[-_./年](\d{1,2})(?![\d])")
_D_YYYY = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _mk_date(y: str, m: str, d: str) -> str | None:
    try:
        return _date(int(y), int(m), int(d)).isoformat()
    except (ValueError, TypeError):
        return None


def find_date(text: str) -> tuple[str | None, float]:
    """从一段文字里找日期。返回 (YYYY-MM-DD | None, 置信度)。"""
    if not text:
        return None, 0.0
    for rx in (_D_YYYYMMDD, _D_COMPACT):
        m = rx.search(text)
        if m:
            got = _mk_date(*m.groups())
            if got:
                return got, 0.95
    m = _D_YYYYMM.search(text)
    if m:
        try:
            _date(int(m.group(1)), int(m.group(2)), 1)
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01", 0.70
        except ValueError:
            pass
    return None, 0.0


def find_year(text: str) -> str | None:
    m = _D_YYYY.search(text or "")
    return str(int(m.group(1))) if m else None


# ── 机型 ────────────────────────────────────────────────────────────────────
_CAM_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"mavic\s*3\s*(pro|classic)?", re.I), "DJI Mavic 3"),
    (re.compile(r"mavic\s*2\s*(pro|zoom)?", re.I), "DJI Mavic 2"),
    (re.compile(r"mini\s*([234])\s*(pro)?", re.I), "DJI Mini"),
    (re.compile(r"\bair\s*([23])\s*(s|pro)?", re.I), "DJI Air"),
    (re.compile(r"phantom\s*(\d)", re.I), "DJI Phantom"),
    (re.compile(r"inspire\s*(\d)", re.I), "DJI Inspire"),
    (re.compile(r"avata\s*(\d)?", re.I), "DJI Avata"),
    (re.compile(r"matrice\s*(\d+)", re.I), "DJI Matrice"),
    # ILCE-7RM3 / ILCE-7M3 / ILCE-6400 都要认：只写 \d{4} 会把 7RM3 漏成「只认出 Sony」
    (re.compile(r"ilce[-_ ]?(\d{4,5}|\d{1,2}[a-z]{1,3}\d{0,2}|\d{1,2})", re.I), "Sony ILCE"),
    (re.compile(r"\ba7\s*(rv|iv|iii|ii|s|r|c)\b", re.I), "Sony A7"),
    (re.compile(r"eos\s*(r5|r6|r7|r8|r10|r50|r100|r3|5d|6d|1dx|90d|850d)", re.I), "Canon EOS"),
    (re.compile(r"\bx[-_ ]?t\s*(\d)\b", re.I), "Fujifilm X-T"),
    (re.compile(r"\bx[-_ ]?h\s*(\d)\b", re.I), "Fujifilm X-H"),
    (re.compile(r"\bosmo\s*(pocket|action)?", re.I), "DJI Osmo"),
    (re.compile(r"\bpocket\s*([23])\b", re.I), "DJI Pocket"),
    (re.compile(r"\bgo\s*pro\b|gopro", re.I), "GoPro"),
    # 只认 Insta360 自己的写法；原来那条 `x[23]\b` 会把任何「X3」都当成 Insta360
    (re.compile(r"insta\s*360|one\s*x\s*[23]\b", re.I), "Insta360"),
]

_BRANDS = [
    (re.compile(r"\bdji\b|大疆", re.I), "DJI"),
    (re.compile(r"\bcanon\b|佳能", re.I), "Canon"),
    (re.compile(r"\bsony\b|索尼", re.I), "Sony"),
    (re.compile(r"\bnikon\b|尼康", re.I), "Nikon"),
    (re.compile(r"\bfuji(film)?\b|富士", re.I), "Fujifilm"),
    (re.compile(r"\bleica\b|徕卡", re.I), "Leica"),
]


def _cam_model(label: str, bits: list[str]) -> str:
    """把规则名和捕获到的段拼成型号。

    单字母后缀直接黏上去（Air 2 + S = Air 2S），单词后缀用空格（Mavic 3 + Pro）。
    统一用空格会得到「DJI Air 2 S」，那不是一个型号名。
    """
    core = ""
    for b in bits:
        if not b:
            continue
        if len(b) <= 2 and b.isalpha():
            core += b
        else:
            core += (" " if core else "") + b
    return f"{label} {core}".strip() if core else label


def _cam_in(text: str) -> tuple[str, str] | None:
    """在一段文字里找机型：同一段里取**最先出现**的那个，位置相同取更长的。

    返回 (型号, 命中的原文) 或 None。
    """
    best_key: tuple[int, int] | None = None
    best: tuple[str, str] | None = None
    for rx, label in _CAM_RULES:
        for m in rx.finditer(text):
            model = _cam_model(label, [g for g in m.groups() if g])
            key = (m.start(), -len(m.group(0)))
            if best_key is None or key < best_key:
                best_key, best = key, (model, m.group(0))
    return best


def find_camera(segments: list[str], filename: str, hint_names: tuple | list = ()) -> tuple[str | None, float, str]:
    """从路径段和文件名里认机型。认不出就返回 None——不猜。

    hint_names：整目录搬时目录里那几个文件名。目录名常常不含机型，但里面的
    DJI_0001.JPG 含，漏了它整类航拍素材都会被当成「型号未知」。

    取证顺序是「离文件越近越可信」：文件名 → 堆里的文件名 → 由近到远的目录。
    以前是拿整条路径拼成一个大字符串、按规则表顺序返回第一个命中——那样
    `Mavic 3 / 2024 / 某活动-Air 2S` 这种路径永远只认到 Mavic 3，明明
    文件名写着 Air 2S 也没用。
    """
    for text in [filename, *hint_names]:
        if not text:
            continue
        hit = _cam_in(text)
        if hit:
            return hit[0], 0.80, f"文件名里出现「{hit[1]}」"
    for seg in reversed([s for s in segments if s]):
        hit = _cam_in(seg)
        if hit:
            return hit[0], 0.80, f"目录名里出现「{hit[1]}」"
    hay = " ".join([*segments, filename, *[h for h in hint_names if h]])
    for rx, brand in _BRANDS:
        m = rx.search(hay)
        if m:
            return brand, 0.62, f"只认出厂牌「{m.group(0)}」，无型号"
    for nm in [filename, *[h for h in hint_names if h]]:
        if re.match(r"(?i)^dji[_\-]", nm or ""):
            return "DJI", 0.58, "DJI_ 前缀说明是无人机素材，型号未知"
    return None, 0.0, ""
