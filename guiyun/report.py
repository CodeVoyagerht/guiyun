"""把方案说成人话：一棵目标树 + 一张明细表 + 几句风险提示。"""

from __future__ import annotations

import time

from .config import Config
from .plan import Item, Plan, segs


def fmt_size(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def _tree(items: list[Item], root: str) -> dict:
    tree: dict = {}
    for it in items:
        rel = it.target_dir[len(root) :].strip("/") if it.target_dir.startswith(root) else it.target_dir
        node = tree
        for part in segs(rel):
            node = node.setdefault(part, {})
        leaf = node.setdefault("\0items", [])
        leaf.append(it)
    return tree


def _render(node: dict, prefix: str = "", lines: list[str] | None = None) -> list[str]:
    lines = lines if lines is not None else []
    keys = [k for k in node if k != "\0items"]
    items = node.get("\0items", [])
    entries = [(k, node[k]) for k in sorted(keys)]
    total = len(entries) + (1 if items else 0)
    for idx, (key, child) in enumerate(entries):
        last = idx == total - 1 and not items
        lines.append(f"{prefix}{'└─ ' if last else '├─ '}{key}/")
        _render(child, prefix + ("   " if last else "│  "), lines)
    if items:
        n = len(items)
        nf = sum(1 for i in items if not i.is_dir)
        nd = n - nf
        size = sum(i.size for i in items)
        bits = []
        if nd:
            bits.append(f"{nd} 个文件夹")
        if nf:
            bits.append(f"{nf} 个文件")
        lines.append(f"{prefix}└─ ({'，'.join(bits)}，{fmt_size(size)})")
    return lines


def tree_text(plan: Plan) -> str:
    todo = [i for i in plan.items if i.status != "skip"]
    if not todo:
        return "（没有要动的东西）"
    tree = _tree(todo, plan.library_root)
    lines = [f"{plan.library_root}/"]
    return "\n".join(lines + _render(tree))


def overview(plan: Plan, cfg: Config) -> str:
    s = plan.stats()
    out = [
        f"库根：{plan.library_root}",
        f"要做：{s['auto']} 条定得下来，{s['pending']} 条进「{cfg.pending_dir}」，"
        f"另有 {s['skip']} 条不用动",
        f"体量：{s['dirs']} 个文件夹 + {s['files']} 个散文件，共 {fmt_size(s['bytes'])}",
    ]
    return "\n".join(out)


def markdown(plan: Plan, cfg: Config, detail: int = 40) -> str:
    s = plan.stats()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(plan.created or time.time()))
    lines = [
        f"# 归云 · 整理方案（{stamp}）",
        "",
        overview(plan, cfg),
        "",
        "## 搬完长这样",
        "",
        "```",
        tree_text(plan),
        "```",
        "",
        "## 明细",
        "",
    ]
    todo = [i for i in plan.items if i.status != "skip"]
    shown = todo[:detail] if detail > 0 else todo
    if shown:
        lines += ["| 源 | 目的 | 判断 | 把握 |", "| --- | --- | --- | --- |"]
        for it in shown:
            tag = "✔" if it.status == "auto" else "?"
            why = " / ".join(it.reasons[:2]) if it.reasons else (it.why or "")
            lines.append(
                f"| `{it.src}` | `{it.dst}` | {tag} {it.project or '—'} · "
                f"{it.date or '—'} · {it.camera or '—'} | {it.conf:.2f} {why} |"
            )
        if len(todo) > len(shown):
            lines.append(f"| … | 还有 {len(todo) - len(shown)} 条，`guiyun plan --all` 看全 | | |")
    else:
        lines.append("（没有要动的东西）")

    pending = plan.pending
    if pending:
        lines += [
            "",
            f"## 拿不准的 {len(pending)} 条（会进「{cfg.pending_dir}/」）",
            "",
        ]
        for it in pending[:20]:
            lines.append(f"- `{it.src}` — {it.why or '判定置信不足'}")
        if len(pending) > 20:
            lines.append(f"- …还有 {len(pending) - 20} 条")

    lines += [
        "",
        "## 动手之前",
        "",
        "- 云端只搬不删：搬错了用 `guiyun undo <run_id>` 反向搬回，不会丢东西。",
        "- 真实执行前先 `guiyun apply --dry-run` 数一遍条数，跟这张表对得上再真跑。",
        "- 索引会在这之后过期（`guiyun status` 会提醒），下一次整理前 `guiyun scan` 一次。",
    ]
    return "\n".join(lines)
