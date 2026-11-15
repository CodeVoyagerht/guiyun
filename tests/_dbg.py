import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from guiyun.config import load
from guiyun.index import Index

cfg = load(Path(__file__).resolve().parent.parent / ".fixture" / "home")
idx = Index(cfg.index_path)
print("include:", cfg.match_include)
print("min_size:", cfg.match_min_size)
for d in idx.dirs():
    p = d["path"]
    kids = idx.children(p)
    print(
        f"{p!r:58} has_children={idx.has_children(p)} "
        f"kids={[(k['name'], k['is_dir'], k['size']) for k in kids]}"
    )
