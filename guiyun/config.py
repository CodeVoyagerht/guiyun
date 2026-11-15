"""配置：一个 TOML，一份默认值，报错必须指到文件和键。

设计取向跟 guicang 一致——**分类法不是焊死的**。项目清单、时间粒度、
机型层开关、哪些扩展名参与整理，全在配置里；改主意改配置，不重装工具。
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOME = Path.home() / ".guiyun"

MEDIA_EXT = [
    "jpg", "jpeg", "png", "heic", "tif", "tiff",
    "raw", "arw", "cr2", "cr3", "nef", "dng", "orf", "rw2", "raf",
    "mp4", "mov", "m4v", "avi", "mkv",
]
JUNK_EXT = ["srt", "lrf", "thm", "lrv", "txt", "ini", "db", "xmp", "aae"]

# 地点/活动清单跟归藏共用同一份文件（默认在归藏的 .guicang 目录里）
PLACES_FILE = "places.toml"
ACTIVITIES_FILE = "activities.toml"


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    path: Path
    backend_kind: str = "baidu"
    backend_local_root: str = ""
    backend_cli: str = "BaiduPCS-Go"
    backend_config_dir: str = ""
    library_root: str = "/摄影库"
    scan_roots: list[str] = field(default_factory=lambda: ["/"])
    scan_max_depth: int = 8
    scan_max_files: int = 200_000
    taxonomy_source: str = ""          # places.toml / activities.toml 所在目录（一般指到 .guicang）
    layout_time: str = "week"          # week | day | month | year
    layout_camera: bool = True
    match_include: list[str] = field(default_factory=lambda: list(MEDIA_EXT))
    match_exclude: list[str] = field(default_factory=lambda: list(JUNK_EXT))
    match_min_size: int = 0
    apply_throttle: float = 0.35
    apply_retry: int = 2
    on_conflict: str = "rename"       # rename | skip
    pending_dir: str = "待定"

    # ── 派生 ──────────────────────────────────────────────────────────────
    @property
    def home(self) -> Path:
        return self.path.parent

    @property
    def index_path(self) -> Path:
        return self.home / "index.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"


def home_dir(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    env = os.environ.get("GUIYUN_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_HOME


def config_path(home: Path) -> Path:
    return home / "guiyun.toml"


# ── 解析 ────────────────────────────────────────────────────────────────────
def _get(table: dict, key: str, default, kind, where: str):
    if key not in table:
        return default
    val = table[key]
    if kind is list:
        if not isinstance(val, list):
            raise ConfigError(f"{where}.{key} 应该是数组，现在是 {type(val).__name__}")
        return val
    if not isinstance(val, kind):
        raise ConfigError(f"{where}.{key} 应该是 {kind.__name__}，现在是 {type(val).__name__}")
    return val


def _toml_hint(message: str) -> str:
    """把 TOML 解析器的英文报错翻成「人话 + 怎么改」。

    最常见的一条是 Windows 路径：TOML 的双引号字符串里 ``\\`` 是转义符，
    用户直接粘 ``root = "D:\\网盘"`` 里的 ``\\U``/``\\n``/``\\t`` 会被当成转义序列，
    报 ``Invalid hex value`` 或干脆吃掉一个字符。
    """
    low = message.lower()
    if "hex value" in low or "escape" in low or "unescaped" in low:
        return (
            "\n\n提示：这多半是 Windows 路径里的反斜杠。TOML 的双引号字符串把 \\ 当转义符，"
            "`D:\\素材` 里的 \\素 会被当成转义序列。两种写法都行：\n"
            "  root = \"D:\\\\素材\"        （每个反斜杠写两遍）\n"
            "  root = 'D:\\素材'          （用单引号，里面不转义 —— 推荐，路径直接粘）"
        )
    if "duplicate" in low or "overwrite" in low or "already exists" in low:
        return "\n\n提示：同一个键（或同一个 [段.键]）写了两次。检查是不是重复了一段 [[projects.items]] 的字段。"
    if "expected newline" in low or "invalid value" in low:
        return "\n\n提示：这行值后面可能多了逗号，或者字符串的引号没配成对。"
    return ""


def load(home: Path | None = None) -> Config:
    home = home or home_dir(None)
    path = config_path(home)
    if not path.exists():
        raise ConfigError(
            f"没找到配置文件：{path}\n先跑 `guiyun init` 生成一份，再按需改。"
        )
    try:
        text = path.read_text(encoding="utf-8")
        # Windows 上记事本、PowerShell 的 > / Set-Content、一堆编辑器都会在 UTF-8
        # 前面塞一个 BOM。tomllib 不认 BOM，报的是 "Invalid statement (at line 1,
        # column 1)" —— 人看着完全莫名其妙，因为第 1 行看起来是好的。
        # 文件内容本身没写错，就替人吃掉这个 BOM，不报错。
        if text.startswith("\ufeff"):
            text = text[1:]
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"配置文件写坏了：{path}\n  {exc}" + _toml_hint(str(exc))
        ) from exc
    except OSError as exc:
        raise ConfigError(f"读不了配置文件 {path}：{exc}") from exc

    cfg = Config(path=path)

    backend = raw.get("backend", {}) or {}
    cfg.backend_kind = _get(backend, "kind", cfg.backend_kind, str, "backend")
    if cfg.backend_kind not in ("baidu", "local"):
        raise ConfigError(f"backend.kind 只能是 baidu 或 local，现在是 {cfg.backend_kind!r}")

    local = backend.get("local", {}) or {}
    cfg.backend_local_root = _get(local, "root", cfg.backend_local_root, str, "backend.local")

    baidu = backend.get("baidu", {}) or {}
    cfg.backend_cli = _get(baidu, "cli", cfg.backend_cli, str, "backend.baidu")
    cfg.backend_config_dir = _get(baidu, "config_dir", cfg.backend_config_dir, str, "backend.baidu")

    if cfg.backend_kind == "local" and not cfg.backend_local_root:
        raise ConfigError("backend.kind = \"local\" 时必须写 backend.local.root（本机目录）")

    lib = raw.get("library", {}) or {}
    cfg.library_root = _get(lib, "root", cfg.library_root, str, "library")
    cfg.pending_dir = _get(lib, "pending_dir", cfg.pending_dir, str, "library")
    if not cfg.library_root.startswith("/"):
        raise ConfigError(f"library.root 必须以 / 开头（云端绝对路径），现在是 {cfg.library_root!r}")

    scan = raw.get("scan", {}) or {}
    cfg.scan_roots = _get(scan, "roots", cfg.scan_roots, list, "scan")
    cfg.scan_max_depth = _get(scan, "max_depth", cfg.scan_max_depth, int, "scan")
    cfg.scan_max_files = _get(scan, "max_files", cfg.scan_max_files, int, "scan")
    for r in cfg.scan_roots:
        if not isinstance(r, str) or not r.startswith("/"):
            raise ConfigError(f"scan.roots 里的路径必须以 / 开头：{r!r}")

    tax = raw.get("taxonomy", {}) or {}
    cfg.taxonomy_source = _get(tax, "source", cfg.taxonomy_source, str, "taxonomy")

    layout = raw.get("layout", {}) or {}
    # 旧键 layout.date 还认，但按归藏的叫法统一成 layout.time
    key = "time" if "time" in layout else "date"
    cfg.layout_time = _get(layout, key, cfg.layout_time, str, "layout")
    if cfg.layout_time not in ("week", "day", "month", "year"):
        raise ConfigError(
            f"layout.time 只能是 week / day / month / year，现在是 {cfg.layout_time!r}"
        )
    cfg.layout_camera = _get(layout, "camera", cfg.layout_camera, bool, "layout")

    match = raw.get("match", {}) or {}
    if "include" in match:
        cfg.match_include = [str(x).lower().lstrip(".") for x in match["include"]]
    if "exclude" in match:
        cfg.match_exclude = [str(x).lower().lstrip(".") for x in match["exclude"]]
    cfg.match_min_size = _get(match, "min_size", cfg.match_min_size, int, "match")

    if raw.get("projects"):
        # 老配置：项目清单已经并进 places.toml，这里只提醒，不报错中断
        sys.stderr.write(
            "提示：[projects] 已经不用了，地点清单改到 places.toml / activities.toml，"
            "跟归藏共用一份（配置里写 [taxonomy] source = \"<素材库>\\.guicang\"）。\n"
        )

    apply_tbl = raw.get("apply", {}) or {}
    cfg.apply_throttle = float(_get(apply_tbl, "throttle", cfg.apply_throttle, (int, float), "apply"))
    cfg.apply_retry = _get(apply_tbl, "retry", cfg.apply_retry, int, "apply")
    cfg.on_conflict = _get(apply_tbl, "on_conflict", cfg.on_conflict, str, "apply")
    if cfg.on_conflict not in ("rename", "skip"):
        raise ConfigError(f"apply.on_conflict 只能是 rename 或 skip，现在是 {cfg.on_conflict!r}")

    return cfg


DEFAULT_TOML = """\
# 归云 guiyun —— 网盘素材整理配置
# 改完不用重装，下次命令直接生效。

[backend]
kind = "baidu"                     # baidu | local

[backend.baidu]
cli = "BaiduPCS-Go"                # 不在 PATH 就写绝对路径
config_dir = ""                    # 留空用 BaiduPCS-Go 默认的 ~/.config/BaiduPCS-Go

[backend.local]
root = ""                          # kind = "local" 时必填，例：E:\\\\网盘镜像

[library]
root = "/摄影库"                    # 整理产物落在这里（云端绝对路径）
pending_dir = "待定"                # 判不准的进这里，不瞎猜

[scan]
roots = ["/"]                      # 从哪些目录开始扫；嫌慢就只扫几个乱的目录
max_depth = 8
max_files = 200000                 # 保险丝：超过就停，别把整个网盘拖下来

[taxonomy]
source = ""                        # places.toml / activities.toml 在哪个目录。一般指到本机素材库的
                                   # .guicang 目录，例：source = 'D:\素材\.guicang'
                                   # 留空就用 ~/.guiyun 下自己那份

[layout]
time = "week"                      # week | day | month | year —— 一次拍摄落哪一层
                                   # week = 年/周六→周五的周期，跟归藏一模一样
camera = true                      # 机型层：只在同一天同类型真的撞了机型时才出现

[match]
include = ["jpg","jpeg","png","heic","tif","tiff",
           "raw","arw","cr2","cr3","nef","dng","orf","rw2","raf",
           "mp4","mov","m4v","avi","mkv"]
exclude = ["srt","lrf","thm","lrv","txt","ini","db","xmp","aae"]
min_size = 0                       # 小于这个字节数的当缩略图，不参与整理

[apply]
throttle = 0.35                    # 每次网盘调用之间的间隔（秒），别调太小
retry = 2
on_conflict = "rename"             # rename | skip

# 地点和活动不在这里配：跟归藏共用 places.toml / activities.toml，
# 一份清单两边都认，才不会出现「本地叫泡泡塘、网盘叫郑州泡泡塘」。
"""


def write_default(home: Path, force: bool = False) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = config_path(home)
    if path.exists() and not force:
        raise ConfigError(f"配置已存在：{path}\n要覆盖加 --force。")
    path.write_text(DEFAULT_TOML, encoding="utf-8")
    (home / "runs").mkdir(exist_ok=True)
    return path
