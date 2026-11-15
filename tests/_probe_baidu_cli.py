# 一次性探针：看 BaiduPCS-Go 路径写错时给人的话是不是人话。可删。
from guiyun.backends.baidu import BaiduBackend
from guiyun.backends.base import BackendError

CASES = [
    ("BaiduPCS-Go", "裸命令名，不在 PATH"),
    (r"D:\nowhere\BaiduPCS-Go.exe", "绝对路径但文件不存在"),
    (r"C:\Users\life2\Projects\guiyun\.tools\bpc\BaiduPCS-Go-v4.0.2-windows-x64\BaiduPCS-Go.exe",
     "真实路径（未登录）"),
]

for cli, label in CASES:
    print(f"--- {label}: {cli}")
    try:
        be = BaiduBackend(cli)
    except BackendError as exc:
        print("    构造就抛了：" + str(exc).splitlines()[0])
        continue
    print("    构造 OK")
    try:
        print("    check:", be.check())
    except BackendError as exc:
        print("    check 抛：" + str(exc).splitlines()[0])
