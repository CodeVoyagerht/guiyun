"""判定与落位：直接借「归藏」（guicang）的规则引擎。

天天要的是**一套规则**。云端（归云）和本地（归藏）如果各写一份判定，两个月后
必然长歪——一处调了阈值、另一处忘了；一处改了省/市折叠、另一处还在按项目分。

所以归云不再自己判：它把「路径文本 + 找到的日期 + 名字里认出的机型」拼成一个
归藏的 Probe，交给 `guicang.classify.decide()` 判，再让 `guicang.taxonomy` 出路径。
地点清单、活动清单也读同一份 `places.toml` / `activities.toml`。

云端读不到 EXIF，这是事实，不是缺陷：拿来判的证据只有路径、文件名、修改时间，
归藏的阈值恰好能承认这个限度——证据不够就进待定，不编置信度。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import ACTIVITIES_FILE, PLACES_FILE, Config, ConfigError
from .rules import find_camera, find_date, find_year

_G = None


def _gc():
    """导入归藏。没装就报一句人话，别抛 ImportError 让人猜。"""
    global _G
    if _G is not None:
        return _G
    try:
        from guicang import classify, manual, model, taxonomy
        from guicang.config import (
            Config as GConfig,
            load_activities,
            load_places,
        )
        from guicang.model import Probe
    except ImportError as exc:  # pragma: no cover - 只在没装归藏时走到
        raise ConfigError(
            "归云现在借用「归藏」的分类规则，得先把归藏装上：\n"
            "  pip install -e <归藏仓库>\n"
            f"（原始报错：{exc}）"
        ) from exc
    _G = {
        "classify": classify,
        "taxonomy": taxonomy,
        "manual": manual,
        "model": model,
        "GConfig": GConfig,
        "Probe": Probe,
        "load_places": load_places,
        "load_activities": load_activities,
    }
    return _G


def _taxonomy_dir(cfg: Config) -> Path:
    """地点/活动清单从哪读。

    优先 `[taxonomy] source`（一般指到本地素材库的 `.guicang` 目录），
    其次 `~/.guiyun/`（自己放一份）。两处都没有就返回空目录——判定会全部进待定，
    并在原因里说清楚缺什么。
    """
    if cfg.taxonomy_source:
        return Path(cfg.taxonomy_source).expanduser()
    return Path(cfg.path).parent


def taxonomy_config(cfg: Config):
    """把归云的配置翻译成归藏的一份 Config（只用来判定和拼路径，不碰磁盘）。"""
    g = _gc()
    directory = _taxonomy_dir(cfg)
    places = g["load_places"](directory / PLACES_FILE) if (directory / PLACES_FILE).is_file() else []
    activities = (
        g["load_activities"](directory / ACTIVITIES_FILE)
        if (directory / ACTIVITIES_FILE).is_file()
        else []
    )
    return g["GConfig"](
        root=Path(os.path.abspath(str(directory))) if directory else Path.cwd(),
        tree="_guiyun",                # 只用来算排除名，归云不落这棵树
        time_depth=cfg.layout_time,
        region_enabled=True,
        activity_enabled=True,
        camera_layer=cfg.layout_camera,
        places=places,
        activities=activities,
    )


def missing_lists(cfg: Config) -> list[str]:
    """清单缺了就说清楚缺什么，别让人对着「全部待定」发呆。"""
    directory = _taxonomy_dir(cfg)
    where = directory if cfg.taxonomy_source else f"{directory}（或 ~/.guiyun/）"
    out: list[str] = []
    for name, label in ((PLACES_FILE, "地点"), (ACTIVITIES_FILE, "活动")):
        if not (directory / name).is_file():
            out.append(f"{name}（{label}清单，找的是 {where}）")
    return out


@dataclass
class Verdict:
    """一条（或一整个文件夹）的判定结果，字段与归藏的 Decision 对齐。"""

    decision: object = None            # guicang.model.Decision
    when: datetime | None = None
    date: str | None = None            # YYYY-MM-DD | YYYY
    date_conf: float = 0.0
    date_kind: str = ""                # exact | year | mtime
    project: str | None = None         # 兼容旧字段名：这里是「地点」
    project_conf: float = 0.0
    camera: str | None = None
    camera_conf: float = 0.0
    reasons: list[str] = None
    pending_why: str = ""

    def __post_init__(self):
        if self.reasons is None:
            self.reasons = []

    @property
    def is_auto(self) -> bool:
        if self.decision is None:
            return False
        # 只认到年份也能搬：归藏那边就是落「年/」这一层，年限是真的，周才是编的。
        # 真正不能自己动手的是「只有上传时间」（date_kind == "mtime"）。
        return getattr(self.decision, "status", "") == "auto" and self.date_kind != "mtime"

    @property
    def branch(self) -> str:
        return getattr(self.decision, "branch", "region") if self.decision else "region"

    @property
    def place(self) -> str | None:
        return getattr(self.decision, "region", None) if self.decision else None

    @property
    def area(self) -> str:
        return getattr(self.decision, "area", "") if self.decision else ""

    @property
    def activity(self) -> str | None:
        return getattr(self.decision, "activity", None) if self.decision else None

    @property
    def shot_type(self) -> str:
        return getattr(self.decision, "shot_type", "其他") if self.decision else "其他"

    @property
    def shot_conf(self) -> float:
        return float(getattr(self.decision, "shot_conf", 0.0)) if self.decision else 0.0


def decide(
    cfg: Config,
    path_segments: list[str],
    filename: str,
    mtime: float | None = None,
    hint_names: tuple | list = (),
    gcfg=None,
) -> Verdict:
    """核心判定：先找日期和机型（云端唯一可靠的文本证据），再交给归藏判。"""
    g = _gc()
    out = Verdict()
    gcfg = gcfg or taxonomy_config(cfg)

    # ── 1) 日期：文件名 > 最近的上级目录 > 更远的目录 > 文件夹里文件名一致 ──
    iso, conf = find_date(filename)
    if iso and conf >= 0.9:
        out.date, out.date_conf, out.date_kind = iso, conf, "exact"
        out.reasons.append(f"文件名里有日期 {iso}")
    else:
        for depth_from_file, seg in enumerate(reversed(path_segments)):
            got, c2 = find_date(seg)
            if got:
                penalty = 0.0 if depth_from_file == 0 else 0.04 * depth_from_file
                out.date, out.date_conf, out.date_kind = got, max(0.55, c2 - penalty), "exact"
                out.reasons.append(f"目录「{seg}」里有日期 {got}")
                break
        if not out.date and hint_names:
            hits: dict[str, int] = {}
            for nm in hint_names:
                got, c3 = find_date(nm)
                if got and c3 >= 0.9:
                    hits[got] = hits.get(got, 0) + 1
            if len(hits) == 1:
                (only, cnt) = next(iter(hits.items()))
                out.date, out.date_conf, out.date_kind = only, 0.85, "exact"
                out.reasons.append(f"文件夹里 {cnt} 个文件名都写着 {only}")
        if not out.date:
            year = find_year(filename) or (find_year(" ".join(path_segments[-2:])) if path_segments else None)
            if year:
                out.date, out.date_conf, out.date_kind = year, 0.50, "year"
                out.reasons.append(f"只找到年份 {year}，就落 年/ 这一层")
            elif mtime:
                out.date = datetime.fromtimestamp(mtime).date().isoformat()
                out.date_conf, out.date_kind = 0.45, "mtime"
                out.reasons.append(f"只有修改时间 {out.date}（网盘上可能是上传时间，不敢用）")

    # ── 2) 机型：名字里明写的型号 ──────────────────────────────────────────────
    cam, cconf, creason = find_camera(path_segments, filename, hint_names)
    out.camera, out.camera_conf = cam, cconf
    if creason:
        out.reasons.append(creason)

    # ── 3) 交给归藏判 ────────────────────────────────────────────────────────
    rel = "/".join([*path_segments, filename] if filename else list(path_segments))
    make, model = _split_make(cam)
    when = _as_when(out.date, out.date_kind)
    # 整堆搬的时候，filename 是目录名——它不带 `IMG_`/`MVI_` 这类前缀，
    # 拍摄类型就只剩默认值。拿堆里第一个媒体文件的名字顶上，前缀证据才用得上。
    member = _member_name(hint_names)
    probe_name = filename if _is_media(filename) else (member or filename)
    probe = g["Probe"](
        path=Path("/".join([*path_segments, probe_name]) if probe_name else (rel or "网盘")),
        kind="video" if _looks_video(probe_name or filename) else "photo",
        mtime=float(mtime or 0),
        captured_at=when if out.date_kind in ("exact", "year") else None,
        captured_source="filename",
        captured_conf=out.date_conf,
        make=make,
        model=model,
    )
    decision = g["classify"].decide(probe, rel, gcfg)
    out.decision, out.when = decision, when
    hint = _srt_hint(decision, filename, hint_names, g)
    if hint:
        out.reasons.append(hint)
    out.project = getattr(decision, "region", None)
    out.project_conf = float(getattr(decision, "region_conf", 0.0))
    for note in getattr(decision, "notes", []) or []:
        if note not in out.reasons:
            out.reasons.append(note)

    # ── 4) 待定原因 ──────────────────────────────────────────────────────────
    if not out.is_auto:
        out.pending_why = _why_pending(cfg, gcfg, out, decision)
    return out


def _stem(name: str) -> str:
    head, dot, _ = name.rpartition(".")
    return (head if dot else name).strip().lower()


def _srt_hint(decision, filename: str, hint_names, g) -> str:
    """同一堆里有同名 `.SRT` 遥测 → 这堆是无人机拍的。

    云端读不到 EXIF，但**文件名的配对比**能读到：大疆的素材一定是
    `DJI_0001.MP4` 配 `DJI_0001.SRT`，这个配对数只有无人机才会产生。
    只把「已经判成航拍但没把握（0.55）」的那一档抬到 0.80，
    不去改判成地拍的东西——SRT 也可能只是人塞进同目录的字幕。
    """
    names = [n for n in [filename, *list(hint_names or ())] if n]
    srt = {_stem(n) for n in names if n.lower().endswith(".srt")}
    if not srt:
        return ""
    media = [_stem(n) for n in names if not n.lower().endswith(".srt")]
    if not any(m in srt for m in media):
        return ""
    conf = float(getattr(decision, "shot_conf", 0.0) or 0.0)
    if conf >= 0.80:
        return ""
    # 路径里明写「地拍」的，就别拿 SRT 去改判——那种 SRT 多半是人塞进去的字幕
    if any(getattr(e, "rule", "") == "path:ground" for e in (getattr(decision, "shot_evidence", []) or [])):
        return ""
    from guicang.model import Evidence

    decision.shot_type = "航拍"
    decision.shot_conf = 0.80
    decision.shot_evidence = list(getattr(decision, "shot_evidence", []) or []) + [
        Evidence("srt:telemetry", "同目录有同名 .SRT 遥测：这堆是无人机拍的", 0.80)
    ]
    g["manual"]._refresh_branch_and_status(decision)
    return "同目录有同名 .SRT 遥测，按航拍算（0.80）"


def _why_pending(cfg: Config, gcfg, out: Verdict, decision) -> str:
    missing = missing_lists(cfg)
    if not out.date:
        return "没找到日期"
    if out.date_kind == "mtime":
        return "只有上传时间，没有能信的拍摄日期"
    if out.branch == "douyin":
        return f"抖音这条的拍摄类型只有 {out.shot_conf:.2f} 的把握（可以 --shot 钉一次）"
    if missing:
        return "；".join(missing)
    if not getattr(decision, "region", None) and not getattr(decision, "activity", None):
        return "路径里没出现 places.toml 或 activities.toml 里的地名/活动名"
    pending = [n for n in (getattr(decision, "notes", []) or []) if n]
    return pending[0] if pending else "证据不够（拍摄类型或地点置信度低于阈值）"


def refresh_pending_why(cfg: Config, verdict: "Verdict", gcfg=None) -> str:
    """钉过之后重算「还是待定」的理由。

    不重算的话，人把日期钉上了，解释里还写着「只有上传时间」——
    等于让解释去骗人。
    """
    why = "" if verdict.is_auto else _why_pending(cfg, gcfg, verdict, verdict.decision)
    verdict.pending_why = why
    return why


def _split_make(cam: str | None) -> tuple[str, str]:
    """把「DJI Mavic 3」拆成 make/model，喂给归藏的机型规则。"""
    if not cam:
        return "", ""
    head, _, rest = cam.partition(" ")
    return (head, cam) if rest else (cam, cam)


def _as_when(iso: str | None, kind: str) -> datetime | None:
    if not iso:
        return None
    try:
        if kind == "year" and len(iso) == 4:
            return datetime(int(iso), 1, 1)
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _looks_video(name: str) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in {"mp4", "mov", "m4v", "avi", "mkv"}


_MEDIA_EXTS = {
    "jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp",
    "raw", "arw", "cr2", "cr3", "nef", "dng", "orf", "rw2", "raf",
    "mp4", "mov", "m4v", "avi", "mkv",
}


def _is_media(name: str) -> bool:
    """这个名字本身是不是一个媒体文件（而不是整堆的目录名）。"""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext in _MEDIA_EXTS


def _member_name(hint_names) -> str:
    """从一个目录堆的文件名里挑一个媒体文件，用来取前缀证据（IMG_/MVI_/GOPR…）。"""
    for name in hint_names or ():
        if _is_media(name):
            return name
    return ""


@dataclass
class Target:
    """落位结果：目标目录 + 该用的归藏配置（年份未知时 time_depth 会临时降级）。"""

    directory: str
    parts: list[str]


def target_for(gcfg, decision, when: datetime | None, filename: str, *, layout=None,
               camera: str = "", year_only: bool = False) -> Target:
    """让归藏算落位。只认到年份的，把时间粒度临时降到 year，不编周。"""
    g = _gc()
    cfg = replace(gcfg, time_depth="year") if year_only else gcfg
    parts = g["taxonomy"].target_parts(
        decision, when, cfg, layout=layout, camera=camera or ""
    )
    return Target(directory="/".join(parts), parts=parts)


def build_layout(gcfg, triples):
    """triples: [(decision, when, camera)]，用整库算省/市折叠与机型分组。"""
    g = _gc()
    return g["taxonomy"].build_layout(triples, gcfg)


def short_branch(decision) -> str:
    return _gc()["taxonomy"].short_branch(decision)
