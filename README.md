# cn-community-sources（独立采集包）

统一收集中文社区与游戏平台的公开「信息 / 作品正文 / 评论」证据。**自包含、可整体复制**，包内含采集代码、测试与技能副本，异地复制后只要有 Python 3.11+ 即可运行（可选依赖见「依赖与平台」）。

能力、token 与边界的完整说明见 [`SKILL.md`](SKILL.md)。

可以用这个 Agent Skill 搜索百度贴吧帖子、从 Steam 中文评测中整理玩家反馈。需要采集小红书笔记、B站视频评论或知乎回答时，可接入按调用计费的 Just One API。

An agent skill for Chinese social media research. Search Baidu Tieba threads and collect player feedback from Chinese Steam reviews. Use Just One API for Xiaohongshu, Bilibili and Zhihu content and comments.

## 安装到 Codex

在项目目录执行以下命令。运行采集脚本需要 Python 3.11 或更高版本。

```bash
npx skills add nuo051020/cn-community-sources --skill cn-community-sources -a codex
.agents/skills/cn-community-sources/run.sh doctor --json
```

## 快速开始

```bash
cd cn-community-sources

./run.sh source-adapter doctor --json     # 查看各提供器就绪状态
./test.sh                                 # 全量单元与契约测试
bash adapter/scripts/smoke_test.sh        # 一键验收（测试 + 真实只读烟测 + 验证报告）
```

常用采集：

```bash
./run.sh source-adapter tieba-search "互动影游" --pages 1 --json
./run.sh source-adapter tieba-thread 主题帖ID --include-nested --json
./run.sh source-adapter steam-reviews APPID --app-name "游戏名" --pages 1 --json
./run.sh source-adapter anysearch "互动影游 玩家反馈" --max-results 10 --json
./run.sh source-adapter anysearch-extract "https://example.com/article" --json
./run.sh source-adapter justone-manifest bilibili "互动影游" --comments --limit 20 --out var/manifest.json
```

输出写入 `var/source-adapter/outputs/<时间戳>-<slug>/`，包含 `raw/`、`records.jsonl`、`threads/`、`search-agent-input.json`、`run-report.json`。

## 已独立验证的能力

把本包放到桌面后，从**其他工作目录**（`/tmp`）、**清空环境变量**（`env -i`）调用 `./run.sh` 实测。两轮验证分别覆盖「零依赖」与「安装可选依赖」两种情况。

**A. 零第三方依赖（系统 Python 3.14，仅标准库）**

| 能力 | 命令 | 结果 |
| --- | --- | --- |
| 单元与契约测试 | `test.sh` | **256 项全部通过** |
| 提供器就绪检查 | `doctor --json` | exit 0，`aiotieba` 正确显示 `unavailable` |
| 贴吧搜索（信息） | `tieba-search` | 18 条记录 |
| Steam 评测（评论） | `steam-reviews` | 10 条记录 |
| 贴吧深挖（缺 aiotieba） | `tieba-thread --via aiotieba` | 优雅降级：`failed` + 缺口「可选依赖 aiotieba 未安装」 |
| AnySearch（缺 requests） | `anysearch` | 明确失败：`records=0`、`coverage=failed` |

**B. 安装可选依赖后（`pip install requests aiotieba`，Python 3.14 venv）**

| 能力 | 命令 | 结果 |
| --- | --- | --- |
| 提供器就绪检查 | `doctor --json` | exit 0 |
| 贴吧搜索（信息） | `tieba-search` | 18 条记录 |
| 贴吧正文（公开页） | `tieba-thread` | 30 条记录 |
| 贴吧正文（aiotieba 通道） | `tieba-thread --via aiotieba` | 30 条记录 |
| Steam 评测（评论） | `steam-reviews` | 20 条记录 |
| AnySearch 搜索（信息） | `anysearch` | 5 条记录 |
| AnySearch 正文提取 | `anysearch-extract` | 1 条，coverage=complete |
| Just One API 调用清单（离线） | `justone-manifest` | exit 0，含调用预算与审批标记 |
| Just One API 归一化（离线） | `ingest-justone` | 1 条记录 + 2 条评论 |
| 证据包契约校验 | `validate-input` | 通过 |
| 一键验收（含真实只读烟测） | `adapter/scripts/smoke_test.sh` | 全部步骤通过 |

未配置 `JUSTONEAPI_TOKEN` 时，`justone-tools` / `justone-balance` / `justone-discover` / `justone-collect` 会以退出码 1 明确报错「JUSTONEAPI_TOKEN 未设置」，这是预期行为。

**包内修正**：`aiotieba >= 4.7` 要求 `tid`/`pid` 为 `int`，已修正 `--via aiotieba` 通道的调用与对应测试（修复前该通道返回 `failed`、0 条记录）。同源工程 `integrations/source_adapter` 仍保留该问题。

## 付费 token

| 名称 | 必需 | 费用 | 用途 |
| --- | --- | --- | --- |
| `JUSTONEAPI_TOKEN` | B站/知乎/小红书/抖音等必须 | **按调用计费** | Just One API 平台搜索、作品详情、评论采集与会话内 MCP 鉴权 |
| `ANYSEARCH_API_KEY` | 可选 | 免费 | 仅提高 AnySearch 速率；不配置时匿名额度可用 |

```bash
export JUSTONEAPI_TOKEN="..."     # 只在目标机器环境中设置，不要写入任何文件
```

`doctor` 只做存在性检查，不会回显或记录密钥值。付费采集前先说明预计调用数与费用，`--max-calls` 是硬上限。

## 依赖与平台

- **运行时**：任何 Python 3.11+ 均可（`run.sh` 使用系统 `python3`，可用 `PYTHON=/path/to/python3` 指定；Windows 用 `run.bat`）。本包不含内置运行时，体积约 2 MB。
- **零依赖即可用**：贴吧公开搜索/正文、Steam 评测只用标准库。
- **可选依赖**（按需安装）：
  - `pip install requests` —— AnySearch CLI（`anysearch` / `anysearch-extract`）使用。
  - `pip install aiotieba` —— 贴吧深挖通道（`tieba-thread --via aiotieba`）使用；缺失时该通道返回 `unavailable` 并在报告中记缺口，不影响其他能力。
- Just One API 通过会话内 MCP 调用，不需要额外 pip 依赖。

## 目录结构

```text
cn-community-sources/
├── SKILL.md              # 技能说明：能力矩阵、token、边界
├── README.md             # 本文件
├── run.sh / run.bat      # 统一入口
├── test.sh               # 全量测试
├── adapter/              # 采集包本体（唯一权威实现）+ 测试 + 配置
├── skills/anysearch/     # AnySearch CLI 技能副本
├── skills/justoneapi-data/ # Just One API MCP 技能副本
└── var/                  # 运行数据（首次运行时自动创建，可删除重建）
```

## 边界

匿名、只读；不使用浏览器自动化与 Computer Use；不绕过验证码/登录墙/访问控制；不执行账号操作；不采集敏感个人信息；不把搜索摘要冒充原文。详见 `SKILL.md`。
