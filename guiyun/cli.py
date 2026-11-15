"""命令行。所有入口都在这里，别的地方不放 main。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, apply as apply_mod, decide as decide_mod, manual as manual_mod, plan as plan_mod, report as report_mod
from .backends import AuthError, BackendError, RateLimitError, open_backend
from .config import ConfigError, home_dir, load, write_default
from .index import Index, walk

EXIT_OK, EXIT_ERR, EXIT_NEED_YES = 0, 1, 2


def say(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = EXIT_ERR) -> int:
    print(f"✗ {msg}", file=sys.stderr, flush=True)
    return code


def _home(args) -> Path:
    return home_dir(getattr(args, "home", None))


def _load_cfg(args):
    try:
        return load(_home(args))
    except ConfigError as exc:
        raise SystemExit(str(exc))


def _ensure(backend) -> int | None:
    """动手前先确认后端是通的。返回 None 表示可以继续，否则是退出码。"""
    try:
        res = backend.check()
    except AuthError as exc:
        return die(f"登录有问题，先 guiyun login：{exc}")
    except BackendError as exc:
        return die(f"后端连不上：{exc}")
    if isinstance(res, dict) and res.get("ok") is False:
        return die(f"后端检查没过：{res.get('detail')}")
    if isinstance(res, dict) and res.get("detail"):
        say(f"· {res['detail']}")
    return None


# ── 子命令 ──────────────────────────────────────────────────────────────────
def cmd_init(args) -> int:
    home = _home(args)
    path = write_default(home, force=args.force)
    say(f"✓ 配置写在 {path}")
    say("")
    say("下一步：")
    say("  1. 把 places.toml / activities.toml 放到 [taxonomy] source 指的那份目录里")
    say("     —— 跟本地归藏用的是同一份清单：认地点、认园区区域、认活动全靠它。")
    say("     还没写过就先用归藏那边生成：guicang init --root <素材根>，再把它 .guicang 里两份清单拷过来。")
    say("  2. guiyun login   （百度网盘登录）")
    say("  3. guiyun scan    （把云端目录树抓下来，只看不搬）")
    say("  4. guiyun plan    （出方案，还是只看不搬）")
    say("  5. guiyun apply --dry-run  →  guiyun apply --yes")
    return EXIT_OK


def cmd_scan(args) -> int:
    cfg = _load_cfg(args)
    roots = args.root or cfg.scan_roots
    idx = Index(cfg.index_path)
    backend = open_backend(cfg)
    bad = _ensure(backend)
    if bad is not None:
        backend.close()
        return bad

    t0 = time.time()
    last = [0.0]

    def progress(path: str, done: int) -> None:
        now = time.time()
        if now - last[0] > 0.8:
            last[0] = now
            print(f"\r  扫了 {done} 项 … {path[-60:]:<60}", end="", flush=True)

    say(f"开始扫：{', '.join(roots)}（跳过 {cfg.library_root}）")
    try:
        entries, truncated = walk(
            backend,
            roots,
            cfg.scan_max_depth,
            cfg.scan_max_files,
            [cfg.library_root],
            progress,
        )
    finally:
        backend.close()
    say("")
    idx.clear_entries()
    n = idx.write_entries(entries)
    idx.set_meta("scanned_at", int(time.time()))
    idx.set_meta("truncated", 1 if truncated else 0)
    idx.set_meta("roots", json.dumps(roots, ensure_ascii=False))
    idx.set_meta("stale", 0)

    dirs = sum(1 for e in entries if e.is_dir)
    files = n - dirs
    size = sum(e.size or 0 for e in entries)
    say(f"✓ 索引好了：{dirs} 个文件夹 + {files} 个文件，共 {report_mod.fmt_size(size)}，耗时 {time.time() - t0:.1f}s")
    if truncated:
        say(f"! 撞上 {cfg.scan_max_files} 条保险丝，只扫了一部分。")
        say("  不是错，是故意停的（避免一次扫太多被风控）。可以把 scan_roots 收窄到具体几个目录再扫。")
    return EXIT_OK


def cmd_plan(args) -> int:
    cfg = _load_cfg(args)
    idx = Index(cfg.index_path)
    if idx.count() == 0:
        return die("索引是空的，先 guiyun scan")
    if idx.get_meta("stale"):
        say("! 上次整理之后索引没重扫过，方案可能跟云端对不上。建议先 guiyun scan。")

    plan = plan_mod.build(cfg, idx)
    path = plan_mod.save(cfg, plan)

    # --json 是给脚本用的：只吐 JSON，一个字节的人类文案都不掺。
    # （混在一起的话，用 `guiyun plan --json | jq` 就得先剥掉前面几行，
    #  这种"差不多能解析"的接口最后一定会坑人。）
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK

    say(report_mod.overview(plan, cfg))
    say("")
    say(report_mod.tree_text(plan))
    say("")
    say(f"方案存在 {path}")

    if args.md:
        out = Path(args.md)
        out.write_text(report_mod.markdown(plan, cfg, detail=args.detail), encoding="utf-8")
        say(f"报告写到 {out}")
    if args.all:
        say("")
        say(report_mod.markdown(plan, cfg, detail=0))
    return EXIT_OK


def cmd_apply(args) -> int:
    cfg = _load_cfg(args)
    idx = Index(cfg.index_path)
    plan = plan_mod.load(cfg)
    if plan is None:
        plan = plan_mod.build(cfg, idx)
        plan_mod.save(cfg, plan)

    todo = [i for i in plan.items if i.status != "skip"]
    if not todo:
        say("没有要动的东西。")
        return EXIT_OK
    if args.limit > 0:
        todo = todo[: args.limit]

    if args.dry_run:
        for it in todo[:10]:
            say(f"  {it.src}  →  {it.dst}")
        if len(todo) > 10:
            say(f"  …还有 {len(todo) - 10} 条")
        say(f"\n空跑：要搬 {len(todo)} 条，实际一条没动。")
        return EXIT_OK

    if not args.yes:
        say(f"要搬 {len(todo)} 条。")
        if not sys.stdin.isatty():
            return die(
                "真搬要显式加 --yes。先跑 `guiyun plan` 看一眼方案再决定，"
                "或用 `guiyun apply --dry-run` 数条数。",
                EXIT_NEED_YES,
            )
        say(f"搬错了可以用 guiyun undo 反向搬回。确认开始？输入 yes 继续：")
        if input().strip().lower() not in {"yes", "y", "好", "确认"}:
            return die("没开始。")

    backend = open_backend(cfg)
    bad = _ensure(backend)
    if bad is not None:
        backend.close()
        return bad

    def progress(i: int, total: int, item) -> None:
        print(f"\r  [{i}/{total}] {item.src[-56:]:<56}", end="", flush=True)

    try:
        out = apply_mod.apply(cfg, plan, backend, idx, limit=args.limit, on_progress=progress)
    finally:
        backend.close()
    say("")
    say(out.line())
    for src, err in out.errors[:10]:
        say(f"  ✗ {src}: {err}")
    if len(out.errors) > 10:
        say(f"  …还有 {len(out.errors) - 10} 条错误")
    say("")
    say(f"这次记账的 run_id：{out.run_id}（要反悔就 guiyun undo {out.run_id}）")
    return EXIT_OK if out.ok else EXIT_ERR


def cmd_undo(args) -> int:
    cfg = _load_cfg(args)
    idx = Index(cfg.index_path)
    backend = open_backend(cfg)
    try:
        out = apply_mod.undo(cfg, idx, backend, args.run_id, dry_run=args.dry_run)
    finally:
        backend.close()
    say(out.line())
    for src, err in out.errors[:10]:
        say(f"  ✗ {src}: {err}")
    return EXIT_OK if out.ok else EXIT_ERR


def cmd_status(args) -> int:
    cfg = _load_cfg(args)
    say(f"配置文件：{cfg.path}")
    say(f"后端：{cfg.backend_kind}" + (f"（{cfg.backend_cli}）" if cfg.backend_kind == "baidu" else f"（{cfg.backend_local_root}）"))
    say(f"库根：{cfg.library_root}    待定区：{cfg.pending_dir}")
    say(f"时间粒度：{cfg.layout_time}" + ("，机型分层开" if cfg.layout_camera else "，机型分层关"))
    try:
        gcfg = decide_mod.taxonomy_config(cfg)
        say(
            f"清单：{len(gcfg.places)} 个地点、{len(gcfg.activities)} 个活动"
            f"（{gcfg.source or '默认空清单'}）"
        )
        for miss in decide_mod.missing_lists(cfg):
            say(f"! 缺 {miss}——认不出来的一律进「待定」，先补上")
    except ConfigError as exc:
        say(f"! {exc}")

    if cfg.backend_kind == "baidu":
        # 「现在能不能真的读写网盘」是第一件要知道的事，摆在索引前面。
        # 查不通也不拦着——status 本来就该在离线时也能看。
        try:
            res = open_backend(cfg).check()
            if res.get("ok"):
                say(f"登录：✓ {res.get('detail') or ''}".rstrip())
            else:
                say(f"登录：✗ {res.get('detail') or '未知'}")
                say("      → 跑 guiyun login 登录")
        except BackendError as exc:
            say(f"登录：✗ {str(exc).splitlines()[0]}")

    idx = Index(cfg.index_path)
    n = idx.count()
    if n == 0:
        say("索引：还没扫过（guiyun scan）")
        return EXIT_OK
    at = idx.get_meta("scanned_at")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(at))) if at else "未知"
    say(f"索引：{n} 项，扫于 {when}，共 {report_mod.fmt_size(idx.total_size())}")
    if idx.get_meta("truncated"):
        say("! 上次扫描被保险丝截断了，索引不完整")
    if idx.get_meta("stale"):
        say("! 索引已过期（上次整理之后没重扫），下次整理前先 guiyun scan")

    plan = plan_mod.load(cfg)
    if plan:
        s = plan.stats()
        say(f"上次方案：{s['auto']} 条可搬 + {s['pending']} 条待定，{time.strftime('%m-%d %H:%M', time.localtime(plan.created))}")

    runs = idx.runs(5)
    if runs:
        say("")
        say("最近的整理：")
        for r in runs:
            try:
                st = json.loads(r["stats"] or "{}")
            except json.JSONDecodeError:
                st = {}
            state = "✓" if r["ok"] else "✗"
            say(
                f"  {state} {r['id']}  搬 {st.get('moved', 0)} 失败 {st.get('failed', 0)}"
                + (f"  中止：{st.get('aborted')}" if st.get("aborted") else "")
            )
    return EXIT_OK


def cmd_report(args) -> int:
    cfg = _load_cfg(args)
    plan = plan_mod.load(cfg)
    if plan is None:
        idx = Index(cfg.index_path)
        if idx.count() == 0:
            return die("既没有方案也没索引，先 guiyun scan")
        plan = plan_mod.build(cfg, idx)
        plan_mod.save(cfg, plan)
    text = report_mod.markdown(plan, cfg, detail=args.detail)
    if args.out:
        out = Path(args.out)
        out.write_text(text, encoding="utf-8")
        say(f"✓ 写到 {out}")
    else:
        say(text)
    return EXIT_OK


def cmd_login(args) -> int:
    cfg = _load_cfg(args)
    if cfg.backend_kind != "baidu":
        return die("只有百度网盘后端要登录；local 后端不用。")
    from .backends.baidu import BaiduBackend

    be = BaiduBackend(cfg.backend_cli, config_dir=cfg.backend_config_dir or None)
    try:
        res = be.check()
        if isinstance(res, dict) and res.get("ok"):
            say("✓ 已经登录着了。")
            say(str(res.get("detail") or ""))
            return EXIT_OK
    except BackendError:
        pass

    bduss = args.bduss
    if not bduss:
        import getpass

        say("要 BDUSS（从浏览器 cookie 里拿，这串等同于你的登录态，别外发）：")
        try:
            bduss = getpass.getpass("BDUSS: ").strip()
        except (EOFError, KeyboardInterrupt):
            # 非交互场景（脚本、管道里跑）读不到输入，别甩 traceback 给人看
            return die(
                "没读到输入。要么加 --bduss=...，要么先在终端里手动跑一次\n"
                f"  \"{cfg.backend_cli}\" login -bduss=...\n"
                "让它自己把登录态存下来，之后 guiyun 就不用再问。"
            )
    if not bduss:
        return die("没拿到 BDUSS。")
    out = be._run(["login", f"-bduss={bduss}"])
    say(out.strip() or "（没有输出）")
    try:
        be.check()
        say("✓ 登录成功，`guiyun scan` 可以开始了。")
        return EXIT_OK
    except BackendError as exc:
        return die(f"登录后还是不通：{exc}")


def cmd_config(args) -> int:
    cfg = _load_cfg(args)
    say(cfg.path.read_text(encoding="utf-8") if cfg.path.exists() else "（还没有配置文件）")
    return EXIT_OK


# ── 待定 / 人工钉住 ─────────────────────────────────────────────────────────
def _plan_or_build(cfg, idx):
    """有存好的方案就用它，没有就从索引重算一份（都是本地活，不碰网盘）。"""
    plan = plan_mod.load(cfg)
    if plan is not None:
        return plan
    if idx.count() == 0:
        return None
    plan = plan_mod.build(cfg, idx)
    plan_mod.save(cfg, plan)
    return plan


def _find_item(plan, path: str):
    """按路径找计划里的那一条。找到的是「搬的单位」——一个整堆目录，或一个散文件。"""
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    exact = [i for i in plan.items if i.src == path]
    if exact:
        return exact[0]
    pref = path.rstrip("/") + "/"
    inner = [i for i in plan.items if i.src.startswith(pref)]
    return min(inner, key=lambda i: len(i.src)) if inner else None


def _where(it) -> str:
    """这一条会落到哪一枝——分支名 + 地点/活动，给 explain/review 用。"""
    if it.branch == "douyin":
        return "抖音"
    if it.branch == "activity":
        return f"活动 {it.activity}" + (f" {it.edition}" if it.edition else "")
    chain = [p for p in (it.place, it.area) if p]
    return "地点 " + " / ".join(chain) if chain else "地点 没认出来"


def _describe(it) -> str:
    bits = []
    bits.append(f"{_where(it)}（{it.conf:.2f}）" if it.place or it.activity else f"{_where(it)}")
    bits.append(f"拍摄 {it.shot_type}" if it.shot_type else "拍摄 没认出来")
    if it.date_seg:
        bits.append(f"时间 {it.date_seg}")
    elif it.date:
        # 有日期但落不了层，只能是靠文件修改时间推的——那种不敢用
        bits.append(f"时间 不可信（只有修改时间 {it.date}）")
    else:
        bits.append("时间 没认出来")
    bits.append(f"机型 {it.camera}" if it.camera else "机型 没认出来")
    return " · ".join(bits)


def cmd_review(args) -> int:
    cfg = _load_cfg(args)
    idx = Index(cfg.index_path)

    if args.clear:
        ok = manual_mod.drop(cfg, args.clear)
        say(f"✓ 撤掉了人工判定：{args.clear}" if ok else f"那条本来就没有人工判定：{args.clear}")
        if ok and idx.count():
            plan_mod.save(cfg, plan_mod.build(cfg, idx))
            say("方案已按机器判定重算。")
        return EXIT_OK

    if args.set:
        try:
            entry = manual_mod.put(
                cfg,
                args.set,
                place=args.place,
                area=args.area,
                activity=args.activity,
                branch=args.branch,
                shot=args.shot,
                date=args.date,
                camera=args.camera,
            )
        except ValueError as exc:
            return die(str(exc))
        say(f"✓ 钉住了 {args.set}")
        for k, label in (
            ("place", "地点"),
            ("area", "园区区域"),
            ("activity", "活动"),
            ("branch", "分支"),
            ("shot", "拍摄类型"),
            ("date", "日期"),
            ("camera", "机型"),
        ):
            if entry.get(k):
                say(f"  {label} = {entry[k]}" + ("（只认到年）" if k == "date" and entry.get("date_kind") == "year" else ""))
        if idx.count() == 0:
            say("（索引还是空的，先 guiyun scan 才能看到它落在哪）")
            return EXIT_OK
        plan = plan_mod.build(cfg, idx)
        plan_mod.save(cfg, plan)
        it = _find_item(plan, args.set)
        if it is None:
            say("! 这轮方案里没找到这条——路径可能写错了，或者它已经在库里、不参与搬迁。")
            return EXIT_OK
        say("")
        say(f"  {it.src}")
        say(f"  {_describe(it)}")
        say(f"  → {'可自动搬' if it.status == 'auto' else '还是待定'}：{it.target_dir}")
        if it.status != "auto" and it.why:
            say(f"  原因：{it.why}")
        return EXIT_OK

    plan = _plan_or_build(cfg, idx)
    if plan is None:
        return die("既没有方案也没索引，先 guiyun scan")
    pend = plan.pending
    if not pend:
        say("✓ 没有待定的，都能自己定下来。")
        return EXIT_OK
    say(f"待定 {len(pend)} 条（这些不会自己搬，等你一句话）：")
    for n, it in enumerate(pend, 1):
        kind = f"文件夹，{it.n_files} 个文件" if it.is_dir else "单个文件"
        say("")
        say(f"  {n}. {it.src}")
        say(f"     {kind}；{_describe(it)}")
        if it.why:
            say(f"     为什么钉不住：{it.why}")
        hint = ""
        if it.branch == "region" and not it.place:
            hint += " --place 地点名"
        if it.branch == "activity" and not it.activity:
            hint += " --activity 活动名"
        if not it.date:
            hint += " --date 2024-05-18"
        say(f'     钉住：guiyun review --set "{it.src}"{hint}')
    say("")
    say("只钉你知道的那几样就行，剩下的机器照样补；钉完 review 还列在这里说明还缺东西。")
    say("园区区域、拍摄类型也能钉：--area 水世界 / --shot 航拍 / --branch 抖音")
    return EXIT_OK


def cmd_clean(args) -> int:
    """清空壳：整理完源目录里会剩下一堆空文件夹。

    只删**真的一件东西都不剩**的目录，而且优先删最深的——父目录要等子目录都没了
    才算空。根目录本身（scan_roots 里那几个）永远不动。
    """
    cfg = _load_cfg(args)
    roots = args.root or cfg.scan_roots
    backend = open_backend(cfg)
    bad = _ensure(backend)
    if bad is not None:
        backend.close()
        return bad

    kids: dict[str, int] = {}      # 目录 → 当前还剩几个子项（文件 + 子目录）
    depth: dict[str, int] = {}
    parent: dict[str, str] = {}
    # 默认不碰库根（那是整理的结果，不是待清理的现场）；但用户显式把库根
    # 当 --root 指过来时，就别拦着了——undo 之后库里会剩一堆空壳。
    lib = cfg.library_root.rstrip("/") or "/"
    skip_lib = not any(r == lib or r.startswith(lib + "/") for r in roots)
    queue = [(r, 0, "/") for r in roots]
    try:
        while queue:
            path, d, up = queue.pop(0)
            if d > cfg.scan_max_depth:
                continue
            if skip_lib and (path == lib or path.startswith(lib + "/")):
                continue
            try:
                entries = backend.list_dir(path)
            except AuthError as exc:
                return die(f"登录有问题：{exc}")
            except RateLimitError as exc:
                return die(f"被风控挡了，先停手：{exc}")
            except BackendError as exc:
                say(f"! 列不动 {path}：{exc}")
                continue
            kids[path] = len(entries)
            depth[path] = d
            parent[path] = up
            for e in entries:
                if e.is_dir:
                    queue.append((e.path, d + 1, path))
    finally:
        backend.close()

    # 由深到浅：子目录都删掉之后，父目录可能也变空了
    live = dict(kids)
    doomed: list[str] = []
    for path in sorted(kids, key=lambda p: depth[p], reverse=True):
        if path in roots or live.get(path, 0) != 0:
            continue
        doomed.append(path)
        up = parent.get(path)
        if up in live:
            live[up] -= 1

    if not doomed:
        say("✓ 没有空文件夹要清。")
        return EXIT_OK
    total = len(doomed)
    say(f"找到 {total} 个空文件夹（只删空的，里面有东西的一个都不碰）：")
    for p in doomed[:15]:
        say(f"  {p}")
    if len(doomed) > 15:
        say(f"  …还有 {len(doomed) - 15} 个")

    if args.dry_run or not args.yes:
        say("")
        say(f"空跑：要删 {total} 个空目录，实际一个没动。真删：guiyun clean --yes")
        return EXIT_OK if args.dry_run else EXIT_NEED_YES

    backend = open_backend(cfg)
    bad = _ensure(backend)
    if bad is not None:
        backend.close()
        return bad
    done = failed = 0
    try:
        for n, p in enumerate(doomed, 1):
            print(f"\r  [{n}/{total}] {p[-56:]:<56}", end="", flush=True)
            try:
                backend.delete(p, expect_empty=True)
                done += 1
            except RateLimitError as exc:
                say("")
                say(f"! 被风控挡了，停在这里（已删 {done} 个）：{exc}")
                break
            except BackendError as exc:
                failed += 1
                if failed <= 5:
                    say("")
                    say(f"  ✗ {p}: {exc}")
            time.sleep(cfg.apply_throttle)
    finally:
        backend.close()
    say("")
    say(f"✓ 删了 {done} 个空文件夹" + (f"，{failed} 个没删掉" if failed else ""))
    say("（百度网盘删的东西进回收站，后悔可以去网页版捞回来）")
    return EXIT_OK if not failed else EXIT_ERR


def cmd_explain(args) -> int:
    cfg = _load_cfg(args)
    idx = Index(cfg.index_path)
    plan = _plan_or_build(cfg, idx)
    if plan is None:
        return die("既没有方案也没索引，先 guiyun scan")
    it = _find_item(plan, args.path)
    if it is None:
        return die(f"方案里没有这一条：{args.path}（它可能在库里，也可能没被当成要搬的东西）")
    say(f"{it.src}")
    say(f"  {'文件夹' if it.is_dir else '单个文件'}"
        + (f"，{it.n_files} 个文件，{report_mod.fmt_size(it.size)}" if it.is_dir else f"，{report_mod.fmt_size(it.size)}"))
    say(f"  结论：{'可自动搬' if it.status == 'auto' else '待定' if it.status == 'pending' else '不动'}")
    say(f"  {_describe(it)}")
    if it.reasons:
        say("  凭什么：")
        for r in it.reasons:
            say(f"    · {r}")
    if it.status == "pending" and it.why:
        say(f"  为什么不动：{it.why}")
        say(f'  想让它动：guiyun review --set "{it.src}" --place 地点名 --date 2024-05-18')
    if it.status != "skip":
        say(f"  落位：{it.dst}")
    return EXIT_OK


# ── 入口 ────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guiyun",
        description="归云 —— 把网盘里的摄影素材按「区域/活动/抖音 → 年 → 周期 → 拍摄类型」归位，"
        "跟本地归藏同一套规则。只搬不删，全程记账。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用顺序：\n"
            "  guiyun init      写一份配置模板\n"
            "  guiyun login     百度网盘登录\n"
            "  guiyun scan      抓目录树进索引（只看不搬）\n"
            "  guiyun plan      出方案（只看不搬）\n"
            "  guiyun review    看哪些钉不住，逐条钉住\n"
            "  guiyun apply --dry-run   数一遍条数\n"
            "  guiyun apply --yes       真搬\n"
            "  guiyun undo <run_id>     反悔\n"
        ),
    )
    p.add_argument("-v", "--version", action="version", version=f"guiyun {__version__}")
    p.add_argument("--home", help="配置家目录（默认 ~/.guiyun，也可用 GUIYUN_HOME）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="写一份配置模板")
    sp.add_argument("--force", action="store_true", help="覆盖已有的配置")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("scan", help="抓目录树进索引（只看不搬）")
    sp.add_argument("--root", action="append", help="只扫这个目录，可重复；默认用配置里的 scan_roots")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("plan", help="出方案（只看不搬）")
    sp.add_argument("--md", help="另外写一份 markdown 报告到这个路径")
    sp.add_argument("--json", action="store_true", help="只打 JSON（给脚本用，不含人类摘要）")
    sp.add_argument("--all", action="store_true", help="打印全部明细")
    sp.add_argument("--detail", type=int, default=40, help="明细最多列几条（默认 40，0 = 全列）")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("apply", help="执行方案")
    sp.add_argument("--dry-run", action="store_true", help="数条数，不动")
    sp.add_argument("--limit", type=int, default=0, help="这次最多搬几条（先小批量试水用）")
    sp.add_argument("--yes", action="store_true", help="确认执行")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("undo", help="把某次整理反向搬回去")
    sp.add_argument("run_id", help="apply 结束时打印的那个 run_id")
    sp.add_argument("--dry-run", action="store_true", help="只数条数，不动")
    sp.set_defaults(func=cmd_undo)

    sp = sub.add_parser("status", help="看配置、索引、上次整理")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("review", help="列待定 / 人工钉住那条判定")
    sp.add_argument("--set", metavar="路径", help="要钉住的那条（文件夹或文件）")
    sp.add_argument("--place", help="钉地点名（places.toml 里的）")
    sp.add_argument("--area", help="钉园区区域（该地点 places.toml 里配的）")
    sp.add_argument("--activity", help="钉活动名（activity.toml 里的）")
    sp.add_argument("--branch", help="钉分支：抖音 / 区域 / 活动")
    sp.add_argument("--shot", help="钉拍摄类型：航拍 / 地拍 / 延时 / FPV / 其他")
    sp.add_argument("--date", help="钉日期：2024-05-18 / 20240518 / 2019（只有年份）")
    sp.add_argument("--camera", help="钉机型名（会出现在路径里）")
    sp.add_argument("--clear", metavar="路径", help="撤掉人工判定，回到机器推断")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("explain", help="一条为什么这么判")
    sp.add_argument("path", help="源路径（文件夹或文件）")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("clean", help="清掉整理后剩下的空文件夹")
    sp.add_argument("--root", action="append", help="只看这个目录，可重复；默认用配置里的 scan_roots")
    sp.add_argument("--dry-run", action="store_true", help="只数个数，不动")
    sp.add_argument("--yes", action="store_true", help="确认真删")
    sp.set_defaults(func=cmd_clean)

    sp = sub.add_parser("report", help="生成 markdown 整理报告")
    sp.add_argument("--out", help="写到文件（默认打到屏幕）")
    sp.add_argument("--detail", type=int, default=40, help="明细最多列几条（0 = 全列）")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("login", help="百度网盘登录")
    sp.add_argument("--bduss", help="直接给 BDUSS（不给就交互输入）")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("config", help="打印当前配置原文")
    sp.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say("")
        return die("用户中断。", 130)
    except (BackendError,) as exc:
        return die(str(exc))
    except ConfigError as exc:
        return die(str(exc))
    except ValueError as exc:
        # 配置/方案/人工判定表读坏了都是这一路，消息已经写成给人看的了
        return die(str(exc))


if __name__ == "__main__":
    sys.exit(main())
