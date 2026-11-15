"""索引：云端树的本地快照，存在 SQLite 里。

为什么必须先有索引，不能边扫边算：
1. 网盘每次列目录都是一次 HTTP 往返，还可能触发风控——plan 必须零调用；
2. 「同一天有没有撞机型」「哪些是重复文件」这类判断要看全局，单条看不到；
3. 索引是真相。方案算错了，重算一次不花网盘流量。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator

from .backends.base import Entry, join

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    path    TEXT PRIMARY KEY,
    parent  TEXT NOT NULL,
    name    TEXT NOT NULL,
    is_dir  INTEGER NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,
    mtime   REAL,
    md5     TEXT,
    depth   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(parent);
CREATE INDEX IF NOT EXISTS idx_entries_dir    ON entries(is_dir);
CREATE INDEX IF NOT EXISTS idx_entries_md5    ON entries(md5);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id       TEXT PRIMARY KEY,
    started  REAL,
    finished REAL,
    ok       INTEGER,
    stats    TEXT
);

CREATE TABLE IF NOT EXISTS ops (
    run_id TEXT NOT NULL,
    seq    INTEGER NOT NULL,
    kind   TEXT NOT NULL,
    src    TEXT NOT NULL,
    dst    TEXT NOT NULL,
    ok     INTEGER,
    err    TEXT,
    PRIMARY KEY (run_id, seq)
);
"""


class Index:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ── 生命周期 ──────────────────────────────────────────────────────────
    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ── 写 ────────────────────────────────────────────────────────────────
    def clear_entries(self) -> None:
        self.db.execute("DELETE FROM entries")
        self.db.commit()

    def write_entries(self, entries: Iterable[Entry]) -> int:
        rows = [
            (
                e.path,
                e.parent,
                e.name,
                1 if e.is_dir else 0,
                e.size,
                e.mtime,
                e.md5,
                e.depth,
            )
            for e in entries
        ]
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO entries"
                " (path,parent,name,is_dir,size,mtime,md5,depth)"
                " VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def set_meta(self, key: str, value) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
            (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except (json.JSONDecodeError, TypeError):
            return row["v"]

    def start_run(self, run_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO runs (id, started, finished, ok, stats) VALUES (?,?,?,?,?)",
                (run_id, time.time(), None, None, None),
            )

    def log_op(self, run_id: str, seq: int, kind: str, src: str, dst: str, ok: bool, err: str | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO ops (run_id,seq,kind,src,dst,ok,err) VALUES (?,?,?,?,?,?,?)",
                (run_id, seq, kind, src, dst, 1 if ok else 0, err),
            )

    def finish_run(self, run_id: str, ok: bool, stats: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished=?, ok=?, stats=? WHERE id=?",
                (time.time(), 1 if ok else 0, json.dumps(stats, ensure_ascii=False), run_id),
            )

    # ── 读 ────────────────────────────────────────────────────────────────
    def count(self, where: str = "", params: tuple = ()) -> int:
        sql = "SELECT COUNT(*) c FROM entries"
        if where:
            sql += f" WHERE {where}"
        return int(self.db.execute(sql, params).fetchone()["c"])

    def total_size(self) -> int:
        row = self.db.execute(
            "SELECT COALESCE(SUM(size),0) s FROM entries WHERE is_dir = 0"
        ).fetchone()
        return int(row["s"])

    def files(self, where: str = "", params: tuple = (), order: str = "path") -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM entries WHERE is_dir = 0"
        if where:
            sql += f" AND ({where})"
        sql += f" ORDER BY {order}"
        yield from self.db.execute(sql, params)

    def dirs(self, order: str = "path") -> Iterator[sqlite3.Row]:
        yield from self.db.execute(
            f"SELECT * FROM entries WHERE is_dir = 1 ORDER BY {order}"
        )

    def children(self, parent: str) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM entries WHERE parent = ?", (parent,)))

    def get(self, path: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM entries WHERE path = ?", (path,)).fetchone()

    def has_children(self, path: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM entries WHERE parent = ? LIMIT 1", (path,)
        ).fetchone() is not None

    def has_subdirs(self, path: str) -> bool:
        """只看子目录。判断"能不能整目录搬"必须用这个——
        只有文件的文件夹照样是叶子，混用 has_children 会让整目录搬永远不触发。"""
        return self.db.execute(
            "SELECT 1 FROM entries WHERE parent = ? AND is_dir = 1 LIMIT 1", (path,)
        ).fetchone() is not None

    # ── 已有路径集合（算冲突用） ──────────────────────────────────────────
    def all_paths(self) -> set[str]:
        return {r["path"] for r in self.db.execute("SELECT path FROM entries")}

    # ── run 历史 ──────────────────────────────────────────────────────────
    def runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)
            )
        )

    def run_ops(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM ops WHERE run_id = ? AND ok = 1 ORDER BY seq DESC",
                (run_id,),
            )
        )


def walk(backend, roots: list[str], max_depth: int, max_files: int,
         skip_prefixes: list[str], progress=None,
         report: dict | None = None) -> tuple[list[Entry], bool]:
    """广度优先遍历，产出 Entry 列表。

    返回 (entries, truncated)。truncated=True 表示撞上了 max_files 保险丝——
    这不是错误，是**故意**停下来，避免一次 scan 把网盘拖到风控。

    `report` 是可选的记账本，会填两个键：
      - `errors`: [(path, 原因)]，读不了的目录。**这些目录里面的东西这次没进来**，
        以前是静默跳过的，用户会以为整棵树都索引好了。
      - `deep`: [path]，撞上 max_depth 的目录（它们本身进来了，里面的没进来）。
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(r, 0) for r in roots]
    truncated = False
    if report is not None:
        report.setdefault("errors", [])
        report.setdefault("deep", [])

    def skipped(path: str) -> bool:
        for pref in skip_prefixes:
            if not pref:
                continue
            if path == pref or path.startswith(pref.rstrip("/") + "/"):
                return True
        return False

    while queue:
        path, depth = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        if skipped(path):
            continue
        if progress:
            progress(path, len(entries))
        try:
            children = backend.list_dir(path)
        except Exception as exc:  # 单个目录读不了不该让整次扫描崩掉
            if report is not None:
                report["errors"].append((path, str(exc)))
            if progress:
                progress(f"!{path}: {exc}", len(entries))
            continue
        for child in children:
            entries.append(child)
            if len(entries) >= max_files:
                truncated = True
                return entries, truncated
            if child.is_dir and not skipped(child.path):
                if depth + 1 < max_depth:
                    queue.append((child.path, depth + 1))
                elif report is not None:
                    report["deep"].append(child.path)
    return entries, truncated
