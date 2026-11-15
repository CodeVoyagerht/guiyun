"""出方案：把索引变成一张「谁搬到哪」的清单。

这一步**零网盘调用**，纯本地算。理由很实在：方案算错了要重算，重算不该再花
一次流量、多一次被风控看见的机会。

两层单位：
- **整目录搬**：一个没有子目录的文件夹，里面都是这一次拍摄的东西（RAW+JPG+
  SRT 混着），整堆搬走。API 调用少一到两个数量级，而且保住了内部的相对结构。
- **单文件搬**：散落在有子目录的文件夹里的媒体文件，只能一条一条走。

落位形状跟本地（归藏）**逐字相同**：区域/省?/市?/地点/园区区域?/年/周期/拍摄类型/机型?
——省和市到底出不出现，要等所有条都判完才知道（只有两个以上省才写省），
所以这里是两遍：先判完，再用整库算版式，最后才拼路径。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from . import manual as manual_mod
from .backends.base import join, split_ext
from .config import Config
from .decide import build_layout, decide, refresh_pending_why, target_for, taxonomy_config
from .index import Index


def segs(path: str) -> list[str]:
    return [s for s in (path or "").split("/") if s]


def under(path: str, root: str) -> bool:
    root = (root or "/").rstrip("/")
    if not root:
        return True
    return path == root or path.startswith(root + "/")


@dataclass
class Item:
    """一条搬迁计划：搬什么、搬到哪、凭什么。"""

    src: str
    name: str
    is_dir: bool
    size: int
    n_files: int
    branch: str = "region"            # region | activity | douyin
    place: str | None = None
    area: str = ""
    activity: str | None = None
    edition: str | None = None
    shot_type: str = ""
    date: str | None = None
    date_seg: str = ""                # 展示用：2024/2024-05-18_05-24
    camera: str | None = None
    conf: float = 0.0
    reasons: list[str] = field(default_factory=list)
    dst: str = ""
    target_dir: str = ""
    status: str = "auto"  # auto | pending | skip
    why: str = ""

    # 旧字段名的别名，cli/report 里还有地方在用
    @property
    def project(self) -> str | None:
        return self.place or self.activity

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Item":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class Plan:
    created: float
    library_root: str
    items: list[Item]

    @property
    def auto(self) -> list[Item]:
        return [i for i in self.items if i.status == "auto"]

    @property
    def pending(self) -> list[Item]:
        return [i for i in self.items if i.status == "pending"]

    @property
    def skipped(self) -> list[Item]:
        return [i for i in self.items if i.status == "skip"]

    def stats(self) -> dict:
        todo = [i for i in self.items if i.status != "skip"]
        return {
            "auto": len(self.auto),
            "pending": len(self.pending),
            "skip": len(self.skipped),
            "dirs": sum(1 for i in todo if i.is_dir),
            "files": sum(1 for i in todo if not i.is_dir),
            "bytes": sum(i.size for i in todo),
        }

    def to_dict(self) -> dict:
        return {
            "created": self.created,
            "library_root": self.library_root,
            "stats": self.stats(),
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Plan":
        return cls(
            created=float(raw.get("created") or 0),
            library_root=raw.get("library_root") or "/",
            items=[Item.from_dict(x) for x in raw.get("items", [])],
        )


# ── 判定 ────────────────────────────────────────────────────────────────────
def _media(cfg: Config, name: str, size: int) -> bool:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in {e.lower() for e in cfg.match_exclude}:
        return False
    if ext not in {e.lower() for e in cfg.match_include}:
        return False
    return size >= cfg.match_min_size


def _uniq(dst: str, used: dict[str, int], *, is_dir: bool = False) -> str:
    """同一个目标路径被两条计划撞上时，第二条开始加 (2)(3)…

    `used` 里同时装着**云端已经有的路径**和本次计划占下的路径，所以
    「目标已存在」这一档不靠调用方另外判——直接在这儿改名就行。
    """
    cur = used.get(dst, 0)
    if cur == 0:
        used[dst] = 1
        return dst

    parent, _, name = dst.rpartition("/")
    n = cur + 1
    while True:
        # 目录名里的点不是扩展名（「2024.05」不能被改成「2024 (2).05」）
        if is_dir or "." not in name:
            cand = f"{name} ({n})"
        else:
            stem, ext = split_ext(name)
            cand = f"{stem} ({n}).{ext}"
        new = f"{parent}/{cand}" if parent else f"/{cand}"
        if used.get(new, 0) == 0:
            break
        n += 1
    used[dst] = n
    used[new] = 1
    return new


def build(cfg: Config, idx: Index) -> Plan:
    lib = (cfg.library_root or "/摄影库").rstrip("/") or "/"
    existing = idx.all_paths()
    gcfg = taxonomy_config(cfg)

    # ── 先把"整目录搬"的候选找出来：没有子目录、且里面有媒体文件的文件夹 ──
    leaf: dict[str, dict] = {}
    for d in idx.dirs():
        p = d["path"]
        if under(p, lib) or p == lib:
            continue
        if idx.has_subdirs(p):
            continue
        kids = [c for c in idx.children(p) if not c["is_dir"] and _media(cfg, c["name"], c["size"] or 0)]
        if not kids:
            continue
        leaf[p] = {
            "n_files": len(kids),
            "size": sum((c["size"] or 0) for c in kids),
            # 线索要连旁车一起看：同名 .SRT 遥测能证明这堆是无人机拍的，
            # 但 .SRT 本身不是媒体文件，不能只拿 media 名单当线索
            "names": [c["name"] for c in idx.children(p) if not c["is_dir"]][:12],
        }

    raw_units: list[tuple[dict, bool, int, int, list[str]]] = []
    for p, meta in leaf.items():
        row = idx.get(p)
        if row is None:
            continue
        raw_units.append((row, True, meta["n_files"], meta["size"], meta["names"]))
    for f in idx.files():
        p = f["path"]
        if under(p, lib):
            continue
        if f["parent"] in leaf:
            continue  # 跟着所属目录一起走，不单独出现
        if not _media(cfg, f["name"], f["size"] or 0):
            continue
        raw_units.append((f, False, 1, f["size"] or 0, []))

    # ── 第一遍：判完 ────────────────────────────────────────────────────────
    overrides = manual_mod.load(cfg)
    items: list[Item] = []
    verdicts: dict[str, object] = {}
    for row, is_dir, n_files, size, hint_names in raw_units:
        path, name = row["path"], row["name"]
        # 整目录搬时，目录名本身就是最强的线索（"2024-05-18 泡泡塘"），
        # 所以要把它当成最近的一段喂给判定；单文件则靠父目录链。
        if is_dir:
            segments, filename = segs(path), ""
        else:
            segments, filename = segs(path)[:-1], name
        verdict = decide(cfg, segments, filename, row["mtime"], hint_names=hint_names, gcfg=gcfg)
        verdict = manual_mod.apply(verdict, manual_mod.for_src(overrides, path), verdict.when, gcfg)
        # 钉过之后理由得重算，不然解释里还留着「只有上传时间」
        refresh_pending_why(cfg, verdict, gcfg)
        verdicts[path] = verdict
        status = "auto" if verdict.is_auto else "pending"
        items.append(
            Item(
                src=path,
                name=name,
                is_dir=is_dir,
                size=size,
                n_files=n_files,
                branch=verdict.branch,
                place=verdict.place,
                area=verdict.area,
                activity=verdict.activity,
                edition=getattr(verdict.decision, "edition", None) if verdict.decision else None,
                shot_type=verdict.shot_type,
                date=verdict.date,
                camera=verdict.camera,
                conf=verdict.project_conf,
                reasons=verdict.reasons,
                status=status,
                why=verdict.pending_why,
            )
        )

    # ── 第二遍：用整库算版式（省市折叠、机型分组），再拼路径 ────────────────
    layout = build_layout(
        gcfg,
        [
            (verdicts[it.src].decision, verdicts[it.src].when, it.camera or "")
            for it in items
            if it.status != "pending" and verdicts[it.src].decision is not None
        ],
    )

    # `used` 先用云端已有的路径播种：不加这一步，on_conflict="rename" 就是空转——
    # `_uniq` 只在「本次计划内部撞名」时才改名，撞上云端已有的同名目标会原样
    # 返回，然后被后端以「目标已存在」拒掉，方案说能搬、执行逐条失败。
    used: dict[str, int] = {p: 1 for p in existing}
    for it in items:
        verdict = verdicts[it.src]
        decision, when = verdict.decision, verdict.when
        if decision is None:
            it.status = "pending"
            it.why = it.why or "没有判定结果"
            it.dst = it.src
            it.target_dir = it.src.rsplit("/", 1)[0]
            continue
        year_only = bool(it.date and len(it.date) == 4)
        # 靠上传时间推出来的日期不能进路径——待定就老老实实写「未知时间」
        path_when = None if verdict.date_kind == "mtime" else when
        # 待定也走同一套：归藏自己知道该进「待定」还是留在抖音枝里。
        # 但两边的「待定」不是一个口径：归云多一条「日期只来自上传时间」。
        # 落位规则：活动 / 抖音 这两枝本身就是要放的地方（届次/抖音才是索引键），
        # 没有日期也留在枝里写「未知时间」；只有「区域」枝缺日期才进待定桶。
        judge = decision
        if it.status == "pending" and getattr(decision, "branch", "region") == "region":
            judge = replace(decision, status="needs_review")
        target = target_for(
            gcfg,
            judge,
            path_when,
            "",
            layout=layout if it.status != "pending" else None,
            camera=it.camera or "",
            year_only=year_only,
        )
        target_dir = join(lib, *target.parts)
        it.target_dir = target_dir
        # 展示用：只看时间那几层。地点/活动/拍摄类型/机型各是一个字段，
        # 混进「时间」里会打出「时间 2024/2024-05-18_05-24/地拍」这种东西。
        not_time = {it.place or "", it.activity or "", it.shot_type or "", it.camera or "", it.edition or ""}
        it.date_seg = "/".join(p for p in target.parts[1:] if p not in not_time)
        dst = join(target_dir, it.name)

        # 已经在正确位置了就别动
        src_parent = it.src.rsplit("/", 1)[0] or "/"
        if src_parent == target_dir:
            it.status = "skip"
            it.why = "已经在正确位置，不用动"
            it.dst = it.src
            continue

        if dst in existing and dst != it.src and cfg.on_conflict == "skip":
            it.status = "skip"
            it.why = f"目标已存在同名项：{dst}"
            it.dst = dst
            continue

        renamed = _uniq(dst, used, is_dir=it.is_dir)
        it.dst = renamed
        if renamed != dst:
            # 撞名了（云端已有，或本次计划里前一条先占了）。改名这件事得说出来，
            # 否则用户看到一个带 (2) 的目录名会以为是自己起的。
            it.why = f"目标已存在同名项，改名落「{renamed.rsplit('/', 1)[-1]}」"

    # 根目录自身、待定目录本身不参与
    items = [i for i in items if i.src != lib]

    return Plan(created=time.time(), library_root=lib, items=sorted(items, key=lambda i: i.src))


# ── 存取 ────────────────────────────────────────────────────────────────────
def plan_path(cfg: Config) -> Path:
    return cfg.home / "plan.json"


def save(cfg: Config, plan: Plan) -> Path:
    path = plan_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load(cfg: Config) -> Plan | None:
    path = plan_path(cfg)
    if not path.exists():
        return None
    try:
        return Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        raise ValueError(f"方案文件读不了：{path}（{exc}）。重新跑一次 `guiyun plan`。") from exc
