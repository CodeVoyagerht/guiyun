# 归云 guiyun

[![CI](https://github.com/CodeVoyagerht/guiyun/actions/workflows/ci.yml/badge.svg)](https://github.com/CodeVoyagerht/guiyun/actions/workflows/ci.yml)

**云端没有硬链接，所以每一次移动都要留退路。**

把网盘里的摄影/航拍素材按 `区域 / 活动 / 抖音 → 年 → 备份周期 → 拍摄类型 → [机型]` 归位。
只搬不删、全程记账、一条命令把整批撤回。判不准的进 `待定/`，**上传时间永不当拍摄日期用**。

它是 **归藏 guicang** 的云端姊妹篇：归藏在本机硬盘里建分类树（硬链接，原件不动），
归云是真搬 —— 因此多了索引、方案预演、台账与撤销。**两边的分类规则是同一套**：
共用同一份 `places.toml` / `activities.toml`，网盘里和本地 `_归藏\` 里看到的路径逐段同构。

```
/摄影库/
├─ 区域/
│  └─ 郑州/                        ← 有两个以上城市时才出现
│     └─ 泡泡塘森林溪谷/            ← 地点（places.toml 的 name）
│        └─ 水世界/                ← 园区区域（places.toml 的 areas）
│           └─ 2024/2024-05-18_05-24/   ← 年 / 备份周期（周六 → 周五）
│              └─ 航拍/
│                 └─ 2024-05-18 泡泡塘 水世界/   ← 整堆搬，RAW+JPG+SRT 不散架
├─ 活动/
│  └─ 开封菊花文化节/2024-第39届/2024/2024-07-06_07-12/航拍/
├─ 抖音/                          ← 抖音拍摄类自成一枝
│  └─ 2024/2024-08-03_08-09/地拍/
└─ 待定/                          ← 判不准的都在这儿，不瞎归
   └─ 2024/2024-06-01_06-07/地拍/
```

## 三条设计前提

1. **先索引，后计算。** `scan` 把云端目录树抓进本地 SQLite（一次几百个请求）；
   `plan` 之后全在本机算，**零网盘调用**。改主意重算一百次也不花一次请求 ——
   因为每次列目录都是一次可能被风控看见的调用，这在国内网盘上不是性能问题，是账号安全问题。
2. **风控即熔断。** 每条移动之间节流 0.35 秒；撞上「请求过于频繁 / 安全验证」立刻停整批，
   报出已搬几条、被什么挡住，绝不重试硬刚。
3. **只搬不删。** `apply` 只做 move；`clean` 只删**空**目录且必须 `--yes`。
   所有移动都进 `index.sqlite` 的台账，`undo` 按台账反序搬回去。代码里没有任何"删素材"的路径。

## 装

```bash
pip install -e .                     # 需要 Python 3.11+
```

**还要装姊妹项目「归藏」guicang**——判定规则（地点、活动、拍摄类型）不在这个仓库里，
两边的分类法共用一份 `places.toml` / `activities.toml`：

```bash
pip install -e <归藏仓库路径>        # 归藏的 src 目录里就是包本体
```

**要求归藏 ≥ 0.3.5**：再早的版本会把「EXIF 只有 Make = DJI、没有 Model」的素材
（`DJI_0101.JPG` 这种最典型的航拍原片）判成**地拍**，落进待定的「其他」；0.3.5 起改判
**航拍**，落进「航拍」。归云看到的差别就是整理出来的文件夹名。

不装的话 `guiyun` 装得上，但 `plan` / `apply` 会直接告诉你要先去装归藏：

```
归云现在借用「归藏」的分类规则，得先把归藏装上
```

这也是为什么 `pyproject.toml` 里 `dependencies = []` 看着像零依赖——本仓库自己的代码
确实只用标准库，但**跑起来**需要归藏（它还没发到 PyPI，所以没写进 dependencies）。

百度网盘还要一份 BaiduPCS-Go（单文件 CLI）。仓库带了 Windows x64 版：

```
.tools/bpc/BaiduPCS-Go-v4.0.2-windows-x64/BaiduPCS-Go.exe
```

不想 `pip install -e .` 也行，`py -m guiyun` / `python3 -m guiyun` 完全等价。
**Windows 上别用裸 `python`** —— 装了 Microsoft Store 版 Python 的机器上它是个什么都不做的占位程序。

## 用

```bash
guiyun init                       # 写 ~/.guiyun/guiyun.toml
# 打开它，把 [taxonomy] source 指到放 places.toml / activities.toml 的目录
# （一般就是本地素材库的 .guicang；用 guicang init 生成）
guiyun login                      # 百度网盘登录（BDUSS，getpass 输入）
guiyun status                     # 配置 / 索引 / 上次整理
guiyun scan                       # 抓目录树进索引（只读）
guiyun plan                       # 出方案：目标树 + 条数 + 字节（只算不动）
guiyun review                     # 列待定；--set 一条条钉住
guiyun apply --dry-run            # 数条数
guiyun apply --limit 5 --yes      # 先搬 5 条试水
guiyun apply --yes                # 确认后全量
guiyun report --out 报告.md        # 留一份台账
guiyun clean --dry-run            # 搬空的源目录
guiyun clean --yes                # 真清（只删空的）
```

| 命令 | 干什么 | 动网盘？ |
|---|---|---|
| `init [--force]` | 写配置骨架 | 否 |
| `login [--bduss]` | 百度网盘登录 | 只登录 |
| `status` | 配置 / 索引 / 最近几次 run | 否 |
| `scan [--root R]...` | 抓目录树进索引 | **只读**（列目录） |
| `plan [--md F] [--json] [--all] [--detail N]` | 出方案、打目标树 | 否 |
| `review [--set P] [--place][--area][--activity][--branch][--shot][--date][--camera] [--clear P]` | 列待定 / 钉住 / 撤销 | 否 |
| `explain <路径>` | 一条凭什么这么判 | 否 |
| `apply [--dry-run] [--limit N] [--yes]` | 搬 | **是（move）** |
| `undo <run_id> [--dry-run]` | 反向搬回去 | **是（move）** |
| `clean [--root R] [--dry-run] [--yes]` | 清空壳目录 | **是（只删空的）** |
| `report [--out F] [--detail N]` | markdown 报告 | 否 |
| `config` | 打印配置原文 | 否 |

`--home` 是顶层参数：`guiyun --home D:\test scan`（在子命令之前）。也可以设 `GUIYUN_HOME`。

`~/.guiyun/` 里：`guiyun.toml`（配置）、`index.sqlite`（索引 + 台账）、`plan.json`（上次方案）、
`manual.json`（人工判定）、`runs/`。

## 后端

```toml
[backend]
kind = "baidu"        # 或 "local"

[backend.local]
root = "E:\\网盘镜像"  # local 后端的命名空间根：/摄影库 ↔ E:\网盘镜像\摄影库
```

`local` 让整套流程不登录也能跑（网盘客户端把云端挂成本地盘、或者先在测试盘上演练一遍）。

## 判不准的时候

- **云端读不到 EXIF。** 日期和机型只能从目录名、文件名推 —— 这是这条路的物理上限，不是 bug。
- **文件修改时间一律不算日期。** 网盘上的 mtime 常常是上传时间，照它分会把一次拍摄拆成几十个文件夹
  且事后没人看得出来。这是 `is_auto` 里唯一一条硬否决。
- 认不出的进 `待定/<年>/<周期>/<拍摄类型>/`；`guiyun explain <路径>` 会说清是谁赢了、
  差在哪；`guiyun review --set` 钉住（`--place` / `--area` / `--activity` / `--branch` / `--shot` /
  `--date` / `--camera`），机器推断永远压不过人。

## 已知限制

- 第一次真机跑要验证 `mv src <不存在的完整路径>` 是否真能一次完成"换目录 + 改名"
  （源码看成立，开发期只用 local 后端验证过；退路是先移进去再改名，两步）。详见
  `skills/guiyun/references/backend.md`。
- 搬完源目录会留空壳文件夹（不是文件），要 `guiyun clean` 清；不清不影响正确性。
- `scan` 有 20 万条的保险丝，撞上只扫一部分（刻意停的，一次扫太多容易被风控）。
- `scan` 默认只下到 8 层（`[scan] max_depth`），更深的目录不进去；个别目录读失败也只是跳过。
  这两种情况都会打印「有 N 个目录…」，`status` 也会一直提醒——那批素材确实没进索引，别当噪音。
- 索引在 `apply` 后标记 `stale`，**不自动重扫**。要接着整理得自己 `guiyun scan`。

## 仓库结构

```
guiyun/                 引擎（本仓库代码只用标准库；判定规则来自归藏 guicang）
├─ backends/            base 契约 + baidu（BaiduPCS-Go）+ local
├─ config.py            ~/.guiyun/guiyun.toml 读写与校验
├─ index.py             SQLite 索引 + 遍历 + 台账
├─ rules.py             文本证据：日期 / 机型（纯文本那两样）
├─ decide.py            判定：借归藏的规则引擎，出分支/地点/园区区域/时间/拍摄类型
├─ manual.py            人工判定表（manual.json）
├─ plan.py              零副作用算方案
├─ apply.py             执行 + 撤销（写台账）
├─ report.py            目标树与 markdown 报告
└─ cli.py               子命令
skills/guiyun/          agent 技能（SKILL.md + references/ + scripts/install.py）
preset/                 DeepSeek Harness agent 预设（归云）
tests/                  fixture 生成 + 端到端自检
.tools/bpc/             附带的 BaiduPCS-Go v4.0.2（Windows x64）
```

自检：`python tests/e2e.py`（造一个假网盘，跑 scan→plan→review→apply→clean→undo 全流程）。

## 文档

- `skills/guiyun/SKILL.md` — 给 agent 的操作手册（工作流、铁律、判定速查）
- `skills/guiyun/references/taxonomy.md` — 落位形状与全部配置字段
- `skills/guiyun/references/evidence.md` — 证据链、阈值表、机型正则、人工判定格式
- `skills/guiyun/references/backend.md` — BaiduPCS-Go 契约、错误码与风控、`clean` 的安全边界
- `preset/README.md` — 预设怎么装（junction + 重启 DSH）

## 仓库

<https://github.com/CodeVoyagerht/guiyun> —— 姊妹项目：<https://github.com/CodeVoyagerht/guicang>

MIT.
