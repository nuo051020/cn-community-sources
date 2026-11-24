# 统一中文社区来源适配器（source_adapter）

匿名、只读、可审计的中文社区来源适配器。统一接入**贴吧公开搜索**、**AnySearch CLI**、**Just One API（经 Codex MCP）**，把结果归一化成保留原文、作者、时间、父子评论关系与完整度的证据记录，并直接导出为知识库现有的 `search-agent/input.schema.json` 1.0.0 证据包。

本目录是独立采集包 `cn-community-sources` 的包本体（唯一权威实现）。能力说明、付费 token 与异地部署见包根目录的 `SKILL.md` 与 `README.md`。

## 边界（硬约束）

- 不使用 Computer Use；不使用任何浏览器自动化框架；不绕过验证码、登录墙或访问控制。
- 不执行发帖、点赞、关注、私信等账号操作；全部请求匿名、只读。
- 不采集或推断敏感个人信息；公开昵称仅作为原文定位字段。
- 不把搜索摘要冒充帖子或评论原文：发现层记录一律标记 `coverage.level = partial` 并在 `notes` 中写明。
- 不硬编码 Just One API 端点 ID：必须先做端点发现，再读取端点 schema。
- `JUSTONEAPI_TOKEN`、`ANYSEARCH_API_KEY` 等密钥不得写入项目、日志、测试样例或输出；本包只按存在性检查密钥，不做记录、回显或转发。
- 不覆盖或删除工作区既有文件；所有写入都限制在指定输出目录内。

## 目录结构

```text
adapter/
├── source_adapter/            # 包本体（扁平布局，无需安装即可 python3 -m 直接运行）
│   ├── models.py              # SourceRecord / CommentRecord / Coverage / ProviderStatus
│   ├── normalize.py           # URL、时间、文本、密钥脱敏
│   ├── http.py                # 有界、可重试、可缓存的 stdlib HTTP 客户端
│   ├── cache.py               # 原子写入的原始响应缓存与输出路径安全
│   ├── export.py              # search-agent 输入包构建与契约校验
│   ├── pipeline.py            # 采集管线与产物写出
│   ├── verification.py        # 只读烟测与禁用代码路径扫描
│   ├── cli.py                 # python3 -m source_adapter 命令
│   └── providers/             # tieba / anysearch / justone
├── tests/                     # unittest 测试与保存的上游响应 fixture
├── scripts/smoke_test.sh      # 一键验收脚本
├── verification/latest.json   # 最近一次验证结果（机器可读）
└── config.example.toml        # 非敏感配置示例
```

## 安装

要求 Python 3.11+（本机验证于 3.14）。核心包**只用标准库**，无需安装依赖：

```bash
cd "cn-community-sources/adapter"
python3 -m source_adapter doctor
```

可选依赖与可选安装方式：

```bash
# 可选：贴吧楼层与楼中楼深挖
python3 -m pip install aiotieba

# 可选：安装为命令行工具（否则请先 cd 到本目录再执行 python3 -m source_adapter）
python3 -m pip install -e .
source-adapter doctor
```

## 配置

配置只保存非敏感参数，见 `config.example.toml`。密钥只从环境或 Codex 安全配置读取：

| 名称 | 用途 | 本包行为 |
| --- | --- | --- |
| `ANYSEARCH_COMMAND` | 覆盖 AnySearch CLI 命令 | 直接使用，不解析密钥 |
| `ANYSEARCH_SKILL_DIR` | 覆盖 AnySearch skill 目录 | 读取其 `runtime.conf`、`scripts/`；只检查 `.env` 中是否存在 key |
| `ANYSEARCH_API_KEY` | AnySearch 配额 | **仅做存在性检查**，不记录、不回显、不转发其值 |
| `JUSTONEAPI_TOKEN` | Just One API MCP 鉴权 | 只由 Codex 会话内的 MCP 使用；本地程序完全不读取 |

还可使用配置文件（默认 `adapter/config.toml`，或用 `--config PATH` 指定）：

```bash
cp config.example.toml config.toml      # 按需修改后生效；CLI 参数优先于文件
python3 -m source_adapter doctor --config config.toml
```

配置加载器（`source_adapter/config.py`，基于标准库 `tomllib`）只接受 `[run]` / `[providers]` 下的已知键，并且：

- 任何包含 `token`、`api_key`、`secret`、`password`、`credential` 等字样的键会被**直接拒绝**，密钥无法被写进项目；
- `endpoint_id` 不是合法配置键：Just One API 端点 ID 只能由会话内的发现工具在运行时返回；
- 数值范围会校验（`concurrency` 只允许 1 或 2，`request_spacing_seconds` 只允许 1.5–3.0）。

AnySearch CLI 自动发现顺序：`--anysearch-command` > `ANYSEARCH_COMMAND` > `<skill_dir>/runtime.conf` 的 `Command:` > `<skill_dir>/scripts/anysearch_cli.py`。

## 命令

```bash
python3 -m source_adapter doctor [--json] [--fail-on-unavailable]
python3 -m source_adapter tieba-search QUERY [--pages N] [--page-size N] [--output DIR]
python3 -m source_adapter tieba-thread TID [--pages N] [--include-nested] [--title T] [--output DIR]
python3 -m source_adapter anysearch QUERY [--max-results N] [--domain D] [--sub-domain S] [--output DIR]
python3 -m source_adapter anysearch-extract URL [--output DIR]
python3 -m source_adapter justone-tools
python3 -m source_adapter justone-balance
python3 -m source_adapter justone-discover PLATFORM QUERY [--intent search|comments] [--out FILE]
python3 -m source_adapter justone-collect PLATFORM QUERY [--limit N] [--pages N] [--vertical T] [--comments] [--top K] [--max-calls N] [--output DIR]
python3 -m source_adapter justone-manifest PLATFORM QUERY [--comments] [--limit N] [--pages N] [--inbox DIR] [--out FILE]
python3 -m source_adapter ingest-justone PLATFORM PATH [--output DIR]
python3 -m source_adapter validate-input PATH
python3 -m source_adapter verify [--verification-path FILE] [--scan-root DIR] [--json]
```

公共参数：`--cache-dir DIR`（默认 `adapter/.cache`）、`--no-cache`、`--offline`、`--json`、`--anysearch-command`、`--anysearch-skill-dir`。

退出码：`0` 成功；`1` 未取得可用证据（提供器不可用或结果为 0 条）；`2` 参数或输入不合法；`3` `--offline` 未命中缓存。

### 验收命令

以下命令为交接书指定的验收入口，均可在本目录直接执行：

```bash
cd adapter
python3 -m unittest discover -s tests -v
python3 -m source_adapter doctor --json
python3 -m source_adapter tieba-search "高中 暗恋" --pages 1 --output verification/tieba-smoke
python3 -m source_adapter justone-manifest bilibili "校园 爱情 故事" --comments --limit 20
python3 -m source_adapter justone-manifest zhihu "学生时代的爱情故事" --comments --limit 20
```

一键脚本会依次执行单元测试、真实只读烟测与验证报告：

```bash
bash scripts/smoke_test.sh
```

## 数据格式

每次运行（`--output DIR`，或默认 `outputs/<时间戳>-<slug>/`）生成：

| 路径 | 内容 |
| --- | --- |
| `raw/` | 未经改写的上游响应（贴吧 JSON、AnySearch Markdown、MCP 原始响应） |
| `records.jsonl` | 归一化证据记录，一行一条，含帖子与评论 |
| `threads/<platform>-<id>.json` | 完整对象：`records`、`comments`、`comment_tree`（父子嵌套）、`coverage` |
| `search-agent-input.json` | 兼容 `search-agent/input.schema.json` 1.0.0 的证据包 |
| `run-report.json` | 运行台账：提供器状态、调用数、缓存命中、覆盖率、失败、缺口、费用（含实测余额差值）、端点发现与调用审计、产物清单 |

每条记录保留：平台对象 ID（`source_id`）、原始 URL、公开作者名、发布时间、抓取时间、原文（`excerpt`/`full_text`）、`thread_id`/`parent_id`/`floor_number`、互动数据、媒体、`collection_method`、`raw_ref`、`coverage`、`status`/`error`、提供器附加字段 `extra`。

枚举：

- `coverage.level`：`complete` | `partial` | `unknown` | `failed`
- 提供器状态：`ready` | `degraded` | `unavailable`
- 记录状态：`ok` | `degraded` | `failed`

`search-agent-input.json` 顶层键固定为 `schema_version`（`1.0.0`）、`request_id`、`query`、`mode`、`locale`、`constraints`、`results`；结果项键固定为 `source_id`、`title`、`source_type`、`url`、`local_path`、`publisher`、`author`、`published_at`、`retrieved_at`、`excerpt`、`full_text`、`metadata`。`metadata` 承载平台、`record_kind`、`coverage`、`raw_ref`、`engagement`、`comment_count` 等追踪信息。`python3 -m source_adapter validate-input PATH` 会按契约复检。

## 贴吧正文与楼中楼（公开页通道，默认）

`tieba-thread --via public`（默认）走公开移动页 `GET https://tieba.baidu.com/mo/q/thread?kz=<tid>&pn=<page>`，无需 `aiotieba`：

- 楼层：`li[data-info]` 的 `pid/floor_num` + `div.content` 正文；楼主层为 `record_kind="thread"`，其余为 `post`；
- 楼中楼：`div.fr_list > ul.flist > li[pid]`，映射为 `comment` 并保留父子关系；
- `--via aiotieba` 仍可选，两条通道产出同一套记录模型。

**已知限制**：该移动页只服务前 30 层且忽略 `pn`（多页通常以 `repeated_page` 结束）；楼中楼只渲染少量，其余藏在「打开 APP 查看」；点赞数不输出（不编造，记为 0）。`/p/<tid>`、`/p/comment` 等端点对匿名非浏览器客户端返回 403 安全验证，本工具不绕过。

## Just One API 直连（MCP）

`justone-tools / justone-balance / justone-discover` 完全免费；`justone-collect` 按调用计费。

- **凭据**：只从 `JUSTONEAPI_TOKEN` 环境变量读取，绝不写入项目、日志或输出；`doctor` 仅检查存在性。
- **发现驱动**：`search_endpoints`（发现）→ `get_endpoint_schema`（读契约）→ `call_endpoint`（采集）。代码中不存在任何数据端点 ID 字面量，**并有测试用正则守住**。
- **契约语义校验**：搜索意图要求端点接受关键词参数，评论意图要求端点接受对象级 ID（`aid`/`answer_id`/`note_id`），避免把「视频详情」误当「视频搜索」。
- **参数映射**：按 schema 的 `name/required/type/enum` 映射关键词、对象 ID、页码、偏移、游标与枚举；必需参数缺失时只记缺口，不猜值。
- **评论层**：B站与知乎的评论端点返回评论列表本身，用 `kind="comments"` 归一化并保留父子关系（`rpid/parent/root`、`answer_id`）。
- **成本护栏**：每次调用前后读账户余额，`run-report.json` 记录 `paid_calls`、`balance_before/after`、`spent`；`--max-calls` 为硬上限；**MCP 传输层重试次数为 0**，同一请求重放走缓存（免费）。

## Just One API 双阶段桥接（离线通道）

Just One API 只能由 Codex 会话内的 `justoneapi` MCP 工具调用，本地 Python 进程无法直接 import，因此采用两阶段：

1. `justone-manifest` 生成调用清单：包含平台、意图、关键词或对象 ID、分页范围、调用上限、是否需要评论/回复，以及强制性的端点发现步骤。清单中 `endpoint_discovery.endpoint_id` 与 `endpoint_discovery.schema` 初始为 `null`，**必须**由会话内的发现工具在运行时返回后填入，代码里不得写死任何端点 ID。
2. 会话完成调用后，把原始响应（MCP `content[].text` 包装或纯 JSON 均可）保存到清单约定的 inbox 目录，再执行：

```bash
python3 -m source_adapter ingest-justone bilibili outputs/justone-inbox/bilibili-1.json --output outputs/justone-bili
```

归一化会保留 B站 `bvid`/`rpid`、知乎 `question.id`/`answer.id`/`comment.id` 等平台对象 ID 与父子评论关系；端点 schema 的发现结果可用会话内的 schema 工具保存后交给 `ingest_endpoint_schema()` 校验（见 `providers/justone.py`）。

## 限速、重试与缓存

- 默认并发 1，允许配置到 2；同一平台请求间隔默认 2 秒，允许范围 1.5–3.0 秒。
- 对 429、408、5xx 与临时网络错误最多重试 3 次（合计最多 4 次请求），指数退避并尊重 `Retry-After`（秒或 HTTP 日期两种格式）。
- 响应体、子进程运行时间与输出目录均有上限：单次响应 ≤ 2 MB，AnySearch 输出 ≤ 500,000 字符，子进程超时 60 秒。
- 缓存键由 provider + 命名空间 + 规范化参数生成；原始响应按抓取结果落盘，写入走「临时文件 + `os.replace`」原子替换。
- `--offline` 只读缓存，缺失即返回退出码 3，绝不静默联网。

## 费用边界

- 贴吧公开搜索与 AnySearch 走公开接口/匿名额度；本管线本身**不产生任何费用**，`run-report.json` 中 `cost.paid_calls = 0`。
- AnySearch 若配置了 API key，仅由 skill 自带 CLI 使用，本包不参与计费逻辑。
- Just One API 属于付费接口：`justone-manifest` 只生成调用清单（含 `call_budget.max_calls` 与 `requires_user_approval: true`），**未经用户明确确认，不执行任何批量收费调用**。测试与烟测均不触发付费调用。
- 端点发现与 schema 读取合计至少 1 次调用；详情与评论调用按分页与 `--comments` 计。

## 已知限制

- 贴吧公开移动端搜索只返回摘要与主题帖元数据，**不提供完整正文**；正文需 `aiotieba` 深挖（`tieba-thread`）。
- AnySearch 返回搜索摘要；正文需 `anysearch-extract`。搜索摘要中的 `date:` 若是相对时间（如 `11 years ago`）无法解析，会原样保留在 `extra.date_text`，`published_at` 记为 `null`。
- `aiotieba` 未安装时，`tieba-thread` 返回 `unavailable` 并写入缺口，不会静默丢数据。
- Just One API 有两条通道：`justone`（离线清单/归一化，始终 `unavailable` 属预期）与 `justone-mcp`（直连，设置 `JUSTONEAPI_TOKEN` 后为 `ready`）。
- 贴吧正文通道只覆盖前 30 层；`/p/`、`/p/comment` 端点被上游 403 安全验证拦截，本工具不绕过，只使用可用的公开移动页。
- B站/知乎响应映射基于常见字段结构，并提供通用回退映射；字段漂移时会产生 `degraded` 记录与缺口，而不是编造字段。
- 本工作区没有 Git 仓库，因此不执行提交步骤，改以 `verification/latest.json` 与 `run-report.json` 作为可追溯台账。
- 禁用代码路径扫描默认只覆盖 `adapter/`（可用 `verify --scan-root` 扩大范围）。
- 并发：当前实现按页顺序请求，等效并发 1；配置只允许 1 或 2，未提供并发 2 的并行调度（分页与限速要求顺序执行）。

## 测试与验证

```bash
cd adapter
python3 -m unittest discover -s tests -v     # 全量单元与契约测试（253 项）
python3 -m source_adapter verify --json      # 真实只读烟测 + 禁用路径扫描 → verification/latest.json
```

`verify` 会：检查各提供器状态；真实执行一次贴吧公开搜索与一次 AnySearch 搜索（均只读）；记录 Just One API 端点发现的会话依赖；扫描禁止的自动化代码路径；把结论写入 `verification/latest.json`。
