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
    (re.compile(r"mini\s*([34])\s*(pro)?", re.I), "DJI Mini"),
    (re.compile(r"\bair\s*([23])\s*s?", re.I), "DJI Air"),
    (re.compile(r"phantom\s*(\d)", re.I), "DJI Phantom"),
    (re.compile(r"inspire\s*(\d)", re.I), "DJI Inspire"),
    (re.compile(r"avata\s*(\d)?", re.I), "DJI Avata"),
    (re.compile(r"matrice\s*(\d+)", re.I), "DJI Matrice"),
    (re.compile(r"ilce[-_ ]?(\d{4}|[a-z]\d{3,4})", re.I), "Sony ILCE"),
    (re.compile(r"\ba7\s*(rv|iv|iii|ii|s|r|c)\b", re.I), "Sony A7"),
    (re.compile(r"eos\s*(r5|r6|r7|r8|r10|r50|r100|r3|5d|6d|1dx|90d|850d)", re.I), "Canon EOS"),
    (re.compile(r"\bx[-_ ]?t\s*(\d)\b", re.I), "Fujifilm X-T"),
    (re.compile(r"\bx[-_ ]?h\s*(\d)\b", re.I), "Fujifilm X-H"),
    (re.compile(r"\bosmo\s*(pocket|action)?", re.I), "DJI Osmo"),
    (re.compile(r"\bpocket\s*([23])\b", re.I), "DJI Pocket"),
    (re.compile(r"\bgo\s*pro\b|gopro", re.I), "GoPro"),
    (re.compile(r"\binsta\s*360\b|x[23]\b", re.I), "Insta360"),
]

_BRANDS = [
    (re.compile(r"\bdji\b|大疆", re.I), "DJI"),
    (re.compile(r"\bcanon\b|佳能", re.I), "Canon"),
    (re.compile(r"\bsony\b|索尼", re.I), "Sony"),
    (re.compile(r"\bnikon\b|尼康", re.I), "Nikon"),
    (re.compile(r"\bfuji(film)?\b|富士", re.I), "Fujifilm"),
    (re.compile(r"\bleica\b|徕卡", re.I), "Leica"),
]


def find_camera(segments: list[str], filename: str, hint_names: tuple | list = ()) -> tuple[str | None, float, str]:
    """从路径段和文件名里认机型。认不出就返回 None——不猜。

    hint_names：整目录搬时目录里那几个文件名。目录名常常不含机型，但里面的
    DJI_0001.JPG 含，漏了它整类航拍素材都会被当成「型号未知」。
    """
    hints = [h for h in hint_names if h]
    hay = " ".join([*segments, filename, *hints])
    for rx, label in _CAM_RULES:
        m = rx.search(hay)
        if m:
            model = label
            bits = [g for g in m.groups() if g]
            if bits:
                model = f"{label} {' '.join(b for b in bits if b)}".strip()
            return model, 0.80, f"名字里出现「{m.group(0)}」"
    for rx, brand in _BRANDS:
        m = rx.search(hay)
        if m:
            return brand, 0.62, f"只认出厂牌「{m.group(0)}」，无型号"
    for nm in [filename, *hints]:
        if re.match(r"(?i)^dji[_\-]", nm or ""):
            return "DJI", 0.58, "DJI_ 前缀说明是无人机素材，型号未知"
    return None, 0.0, ""
