"""一次性校验：归云预设能不能被 YAML 解析，结构与已验证的归藏预设是否一致。"""
import os
import re

import yaml


def load(path):
    text = open(path, encoding="utf-8").read()
    text = text.replace("!!js ", "!!str ")
    return yaml.safe_load(text)


base = os.path.expanduser(r"~\.dsh\profiles\desktop\agent-presets")
g = load(os.path.join(base, "guicang", "agent.cordis.yml"))
y = load(os.path.join(base, "guiyun", "agent.cordis.yml"))


def shape(node):
    out = []
    for item in node:
        row = [item.get("id"), item.get("name"), bool(item.get("config")), bool(item.get("group"))]
        cfg = item.get("config")
        if isinstance(cfg, list):
            row.append([c.get("id") for c in cfg])
        out.append(row)
    return out


print("段数:", len(g), len(y))
sg, sy = shape(g), shape(y)
print("结构一致:", sg == sy)
if sg != sy:
    for a, b in zip(sg, sy):
        if a != b:
            print("  DIFF", a, b)

p = y[0]
assert p["id"] == "persona", p["id"]
pre = p["config"]["prefix"]
print("persona 字数:", len(pre))
print("persona 段标题:", [ln for ln in pre.splitlines() if ln.startswith("# ")])
planning = next(item for item in y if item.get("id") == "planning")
print("plan-mode section 字数:",
      len(planning["config"][0]["config"]["section"]))

bad = [m for m in re.findall(r"\{\{(\w+)\}\}", pre) if m not in ("model", "provider")]
print("persona 里的未注册变量:", bad or "无")

# 整份文件里真引用（排除注释行）的变量
raw = open(os.path.join(base, "guiyun", "agent.cordis.yml"), encoding="utf-8").read()
live = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
print("全文件真引用变量:", sorted(set(re.findall(r"\{\{(\w+)\}\}", live))))
