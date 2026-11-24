---
name: cn-community-sources
version: 1.0.0
description: 统一收集中文社区与游戏平台的公开「信息 / 作品正文 / 评论」证据，可整体复制到异地环境直接运行。当任务涉及贴吧、B站（哔哩哔哩）、知乎、小红书、抖音、快手、微博、Steam 评测，或需要玩家反馈、用户评论、社区舆情、作品（游戏/视频/帖子/回答）详情与网页正文提取时使用。所有采集匿名只读、保留原文与父子评论关系、输出覆盖率与缺口，并明确区分免费接口与按调用计费的付费接口。
credentials:
  - name: JUSTONEAPI_TOKEN
    required: true
    paid: true
    description: "Just One API 付费接口凭据。用于 B站/知乎/小红书/抖音/快手/微博等平台的搜索、作品详情与评论采集，按调用计费。缺失时这些平台不可用（离线清单通道仍可用）。"
    storage: "仅目标机器的环境变量，或会话内 justoneapi MCP 的 bearer_token_env_var；禁止写入本 Skill 目录、config.toml、日志与版本库。"
  - name: ANYSEARCH_API_KEY
    required: false
    paid: false
    description: "AnySearch 可选密钥，仅用于提高速率与配额；不配置时匿名额度可用（速率受限）。它不产生费用，也不是付费依赖。"
    storage: "环境变量，或 skills/anysearch/.env；不得提交。"
---

# 中文社区来源采集（信息 / 作品 / 评论）

本目录是一个**自包含、可整体复制**的采集包：把贴吧公开搜索、贴吧正文与楼中楼、Steam 评测、AnySearch 搜索与正文提取、Just One API 多平台搜索/详情/评论，统一成一套只读、可追溯、带覆盖率的证据采集能力。

包内含采集代码、测试与技能副本，复制到异地后只要有 Python 3.11+ 即可运行；贴吧公开通道与 Steam 仅用标准库，AnySearch 与贴吧深挖通道需要按需安装可选依赖（见「依赖清单」）。

## 何时启用

任一条件成立时启用：

1. 需要**评论 / 玩家反馈 / 用户声音**：Steam 评测、贴吧楼中楼、B站与知乎评论。
2. 需要**信息 / 讨论发现**：某个题材、游戏或话题在社区里的帖子与讨论分布。
3. 需要**作品正文或详情**：贴吧主题帖楼层、B站视频、知乎问题与回答、网页正文。
4. 需要把上述内容整理成**可追溯证据**：原文、作者、时间、链接、父子评论关系、覆盖率与缺口。

不适用：登录、发帖、点赞、私信等账号操作；绕过验证码或访问控制；采集敏感个人信息。见下文「边界与红线」。

## 目录结构

```text
cn-community-sources/
├── SKILL.md              # 本文件：能力、依赖与 token 说明
├── README.md             # 异地部署与自检说明
├── run.sh / run.bat      # 启动脚本（兼容 ./run.sh source-adapter <命令>）
├── test.sh               # 包内全量测试
├── adapter/              # 采集包本体 + 测试 + 配置（唯一权威实现）
│   ├── source_adapter/   # providers / pipeline / cli / http / cache / export ...
│   ├── tests/            # 单元与契约测试（含离线 fixture）
│   ├── scripts/smoke_test.sh
│   ├── verification/     # 最近一次烟测报告
│   └── config.toml       # 非敏感配置（输出目录、限速、分页上限）
├── skills/
│   ├── anysearch/        # AnySearch CLI 技能副本（本地副本，无主目录依赖）
│   └── justoneapi-data/  # Just One API MCP 技能副本
└── var/                  # 运行数据（输出、缓存），可随时删除重建
```

## 入口

```bash
./run.sh source-adapter <命令> [参数...]      # 推荐写法
./run.sh <命令> [参数...]                     # 等价简写
```

`run.sh` 使用系统 `python3`（需 3.11+，会自检版本；可用 `PYTHON=/path/to/python3` 指定解释器），并自动把 `ANYSEARCH_SKILL_DIR` 指向 `skills/anysearch`。所有路径都由脚本自身位置推导，**在任意工作目录下执行都有效**。

## 能力矩阵

按证据层划分，**搜索摘要、作品正文、评论是三层不同证据，不得互相冒充**。

| 平台 / 来源 | 信息（发现） | 作品正文 | 评论 | 费用 | 依赖 |
| --- | --- | --- | --- | --- | --- |
| 贴吧（公开移动端） | `tieba-search`（摘要 + 主题帖元数据） | `tieba-thread`（楼层正文） | `tieba-thread --include-nested`（楼中楼，保留父子关系） | 免费 | 标准库；`aiotieba` 仅增强深挖 |
| 贴吧（aiotieba 通道） | — | `tieba-thread --via aiotieba` | 同左（可选 `--include-nested`） | 免费 | 可选依赖 `aiotieba`（`pip install aiotieba`） |
| Steam | — | 官方 `appreviews` 评测页 | `steam-reviews`（任意 appid 评测） | 免费 | 标准库 |
| AnySearch | `anysearch`（搜索摘要） | `anysearch-extract`（URL → Markdown 正文） | — | 免费（匿名额度；可选 key 提速） | `skills/anysearch` CLI |
| Just One API：B站 / 知乎 / 小红书 / 抖音 / 快手 / 微博 / 电商等 | `justone-discover`、`justone-collect`（搜索） | 详情端点（视频、回答、笔记、商品等） | `justone-collect --comments` | **付费**（按调用计费） | **`JUSTONEAPI_TOKEN`** + 会话内 `justoneapi` MCP |

说明：

- 贴吧公开移动端搜索**只给摘要与元数据**，不提供完整正文；正文与楼中楼必须走 `tieba-thread`。
- AnySearch 搜索**只给摘要**；正文必须走 `anysearch-extract`。
- Just One API 的端点 ID **永不硬编码**：必须先发现端点、再读 schema、最后调用。
- 「作品」在不同平台是不同对象：Steam 是游戏评测、贴吧是主题帖、B站是视频、知乎是问题/回答。本包用同一套记录模型表达它们。

## 命令速查

### 免费：信息发现与作品正文

```bash
# 贴吧搜索（摘要 + 元数据）
./run.sh source-adapter tieba-search "互动影游" --pages 1 --json

# 贴吧主题帖：楼层正文 + 可选楼中楼
./run.sh source-adapter tieba-thread 主题帖ID --pages 1 --include-nested --json
./run.sh source-adapter tieba-thread 主题帖ID --via aiotieba --include-nested --json

# Steam 任意 appid 评测
./run.sh source-adapter steam-reviews 你的APPID --app-name "游戏名" --pages 1 --page-size 20 --json

# AnySearch 搜索摘要 / 正文提取
./run.sh source-adapter anysearch "互动影游 玩家反馈" --max-results 10 --json
./run.sh source-adapter anysearch-extract "https://example.com/article" --json
```

### 免费但不计费、需要 token：Just One API 发现层

```bash
# 列出可用工具、查询账户余额、发现端点并读取 schema（这些调用不计费，但要有凭据）
./run.sh source-adapter justone-tools
./run.sh source-adapter justone-balance --json
./run.sh source-adapter justone-discover bilibili "互动影游" --intent search --json
./run.sh source-adapter justone-discover bilibili 视频ID --intent comments --json
```

### 付费：Just One API 数据采集

```bash
# 按调用计费；--max-calls 是硬上限；--comments 追加评论层
./run.sh source-adapter justone-collect bilibili "互动影游" --limit 20 --comments --top 2 --max-calls 10 --json
```

执行付费采集前必须：先向用户说明预计调用数与费用，取得明确的调用上限或确认；不要用「继续」「看看」这类模糊答复当作授权。

### 离线桥接：无需本地 token

当本地进程无法持有 token、或需要由会话内 MCP 完成调用时，用两阶段桥接：

```bash
# 1) 生成调用清单（完全离线，不需要 token）
./run.sh source-adapter justone-manifest bilibili "互动影游" --comments --limit 20 --out var/justone-manifest.json

# 2) 由会话内 justoneapi MCP 按清单完成端点发现与调用，把原始响应存入清单约定的 inbox 目录

# 3) 归一化原始响应（离线，不需要 token）
./run.sh source-adapter ingest-justone bilibili var/source-adapter/outputs/justone-inbox/bilibili-1.json
```

### 就绪检查与验收

```bash
./run.sh source-adapter doctor --json          # 各提供器状态：ready / degraded / unavailable
./run.sh source-adapter verify --json          # 只读烟测 + 禁用代码路径扫描 → adapter/verification/latest.json
./test.sh                                      # 包内全量单元与契约测试
bash adapter/scripts/smoke_test.sh             # 一键验收（测试 + 真实只读烟测 + 验证报告）
```

## 付费 token 配置（必读）

### 1. `JUSTONEAPI_TOKEN` —— 唯一的付费接口凭据

- **用途**：Just One API 的 B站 / 知乎 / 小红书 / 抖音 / 快手 / 微博等平台的搜索、作品详情与评论采集；也用于会话内 `justoneapi` MCP 鉴权。
- **计费**：`justone-collect` 与数据端点调用**按调用计费**；`justone-tools`、`justone-balance`、`justone-discover` 与端点 schema 读取**不计费**（但都需要凭据）。`justone-manifest` 与 `ingest-justone` 完全离线，不需要凭据。
- **注入方式（二选一）**：
  - 目标机器环境变量：`export JUSTONEAPI_TOKEN="..."`（只在该机器的会话/终端环境中存在）。
  - 会话内 MCP 配置：`bearer_token_env_var = "JUSTONEAPI_TOKEN"`，MCP 地址 `https://mcp.justoneapi.com/mcp`（详见 `skills/justoneapi-data/SKILL.md`）。
- **获取**：在 Just One API 官网/控制台申请。本包不含凭据，也不代购额度。
- **禁止**：不得写入本包任意文件（含 `SKILL.md`、`adapter/config.toml`、`var/` 产物、日志、报告、测试 fixture）或版本库。本包的配置加载器会**直接拒绝**任何含 `token` / `api_key` / `secret` / `password` / `credential` 字样的配置键。
- **本包行为**：`doctor` 对 `JUSTONEAPI_TOKEN` **只做存在性检查**（输出 true/false），不记录、不回显、不转发其值。未见凭据时 `justone-mcp` 为 `unavailable`，`justone-discover/collect` 直接报错，这是预期行为而非故障。

### 2. `ANYSEARCH_API_KEY` —— 可选，不是付费依赖

- **用途**：提高 AnySearch 速率与配额。
- **不配置时**：匿名额度仍可用，`doctor` 显示为 `degraded`（可用但受限），搜索与正文提取照常工作。
- **注入方式**：环境变量 `ANYSEARCH_API_KEY`，或 `skills/anysearch/.env`（该目录下只有 `.env.example`，复制后自填；`.env` 不得提交）。
- **它不产生费用**，因此「必须配置的付费 token」只有 `JUSTONEAPI_TOKEN` 一个。

### 3. 付费调用的成本护栏

- `justone-manifest` 会在清单里写入 `call_budget.max_calls` 和 `call_budget.requires_user_approval: true`；未经用户明确确认不得执行批量收费调用。
- `justone-collect --max-calls N` 是硬上限。
- 每次调用前后读取账户余额，`run-report.json` 记录 `paid_calls`、`balance_before/after`、`spent`；缓存命中不重复计费。
- MCP 传输层重试次数为 0，同一请求重放走缓存（免费），避免失败自动重试造成重复扣费。

## 证据产物与语义

每次采集生成一个运行目录（`--output DIR`，默认 `var/source-adapter/outputs/<时间戳>-<slug>/`）：

| 产物 | 内容 |
| --- | --- |
| `raw/` | 未经改写的上游响应（贴吧 JSON、AnySearch Markdown、MCP 原始响应） |
| `records.jsonl` | 归一化证据记录，一行一条，含帖子与评论 |
| `threads/<platform>-<id>.json` | 完整对象：`records`、`comments`、`comment_tree`（父子嵌套）、`coverage` |
| `search-agent-input.json` | 兼容旧 `search-agent/input.schema.json` 1.0.0 的证据包 |
| `run-report.json` | 台账：提供器状态、调用数、缓存命中、覆盖率、失败、缺口、费用、产物清单 |

记录字段保留：平台对象 ID、原始 URL、公开作者名、发布时间、抓取时间、`excerpt`/`full_text`、`thread_id`/`parent_id`/`floor_number`、互动数据、媒体、`collection_method`、`raw_ref`、`coverage`、`status`/`error`。

覆盖率枚举：`complete` | `partial` | `unknown` | `failed`。**发现层（搜索摘要）一律为 `partial`**，不得当作原文使用。

## 依赖清单（插件与运行时）

| 依赖 | 类型 | 是否必需 | 说明 |
| --- | --- | --- | --- |
| Python 3.11+ | 运行时 | 必需 | 用系统 `python3`（`run.sh` 自检版本，可用 `PYTHON=/path/to/python3` 指定）；包内不含运行时 |
| Python 标准库 | 运行时 | 必需 | 贴吧公开搜索/正文与 Steam 只用标准库即可运行 |
| `requests` | Python 包（可选） | AnySearch 必需 | `pip install requests`；缺失时 `anysearch` / `anysearch-extract` 返回 `coverage=failed` |
| `aiotieba` | Python 包（可选） | 贴吧深挖必需 | `pip install aiotieba`；缺失时该通道返回 `unavailable`/`failed` 并记录缺口，不影响其他能力 |
| `skills/anysearch` | 技能（本地副本） | AnySearch 必需 | AnySearch CLI 与脚本；`run.sh` 已把它设为 `ANYSEARCH_SKILL_DIR` |
| `skills/justoneapi-data` | 技能（本地副本） | Just One API 必需 | 会话内 `justoneapi` MCP 配置与用法 |
| Just One API MCP | 远程服务 | 直连通道必需 | `https://mcp.justoneapi.com/mcp`；需要 `JUSTONEAPI_TOKEN` |

以上技能副本均为**包内相对路径**，不引用用户主目录中的同名技能或绝对路径。

## 异地部署

1. 整体复制 `cn-community-sources/` 到目标机器（保持目录结构；包内为纯代码与技能副本，约 2 MB）。
2. 确认 Python 3.11+ 可用：`./run.sh source-adapter doctor --json`（`run.sh` 会自检版本并给出明确报错）。
3. 按需安装可选依赖：`pip install requests aiotieba`（AnySearch 与贴吧深挖需要；仅用贴吧公开通道与 Steam 时可跳过）。Windows 用 `run.bat`。
4. 需要付费平台时，再在目标机器注入 `JUSTONEAPI_TOKEN`（见上文）。
5. 自检：`./test.sh`、`./run.sh source-adapter doctor --json`、`bash adapter/scripts/smoke_test.sh`。

## 边界与红线

- 全部请求匿名、只读；不使用浏览器自动化，不使用 Computer Use，不绕过验证码、登录墙或访问控制。
- 不执行发帖、点赞、关注、私信等账号操作。
- 不采集或推断敏感个人信息；公开昵称仅作为原文定位字段。
- 不把搜索摘要冒充帖子或评论原文；不编造缺失数字（如贴吧公开页不输出点赞数时记 0 并说明）。
- 不硬编码 Just One API 端点 ID；必须先发现端点再读 schema。
- 密钥只从环境或安全配置注入。
- 不覆盖或删除包外既有文件；所有写入限制在包内 `var/` 与指定输出目录。

## 退出码与排错

| 退出码 | 含义 | 处理 |
| --- | --- | --- |
| `0` | 成功 | — |
| `1` | 未取得可用证据（提供器不可用或结果 0 条） | 查 `run-report.json` 的 `failures` / `gaps` |
| `2` | 参数或输入不合法 | 按报错修正参数 |
| `3` | `--offline` 未命中缓存 | 去掉 `--offline` 或先联网采集一次 |

常见现象：

- `justone` 提供器始终 `unavailable` 属**预期**（离线清单通道）；直连看 `justone-mcp`。
- 未设置 `JUSTONEAPI_TOKEN` 时调用 `justone-discover/collect` 会明确报错，不会静默降级到抓取。
- 贴吧正文只覆盖前 30 层，楼中楼大部分藏在「打开 APP 查看」；`/p/` 与 `/p/comment` 端点被上游 403 拦截，本工具不绕过。
- AnySearch 摘要中的相对时间（如 `11 years ago`）无法解析时保留在 `extra.date_text`，`published_at` 记为 `null`。

## 已知限制

- 贴吧公开搜索不提供完整正文；Steam 只覆盖官方评测接口，无游戏搜索。
- AnySearch 返回摘要，正文需另行提取；匿名额度速率受限。
- Just One API 的数据端点 ID 与字段结构由上游运行时决定；字段漂移会产生 `degraded` 记录与缺口，而不是编造字段。
- 并发固定为顺序执行（配置只允许 1 或 2）；分页与限速要求请求间隔 1.5–3.0 秒。
- 包内不含 Python 运行时，依赖目标机 Python 3.11+；AnySearch 需 `requests`，贴吧深挖需 `aiotieba`，缺失时对应能力降级并记录缺口。
- 付费通道（Just One API 数据调用）的实际费用与可用平台取决于账户额度，需在目标机器实测。
- 本能力面向单企业内部团队使用，尚未达到企业多人部署验收。

## 设计沿革

本包的采集与证据语义沿用既有的「统一中文社区来源适配」实现（同一套提供器边界、覆盖率语义与 `run-report.json` 台账约定）。包内 `adapter/` 是唯一权威实现；修改能力边界或契约时应同步更新本文件与 `adapter/README.md`。
