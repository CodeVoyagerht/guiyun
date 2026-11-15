"""人工钉住的判定。

云端读不到 EXIF：机型、日期只能从目录名和文件名推。推不出来的时候，人一句话
就说清了——那就得让人说了算，否则「待定」会变成一个永远清不掉的筐。

钉的字段跟归藏一致（地点 / 园区区域 / 活动 / 分支 / 拍摄类型 / 日期 / 机型），
套用时也交给归藏的那套逻辑去重算分支与状态——不然两个工具会各自维护一份
「什么时候算待定」的规矩，慢慢就分家了。

文件是 `~/.guiyun/manual.json`，人是可以直接改的，格式：

    {
      "/未整理/泉州西街/视频": {
        "place": "泉州西街", "area": "", "branch": "region",
        "shot": "航拍", "date": "2024-03-15", "date_kind": "exact",
        "camera": "DJI Mavic 3", "set_at": 1763200000
      }
    }

`date_kind` 只有 `exact`（YYYY-MM-DD）和 `year`（只知道年）两种：只认得年就落
`2019/`，不硬编 `2019-01-01`——假日子会在树上留一辈子。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .rules import find_date, find_year

BRANCH_ALIASES = {
    "区域": "region", "region": "region",
    "活动": "activity", "activity": "activity",
    "抖音": "douyin", "douyin": "douyin",
}


def path(cfg: Config) -> Path:
    return cfg.home / "manual.json"


def load(cfg: Config) -> dict[str, dict]:
    p = path(cfg)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"人工判定表读不了：{p}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"人工判定表格式不对（该是对象）：{p}")
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save(cfg: Config, data: dict[str, dict]) -> Path:
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return p


# ── 输入归一 ────────────────────────────────────────────────────────────────
_NOW = re.compile(r"^(今天|today)$", re.I)


def normalize_date(text: str | None) -> tuple[str | None, str]:
    """把用户给的日期掰成 (iso, exact|year)。认不出就抛 ValueError——宁可报错，
    也不猜一个日子落进树里。"""
    if not text:
        return None, ""
    s = str(text).strip()
    if _NOW.match(s):
        return time.strftime("%Y-%m-%d"), "exact"
    iso, conf = find_date(s)
    if iso and conf >= 0.70:
        return iso, "exact"
    year = find_year(s)
    if year:
        return f"{year}-01-01", "year"
    raise ValueError(f"看不懂这个日期：{text!r}。给 2024-05-18、20240518、2024.5.18，或者只给年份 2019。")


def normalize_branch(value: str | None) -> str | None:
    if not value:
        return None
    key = str(value).strip().lower()
    if key not in BRANCH_ALIASES:
        raise ValueError(f"分支只能是 区域 / 活动 / 抖音，现在是 {value!r}")
    return BRANCH_ALIASES[key]


def normalize(text: str | None) -> str | None:
    if text is None:
        return None
    s = str(text).strip().strip("/")
    return s or None


# ── 增删 ────────────────────────────────────────────────────────────────────
_FIELDS = {
    "place": "地点",
    "area": "园区区域",
    "activity": "活动",
    "shot": "拍摄类型",
    "camera": "机型",
}


def put(
    cfg: Config,
    src: str,
    *,
    place: str | None = None,
    area: str | None = None,
    activity: str | None = None,
    branch: str | None = None,
    shot: str | None = None,
    date: str | None = None,
    camera: str | None = None,
) -> dict:
    """钉一条。只写你给了的字段，其余保持原样（分次钉也可以）。"""
    data = load(cfg)
    src = src.strip()
    if not src.startswith("/"):
        src = "/" + src
    entry = dict(data.get(src) or {})
    touched: list[str] = []

    for field, label in _FIELDS.items():
        given = {"place": place, "area": area, "activity": activity,
                 "shot": shot, "camera": camera}[field]
        if given is None:
            continue
        value = normalize(given)
        if value is None:
            entry.pop(field, None)
            touched.append(f"{label}（已清空）")
        else:
            entry[field] = value
            touched.append(f"{label} = {value}")

    if branch is not None:
        value = normalize_branch(branch)
        if value is None:
            entry.pop("branch", None)
            touched.append("分支（已清空）")
        else:
            entry["branch"] = value
            touched.append(f"分支 = {value}")

    if date is not None:
        iso, kind = normalize_date(date)
        if iso is None:
            entry.pop("date", None)
            entry.pop("date_kind", None)
            touched.append("日期（已清空）")
        else:
            entry["date"] = iso
            entry["date_kind"] = kind
            touched.append(f"日期 = {iso}" + ("（只认到年）" if kind == "year" else ""))

    if not touched:
        raise ValueError(
            "没给要钉什么：至少带 --place / --area / --activity / --branch / --shot / --date / --camera 之一。"
        )
    entry["set_at"] = int(time.time())
    data[src] = entry
    save(cfg, data)
    return entry


def drop(cfg: Config, src: str) -> bool:
    data = load(cfg)
    src = src.strip() if src.startswith("/") else "/" + src.strip()
    if src not in data:
        return False
    del data[src]
    save(cfg, data)
    return True


def for_src(data: dict[str, dict], src: str) -> dict:
    """精确命中优先；退一格找祖先（钉了整个目录，里面的文件也算数）。"""
    if src in data:
        return data[src]
    parent = src.rsplit("/", 1)[0]
    while parent and parent != "/":
        if parent in data:
            return data[parent]
        parent = parent.rsplit("/", 1)[0]
    return {}


# ── 套用 ────────────────────────────────────────────────────────────────────
def apply(v, entry: dict, when: datetime | None = None, gcfg=None):
    """把人工判定盖在机器判定上。

    分类那几样（地点/园区区域/活动/分支/拍摄类型）交给归藏的 manual.apply，
    状态与分支的重算也就跟本地库同一套；日期和机型只在归云这边补，
    因为它们是「文本推不出来」的那两样。
    """
    if not entry:
        return v
    if v.decision is not None:
        from guicang import manual as gmanual

        payload = {}
        if entry.get("place"):
            payload["region"] = entry["place"]
        if entry.get("area"):
            payload["area"] = entry["area"]
        if entry.get("activity"):
            payload["activity"] = entry["activity"]
            payload["edition"] = entry.get("edition") or ""
        if entry.get("branch"):
            payload["branch"] = entry["branch"]
        if entry.get("shot"):
            payload["shot_type"] = entry["shot"]
        if payload:
            v.decision = gmanual.apply(v.decision, payload, when or v.when)
            if payload.get("region") and gcfg is not None:
                # 钉了地点名，省/市/园区区域得从清单里补回来——
                # 不然路径里会缺一层（比如「开封/开封古城」只剩地点）
                from guicang import classify as gclassify

                gclassify.settle_place(v.decision, gcfg)
            v.project = getattr(v.decision, "region", None)
            v.project_conf = float(getattr(v.decision, "region_conf", 0.0))
            for label, key in (
                ("地点", "region"), ("园区区域", "area"),
                ("活动", "activity"), ("分支", "branch"), ("拍摄类型", "shot_type"),
            ):
                if payload.get(key):
                    note = f"人工钉住{label}：{payload[key]}"
                    if note not in v.reasons:
                        v.reasons.insert(0, note)

    if entry.get("date"):
        v.date = entry["date"]
        v.date_kind = entry.get("date_kind") or "exact"
        v.date_conf = 1.0
        try:
            v.when = datetime.fromisoformat(entry["date"])
        except ValueError:
            v.when = v.when
        kind = "（只认到年）" if v.date_kind == "year" else ""
        v.reasons.insert(0, f"人工钉住日期：{entry['date']}{kind}")

    if entry.get("camera"):
        v.camera = entry["camera"]
        v.camera_conf = 1.0
        v.reasons.insert(0, f"人工钉住机型：{entry['camera']}")

    if v.is_auto:
        v.pending_why = ""
    elif not v.pending_why:
        missing = []
        if not v.project and not v.activity and v.branch != "douyin":
            missing.append("地点")
        if not v.date or v.date_kind not in ("exact", "year"):
            missing.append("日期")
        v.pending_why = ("人工钉过，但还缺：" + "、".join(missing)) if missing else v.pending_why
    return v
