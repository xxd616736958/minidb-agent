# MiniDB Agent

MiniDB Agent 是一个面向 PostgreSQL 管理任务的智能体项目。它基于 LangGraph / LangChain 构建，目标不是简单聊天，而是帮助用户完成数据库诊断、慢 SQL 分析、schema 检查、索引建议、变更 SQL 草拟、审批、执行验证和最终交付报告。

项目当前重点是让智能体具备可控、可审计、可恢复的数据库管理能力：

- 任务理解与意图建模
- 规划与任务分解
- Agent Loop 推理循环
- 上下文管理与记忆系统
- PostgreSQL 专用工具
- 工具注册、调用与安全策略
- 执行环境与工作区管理
- 状态管理、错误处理和自我修复
- 人机协作、SQL 审批和权限控制
- 质量检查、交付包、最终报告和审计 manifest
- 交互式 CLI 与非交互 exec 模式

## 快速启动

### 1. 准备环境

要求：

- Python 3.11+
- DeepSeek API Key 或 OpenAI API Key
- 可选：PostgreSQL 目标库连接串

首次安装：

```bash
cd /Users/nncc/code/agent_analyze/mini_agent
make install
```

国内网络可以显式指定镜像源：

```bash
make install PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
```

如果 `.venv` 和 `.env` 已存在，可以直接启动。

安装完成后会提供 `minidb-agent` CLI 入口。

如果之前激活过旧虚拟环境，请先退出再重新激活当前项目环境：

```bash
deactivate 2>/dev/null || true
source /Users/nncc/code/agent_analyze/mini_agent/.venv/bin/activate
command -v minidb-agent
```

### 2. 配置 `.env`

最小配置示例：

```bash
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LANGSMITH_TRACING=false
```

如需连接 PostgreSQL 目标库：

```bash
POSTGRES_TARGET_URL=postgresql://user:password@127.0.0.1:5432/appdb
POSTGRES_TARGET_ENV=dev
```

说明：

- `POSTGRES_TARGET_URL` 用于 PostgreSQL 管理工具连接目标库。
- `POSTGRES_TARGET_ENV` 可取 `local`、`dev`、`staging`、`production`。
- 生产或未知环境默认更保守，写操作会被限制或要求审批。
- CLI 展示连接信息时会自动脱敏密码。

当前项目不会把明文数据库连接串写入 LangGraph 状态。真正执行 PostgreSQL 工具时，服务端读取服务进程环境中的 `POSTGRES_TARGET_URL`；CLI 会从 `/agent/info` 获取服务端脱敏后的目标库信息，并用它构造本次会话的安全上下文。`--database-url` 主要用于本地 doctor 检查、没有服务端信息时的展示兜底，以及显式覆盖启动参数。

### 3. 启动服务

```bash
make dev
```

服务地址：

```text
http://127.0.0.1:2024
```

健康检查：

```bash
curl http://127.0.0.1:2024/health
```

LangGraph Studio：

```text
https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

## CLI 使用

像 Codex / Claude Code 一样，从目标工作目录直接启动：

```bash
cd /path/to/your/project
minidb-agent
```

首次启动时，如果还没有 PostgreSQL 目标配置，CLI 会提示输入数据库连接串和环境；配置会保存到 `~/.minidb_agent/config.json`，后续进入同一机器不需要重复输入。启动时 CLI 会自动启动或复用本地 LangGraph 服务，不需要先执行 `make dev`。

也可以一次性传入配置：

```bash
minidb-agent \
  --workspace "$PWD" \
  --database-url "$POSTGRES_TARGET_URL" \
  --target-env dev
```

如果只想使用外部已启动服务，可以显式指定 `--url`；如果不希望 CLI 自动启动本地服务，可以加 `--no-server`：

```bash
minidb-agent --url http://127.0.0.1:2024 --no-server
```

默认 CLI 只显示必要的连接摘要、最终回答、审批和错误信息；完整数据库连接卡片可用 `/db` 查看。需要查看内部规划、状态恢复、模型路由、交付包等调试事件时，使用：

```bash
minidb-agent --verbose
```

每次直接启动 `minidb-agent` 都会创建一个新会话，不会自动续接旧任务。需要恢复历史会话时，使用 `minidb-agent resume` 或在交互中输入 `/resume`；不带 id 时会显示历史会话列表，可用上下键选择、回车确认。`resume --last` 会直接恢复最近一次会话。

在交互提示符输入 `/` 会预览可用命令，继续输入会自动过滤，例如 `/do` 会提示 `/doctor`。

### 工作区

MiniDB Agent 的工作区由 CLI 进程的当前目录或 `--workspace` 指定。服务进程自己的 cwd 只代表服务从哪里启动，不代表本次 CLI 会话的目标工作区。

如果使用 Makefile，可以通过 `ARGS` 透传参数：

```bash
make -C /Users/nncc/code/agent_analyze/mini_agent cli \
  ARGS="--workspace /path/to/your/project --database-url $POSTGRES_TARGET_URL --target-env dev"
```

这样智能体看到的工作区就是目标目录，文件读写、artifact 输出和工作区安全策略都会围绕该目录构建。

### 切换 PostgreSQL 实例

在交互中输入：

```text
/reconnectdb
```

然后输入新的 PostgreSQL URL 和环境。CLI 会持久化新配置、重启它管理的本地服务，并自动开启一个新会话，后续工具会操作新的数据库实例。

也可以从命令行覆盖一次：

```bash
minidb-agent --database-url "postgresql://user:password@127.0.0.1:5432/anotherdb" --target-env staging
```

如果 CLI 摘要显示 `source=server`，说明会话使用的是服务端实际 PostgreSQL 目标；如果显示 `source=cli`，说明当前没有拿到服务端目标元数据，CLI 只能使用本地参数作为安全上下文兜底。

### 常用交互命令

```text
/help              查看帮助
/db                查看当前 PostgreSQL 连接卡片
/schema [table]    查看 schema 或表结构摘要
/tables            列出用户表
/plan              查看当前任务计划
/risk              查看当前风险和安全状态
/approvals         查看待审批和历史审批
/artifacts         查看交付报告、manifest 和 artifact
/doctor            诊断服务、数据库和工作区
/reconnectdb       重新配置并切换 PostgreSQL 实例
/readonly on|off   切换本 CLI 会话的只读请求
/cancel            中断当前运行中的任务
/sessions          查看本地会话索引
/resume <id>       恢复会话
/new               新建会话
/archive           归档当前会话索引
/quit              退出
```

## 非交互模式

`exec` 适合脚本、CI、巡检和一次性任务：

```bash
.venv/bin/python -m cli.main exec "检查当前数据库健康状态"
```

JSON 输出：

```bash
.venv/bin/python -m cli.main exec --json "检查 public.orders 表索引建议"
```

JSONL 事件流：

```bash
.venv/bin/python -m cli.main exec --jsonl --output-file report.jsonl "巡检当前数据库"
```

从 stdin 读取 prompt：

```bash
cat prompt.txt | .venv/bin/python -m cli.main exec -
```

## Doctor 诊断

检查服务、数据库连接、工作区和 artifact 目录：

```bash
.venv/bin/python -m cli.main doctor \
  --database-url "$POSTGRES_TARGET_URL" \
  --target-env dev
```

跳过服务或数据库连接检查：

```bash
.venv/bin/python -m cli.main doctor --skip-server --skip-database
```

## PostgreSQL 工具能力

当前内置 PostgreSQL 工具包括：

- `postgres_connection_check`
- `postgres_sql_classify`
- `postgres_list_schemas`
- `postgres_list_objects`
- `postgres_object_detail`
- `postgres_query_readonly`
- `postgres_explain`
- `postgres_top_queries`
- `postgres_health_check`
- `postgres_lock_inspect`
- `postgres_index_advisor`
- `postgres_hypothetical_index_test`
- `postgres_dry_run`
- `postgres_execute_write`
- `postgres_analyze_table`
- `postgres_vacuum_table`
- `postgres_create_index_concurrently`
- `postgres_extension_check`

只读工具会使用只读事务和结果限制。写工具必须经过 SQL 安全检查、审批、SQL hash 绑定、风险说明、回滚说明和验证条件。

## 安全策略

MiniDB Agent 对数据库任务采用保守策略：

- 未知或生产环境默认更严格。
- 自由 SQL 会先分类风险。
- 只读查询使用 `BEGIN TRANSACTION READ ONLY`。
- 写操作必须绑定审批记录和 SQL hash。
- 高风险 SQL 需要影响说明、回滚说明和验证标准。
- CLI、报告和 JSON 输出都会脱敏连接串、密码、token 和 API key。
- shell 数据库客户端默认被执行环境策略阻断，避免绕过数据库工具的安全控制。

## 产物交付

任务结束后，系统会生成交付包：

- 用户可读最终报告
- 审计报告
- artifact manifest
- SQL delivery items
- 证据引用
- 质量门检查结果
- 下一步建议

CLI 中可用 `/artifacts` 查看交付路径。

## 常用 Make 命令

```bash
make install                     # 创建虚拟环境并安装依赖
make dev                         # 启动 LangGraph 开发服务
make cli                         # 启动交互式 CLI
make cli ARGS="--workspace $PWD" # 启动 CLI 并透传参数
make test                        # 运行测试
make reset-db                    # 删除本地 SQLite 数据
make clean                       # 清理虚拟环境、缓存和数据
make docker-up                   # Docker Compose 启动
make docker-down                 # Docker Compose 停止
```

## 项目结构

```text
mini_agent/
├── agent/                  # LangGraph 节点、状态和路由
├── cli/                    # 交互式 CLI、exec、doctor、会话索引
├── collaboration/          # 人机协作对象和事件
├── delegation/             # 多智能体与任务委派
├── delivery/               # 交付包、报告和 manifest
├── docs/                   # 模块设计文档
├── error_handling/         # 错误分类、自我修复和恢复
├── execution/              # 工作区、数据库环境和 artifact 管理
├── memory/                 # 短期、工作、长期记忆
├── models/                 # 模型抽象与路由
├── quality/                # 评估、质量门和报告
├── safety/                 # 安全护栏与权限控制
├── server/                 # FastAPI app 和认证中间件
├── state_management/       # 状态迁移、校验和恢复
├── tools/                  # 工具注册、策略和 PostgreSQL 工具
├── tests/                  # 单元测试和回归测试
├── langgraph.json          # LangGraph 服务配置
├── Makefile
└── README.md
```

## 测试

```bash
.venv/bin/python -m pytest -q
```

当前测试覆盖任务理解、规划、Agent Loop、上下文、记忆、工具、PostgreSQL 工具、安全、工作区、状态、协作、错误处理、质量、模型路由、交付和 CLI。

## 开发提示

- 新增模块设计文档放在 `docs/`。
- 新增工具优先放在 `tools/builtin/` 或 `tools/postgres/`。
- 数据库写工具必须接入安全策略、审批和验证。
- CLI 不应保存明文数据库连接串。
- 变更后至少运行：

```bash
.venv/bin/python -m compileall agent cli tools tests
.venv/bin/python -m pytest -q
```

## License

MIT
