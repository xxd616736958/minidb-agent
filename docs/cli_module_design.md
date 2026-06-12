# PostgreSQL 管理智能体：CLI 模块设计

## 1. 背景

mini-agent 已经具备 PostgreSQL 管理智能体的核心后端能力：任务理解、规划、Agent Loop、上下文管理、记忆、工具、安全护栏、审批、执行环境、状态管理、质量控制和产物交付。

但 CLI 仍然偏“终端聊天客户端”：

```text
用户输入自然语言
  -> CLI 发送到 LangGraph Server
  -> CLI 按节点打印部分状态
  -> 遇到审批时弹出通用确认
  -> 最后显示模型或节点输出
```

对普通问答来说，这已经能工作；但对 PostgreSQL 管理智能体来说，CLI 需要承担更强的“操作驾驶舱”职责。用户必须在终端里清楚知道：

- 当前连接的是哪个 PostgreSQL 实例和数据库。
- 当前处于只读模式、可写模式还是强审批模式。
- 当前任务理解、计划、风险和审批状态是什么。
- Agent 正在执行哪个数据库阶段：观察、诊断、生成 SQL、审批、执行、验证、交付。
- 写操作会影响什么对象、风险是什么、是否有回滚和验证。
- 最终报告、SQL 包、审计 manifest 在哪里。

因此，CLI 模块不应只是输入输出壳，而应成为用户与数据库智能体协作的控制面。

## 2. 模块定位

CLI 模块位于用户终端和 Agent Server 之间：

```text
User Terminal
  -> CLI
       -> 参数解析 / 配置加载
       -> 数据库连接上下文展示
       -> 交互式 REPL
       -> 非交互 exec
       -> slash commands
       -> 流式事件渲染
       -> SQL 审批 UI
       -> 会话管理
       -> 产物展示
       -> 机器可读输出
  -> LangGraph Server
       -> Agent Loop
       -> Tools
       -> Safety / Approval
       -> Delivery
```

CLI 不负责数据库推理，也不直接绕过 Agent 调工具。CLI 的核心职责是：

1. 把用户输入、启动参数、会话选择和审批决策转成 Agent 可消费的结构化输入。
2. 把 Agent 的结构化状态、流式事件、风险判断、审批请求和交付产物转成用户可理解的终端界面。
3. 在交互模式和非交互模式之间提供一致的行为、权限和输出语义。

## 3. 设计目标

1. 同时支持交互式数据库协作和非交互自动化执行。
2. 启动时明确数据库连接、目标环境、权限模式和工作区。
3. 用数据库任务阶段替代内部节点名，降低用户理解成本。
4. 提供数据库专用 slash commands，而不是只提供通用聊天命令。
5. 让 SQL 写操作审批具备 DBA 所需信息：SQL、风险、影响、回滚、验证、目标环境。
6. 支持只读、安全审批、强审批和会话级临时授权。
7. 支持 human、json、jsonl 输出，方便人类和自动化系统使用。
8. 支持按数据库、环境和工作区维度管理会话。
9. 支持最终产物、报告、manifest、SQL 交付项的终端展示。
10. 保持 CLI 轻量：不把任务理解、规划、安全判断和数据库执行逻辑搬进 CLI。

## 4. 非目标

1. 不在 CLI 中实现新的数据库执行引擎。
2. 不在 CLI 中硬编码复杂意图识别规则。
3. 不让 CLI 绕过安全护栏直接执行写 SQL。
4. 不在第一阶段实现完整 TUI 框架或 Web UI。
5. 不把所有原始数据库结果直接刷到终端。
6. 不在第一阶段实现企业级凭据管理系统，先对接环境变量、配置文件和已有服务端配置。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前 mini-agent CLI 已有以下基础：

- `cli/main.py`：支持 `--url`、`--api-key`、`--resume`、`--new`、`--log-level`，并在启动时检查 server health。
- `cli/repl.py`：支持交互式输入、history、session、resume、plan、clear、help。
- `cli/display.py`：已经能展示任务理解、任务卡、数据库计划、计划 review、循环状态、工具调用、工具结果、审批和交付包。
- `cli/approval.py`：已有通用危险工具审批 UI，支持 approve、reject、edit、edit_and_rerun。
- `cli/history.py`：已有会话历史查询基础。
- 后端状态中已有 `DBTaskIntent`、`DBTaskPlan`、`ApprovalDecision`、`SQLSafetyReport`、`DeliveryPackage`、`ArtifactManifest` 等结构。

### 5.2 主要不足

1. CLI 启动参数还没有围绕 PostgreSQL 管理场景设计，缺少 `database_url`、目标环境、只读模式、审批模式和输出模式。
2. 当前 CLI 主要是交互式客户端，缺少稳定的非交互 `exec` 模式。
3. 流式输出仍然偏节点名，用户需要理解 `task_planner`、`tool_policy_gate`、`normalize_observation` 等内部概念。
4. slash commands 还偏通用，缺少数据库专用命令。
5. 审批 UI 还是通用工具审批，不够 SQL 化。
6. 会话恢复只偏 thread_id，缺少按项目、数据库、环境组织的会话索引。
7. 输出格式以 human 为主，缺少 json/jsonl 事件流。
8. 最终交付物虽然后端已经生成，但 CLI 还需要更明确地把报告、manifest、SQL 交付项作为“一次任务交付包”展示。

## 6. 设计点详解

### 6.1 交互模式与非交互 exec 模式分离

**Codex 的设计方案**

Codex 把默认交互式 CLI 和 `codex exec` 分成两条入口。默认入口进入 TUI/交互体验，`exec` 面向脚本化和自动化，支持 prompt 参数、stdin 输入、JSON/JSONL 输出、最后消息写文件、resume、review 等能力。这样做的核心是把“人类协作”和“自动化执行”的输出协议分开，避免脚本依赖易变的人类界面。

**Claude 的设计方案**

Claude Code 也区分交互式 REPL、非交互调用、远程 session、SDK/print 场景。交互式路径强调终端体验、权限弹窗、输入历史和上下文展示；非交互路径更强调稳定输出、会话恢复和机器集成。

**权衡后的最终方案**

mini-agent 应保留当前 `python -m cli.main` 的交互式 REPL，同时新增 `exec` 子命令。交互模式用于多轮数据库诊断、计划确认和审批；非交互模式用于 CI、巡检、脚本和定时任务。两者共用同一套配置解析、权限策略和事件模型，但输出层分离：交互模式用 Rich 渲染，exec 模式默认简洁文本，可选 `--json` 或 `--jsonl`。这样既继承 Codex 的自动化能力，又保留 Claude 式强交互体验。

推荐命令：

```bash
minidb-agent
minidb-agent exec "分析最近慢 SQL"
minidb-agent exec --json "检查 public.orders 表索引建议"
minidb-agent exec --jsonl --output report.jsonl "巡检当前数据库"
```

### 6.2 PostgreSQL 场景化启动参数

**Codex 的设计方案**

Codex 的 CLI 通过共享参数体系统一处理模型、sandbox、approval、profile、cwd、config override 等选项，并把这些选项传给交互和非交互入口。它的核心思想是：运行边界必须在入口处声明，而不是等模型运行后再猜。

**Claude 的设计方案**

Claude 在启动时会集中加载配置、权限模式、工作目录、模型、session、feature gate、远程控制、MCP 等上下文，并在进入 REPL 前完成大量环境初始化。它强调启动阶段把“本次会话的能力边界”确定下来。

**权衡后的最终方案**

mini-agent 的 CLI 参数应围绕数据库管理重新设计。保留 `--url`、`--api-key`、`--resume`、`--new`，新增：

```text
--database-url / --db-profile
--target-env dev|test|staging|prod
--readonly
--approval-mode auto-readonly|on-write|always|never
--workspace DIR
--profile NAME
--output human|json|jsonl
--output-file FILE
--no-save-session
```

这些参数不替代后端安全策略，而是作为会话输入传给 Agent。最终效果是用户一启动就明确“连哪里、以什么权限、输出给谁、是否允许写”，降低误连生产库和误执行写操作的风险。

### 6.3 启动首屏数据库连接卡片

**Codex 的设计方案**

Codex 会显式呈现运行模式、sandbox、approval、cwd 等影响行为的上下文。它不是把这些信息藏在配置里，而是让用户在操作前知道本轮权限边界。

**Claude 的设计方案**

Claude REPL 启动时会组织项目上下文、session、权限模式、工作目录和可用能力，并用终端 UI 向用户展示关键状态。它强调用户进入会话后立刻知道当前环境。

**权衡后的最终方案**

mini-agent 启动后应展示数据库连接卡片，内容包括：

- server URL
- thread/session id
- target environment
- database host、port、database、user
- readonly / writable
- approval mode
- workspace
- artifact directory

必须脱敏 password、token、完整连接串。生产环境应使用更醒目的风险提示。这样做的效果是把数据库智能体最关键的上下文放在用户眼前，避免“以为是测试库，实际是生产库”的错误。

### 6.4 数据库专用 slash commands

**Codex 的设计方案**

Codex 在顶层 CLI 提供 `resume`、`fork`、`archive`、`doctor`、`mcp`、`plugin`、`sandbox` 等面向工作流的命令，交互式 TUI 中也有 slash commands。它的命令设计不是围绕模型能力，而是围绕用户工作流。

**Claude 的设计方案**

Claude Code 有丰富的命令系统，支持会话、配置、权限、MCP、远程控制、上下文切换等操作。它把很多“控制动作”从自然语言中分离出来，避免用户每次都靠 prompt 控制系统。

**权衡后的最终方案**

mini-agent 应把 REPL 命令分为通用命令和数据库命令：

```text
/help
/info
/sessions
/resume <id>
/new
/plan
/risk
/approvals
/artifacts
/db
/schema [table]
/tables
/explain <sql>
/readonly on|off
/doctor
```

这些命令不应绕过 Agent 直接做复杂业务，而是调用安全的 server API 或发送结构化控制输入。最终效果是高频控制操作变得可预测，用户不用把“显示当前数据库连接”“查看待审批 SQL”这类动作写成长 prompt。

### 6.5 阶段化流式事件渲染

**Codex 的设计方案**

Codex exec 的事件处理器会把内部线程事件映射为稳定的事件类型，例如 turn started、item started、tool call、command execution、file change、turn completed、error，并支持 JSONL 输出。这让 UI 和脚本不用依赖内部实现细节。

**Claude 的设计方案**

Claude 的 REPL 消息适配层会把 SDK/远程 session 消息转成 REPL 可渲染的消息类型，并对未知事件做容错处理。它强调 UI 消费的是稳定消息协议，而不是后端任意对象。

**权衡后的最终方案**

mini-agent CLI 应新增一层 `CliEventAdapter`，把 LangGraph 节点更新映射为数据库任务阶段：

```text
task_understanding
plan_ready
safety_check
approval_required
tool_running
observation_ready
verification_ready
delivery_ready
blocked
error
```

交互模式按阶段渲染，jsonl 模式逐行输出结构化事件。这样能隐藏 `intent_validator`、`tool_policy_gate`、`normalize_observation` 等内部节点名，让用户看到的是“计划已生成”“SQL 安全检查通过”“等待审批”“最终报告已生成”。

### 6.6 SQL 专用审批 UI

**Codex 的设计方案**

Codex 的 approval/sandbox 体系把危险操作拦在执行前，并允许用户批准或拒绝。它还支持按规则减少重复确认，但危险操作仍需要明确边界。

**Claude 的设计方案**

Claude 的权限 UI 会根据工具类型给用户不同选项，例如文件权限、工具权限、workspace 目录授权、session 级授权，并能让用户给出拒绝反馈或修改输入。它不是一个通用 yes/no 弹窗，而是按场景组织审批信息。

**权衡后的最终方案**

mini-agent 的审批 UI 应从通用工具参数展示升级为 SQL 审批卡。审批卡字段：

```text
approval_id
target_environment
database
schema/table
sql_preview
sql_hash
classification
risk_level
impact_summary
estimated_rows
transaction_mode
rollback_summary
verification_criteria
options
```

可选动作：

```text
approve
reject
edit_sql
explain_more
dry_run_more
report_only
abort
```

最终效果是用户审批数据库写操作时能看到真正影响决策的信息，而不是只看到一个工具名和 JSON 参数。

### 6.7 权限模式与会话级临时授权

**Codex 的设计方案**

Codex 支持 approval policy、sandbox mode、配置 profile 和 prefix rule。它允许低风险操作减少重复确认，同时把高风险操作放到审批或沙箱边界内。

**Claude 的设计方案**

Claude 支持 permission mode、session-level permission、workspace directory permission 和可持久化/临时化的权限规则。它的重点是区分“本次会话允许”和“长期记住”。

**权衡后的最终方案**

mini-agent 应提供数据库化权限模式：

```text
readonly: 只允许 SELECT/EXPLAIN/元数据读取
auto-readonly: 只读自动执行，写操作进入审批
on-write: 写操作审批，只读可自动
always: 所有数据库工具调用都需要确认
never: 非交互专用，只允许安全策略判定为可自动的动作
```

会话级临时授权只能用于低风险只读操作，例如本会话内允许 `EXPLAIN SELECT` 或读取指定 schema 元数据；不允许永久自动批准 `UPDATE`、`DELETE`、`DDL`。最终效果是在安全和效率之间取得平衡。

### 6.8 `doctor` 诊断命令

**Codex 的设计方案**

Codex 有 `doctor` 命令，用来诊断安装、配置、认证、运行环境和本地状态。它把“为什么不能正常运行”的问题前置成一个可执行检查。

**Claude 的设计方案**

Claude 在启动流程中会加载和校验配置、权限、认证、MCP、工作目录、模型能力等，并在异常时给用户较具体的错误信息或引导。

**权衡后的最终方案**

mini-agent 应提供 `minidb-agent doctor` 和 REPL 内 `/doctor`。检查项包括：

- Agent server health
- API key 是否有效
- PostgreSQL 是否可连接
- 当前账号权限
- readonly transaction 是否可用
- `POSTGRES_TARGET_URL` / `DATABASE_URL` 是否配置
- 目标环境是否为 prod
- workspace 是否可写
- artifact 目录是否可写
- 必要 Python 依赖是否存在

最终效果是把“连不上库、权限不足、服务没起、目录不可写”这类问题快速定位，而不是让用户从 Agent 报错里猜。

### 6.9 human/json/jsonl 输出协议

**Codex 的设计方案**

Codex exec 支持人类输出和 JSONL 输出，并能把最后消息写入文件。JSONL 事件流让脚本、CI 和外部系统可以稳定消费 Agent 过程。

**Claude 的设计方案**

Claude SDK 和远程 session 使用结构化消息流，把 assistant message、tool use、permission request、result 等事件标准化，再交给 REPL 或其他客户端渲染。

**权衡后的最终方案**

mini-agent 输出应分三类：

```text
human: Rich 表格、面板、Markdown，适合终端阅读
json: 最终结果单个 JSON，适合脚本读取最终交付
jsonl: 每个阶段一行事件，适合 CI、监控和审计系统
```

JSON/JSONL 不输出 Rich 样式，不输出未脱敏密钥，不依赖内部节点名。最终效果是同一个数据库智能体既能给人用，也能给自动化平台用。

### 6.10 会话索引与恢复

**Codex 的设计方案**

Codex 支持 resume、fork、archive、unarchive，并能按最近会话恢复。它把 session 当成一等对象，而不是只保存一个最后 ID。

**Claude 的设计方案**

Claude 有 session discovery、session history、resume chooser、远程 session 管理和 session pointer。它能围绕项目目录、远程环境和会话元数据组织恢复入口。

**权衡后的最终方案**

mini-agent 应从单个 `~/.zuixiaoagent_session` 升级为会话索引文件，例如：

```json
{
  "sessions": [
    {
      "thread_id": "...",
      "title": "orders 慢查询诊断",
      "project_dir": "...",
      "database_fingerprint": "...",
      "target_environment": "prod",
      "last_intent": "performance_diagnosis",
      "last_status": "completed",
      "updated_at": "..."
    }
  ]
}
```

CLI 支持 `/sessions`、`/resume`、`/archive`、`/new`。数据库 fingerprint 必须脱敏，只用于区分实例。最终效果是用户可以安全恢复“某个库的某个问题”，而不是误接上另一个数据库任务。

### 6.11 产物交付展示

**Codex 的设计方案**

Codex 在 exec 事件中会区分 final message、file change、tool result、turn completed，并支持把最后消息写到文件。它让“任务产物”从聊天过程里分离出来。

**Claude 的设计方案**

Claude REPL 会把工具结果、最终消息、权限事件和远程 session 状态转成用户可见消息，并在会话历史中保留可恢复的上下文。

**权衡后的最终方案**

mini-agent 已有 `DeliveryPackage`、`ArtifactManifest` 和 `final_report` 节点。CLI 应在任务结束时统一展示：

```text
Delivery status
User report path
Audit report path
Manifest path
SQL delivery items
Quality gate status
Next actions
```

并提供 `/artifacts` 重新查看最近交付包。最终效果是数据库任务结束后，用户拿到的是可交付报告和审计材料，而不是散落在终端历史中的几段文字。

### 6.12 中断、取消与恢复语义

**Codex 的设计方案**

Codex 支持恢复已有 session，非交互和交互任务都围绕 session 状态推进。它强调任务中断后可以通过 session 继续，而不是从零开始。

**Claude 的设计方案**

Claude 远程 session 管理支持 interrupt，并能处理 WebSocket 断连、session not found 重试、控制请求取消等情况。它把“中断当前请求”和“退出整个会话”区分开。

**权衡后的最终方案**

mini-agent CLI 应定义清晰行为：

- Ctrl+C 第一次：取消当前 run，但保留 session。
- Ctrl+C 第二次：退出 CLI。
- `/cancel`：请求 server 中断当前任务。
- `/resume`：恢复已有 thread。
- 中断后展示当前计划、待审批、已生成 artifact 和下一步建议。

最终效果是长时间数据库诊断、慢查询分析或等待审批时，用户可以安全中断，不会丢失已完成的观察和交付材料。

### 6.13 CLI 保持薄控制面，不承载核心智能逻辑

**Codex 的设计方案**

Codex CLI 负责参数解析、配置加载、TUI/exec 入口、事件处理、登录、诊断和沙箱命令；核心推理、工具执行、审批策略在 core/protocol/execpolicy 等模块内。CLI 是控制面，不是智能体大脑。

**Claude 的设计方案**

Claude CLI/REPL 负责启动、渲染、输入、权限交互、session 管理和远程连接；任务执行、工具、权限规则、模型调用和上下文构造分布在独立服务和模块中。UI 不直接承担推理逻辑。

**权衡后的最终方案**

mini-agent CLI 只做四类事：

1. 采集输入：命令参数、自然语言、slash command、审批决策。
2. 展示状态：任务阶段、计划、风险、工具结果、交付物。
3. 管理会话：新建、恢复、归档、历史。
4. 输出结果：human/json/jsonl、文件路径、报告入口。

任务理解、规划、安全判断、SQL 分析、工具执行、记忆和交付生成仍在后端模块。最终效果是 CLI 可维护、可测试、可替换，将来新增 Web UI 或 API 客户端也能复用同一套后端协议。

## 7. 推荐核心对象

### 7.1 CliRuntimeConfig

```python
class CliRuntimeConfig(TypedDict):
    server_url: str
    api_key: Optional[str]
    database_url: Optional[str]
    db_profile: Optional[str]
    target_environment: Literal["dev", "test", "staging", "prod", "unknown"]
    readonly: bool
    approval_mode: Literal["auto-readonly", "on-write", "always", "never"]
    workspace: str
    output_mode: Literal["human", "json", "jsonl"]
    output_file: Optional[str]
    save_session: bool
```

### 7.2 DbConnectionCard

```python
class DbConnectionCard(TypedDict):
    host: str
    port: int
    database: str
    user: str
    target_environment: str
    readonly: bool
    approval_mode: str
    fingerprint: str
    display_url: str
```

`display_url` 必须脱敏，`fingerprint` 用于会话索引，不存储明文密码。

### 7.3 CliSessionRecord

```python
class CliSessionRecord(TypedDict):
    thread_id: str
    title: str
    project_dir: str
    database_fingerprint: Optional[str]
    target_environment: str
    last_intent: Optional[str]
    last_status: str
    artifact_paths: list[str]
    created_at: str
    updated_at: str
    archived: bool
```

### 7.4 CliEvent

```python
class CliEvent(TypedDict):
    type: Literal[
        "task_understanding",
        "plan_ready",
        "safety_check",
        "approval_required",
        "tool_running",
        "observation_ready",
        "verification_ready",
        "delivery_ready",
        "blocked",
        "error",
    ]
    thread_id: str
    run_id: Optional[str]
    summary: str
    payload: dict[str, Any]
    created_at: str
```

### 7.5 SqlApprovalPrompt

```python
class SqlApprovalPrompt(TypedDict):
    approval_id: str
    target_environment: str
    database: Optional[str]
    sql_preview: str
    sql_hash: str
    classification: str
    risk_level: str
    impact_summary: Optional[str]
    rollback_summary: Optional[str]
    verification_criteria: list[str]
    choices: list[str]
```

## 8. 推荐命令体系

### 8.1 顶层命令

```text
minidb-agent
minidb-agent exec [OPTIONS] [PROMPT]
minidb-agent doctor
minidb-agent sessions
minidb-agent resume <thread_id>
minidb-agent config show
```

第一阶段优先实现：

1. `exec`
2. `doctor`
3. `sessions`
4. `resume`

### 8.2 REPL 命令

```text
/help
/info
/new
/resume <thread_id>
/sessions
/plan
/risk
/approvals
/artifacts
/db
/schema [table]
/tables
/doctor
/readonly on|off
/cancel
/clear
/quit
```

## 9. 推荐事件渲染规则

| Agent 输出来源 | CLI 阶段 | human 渲染 | json/jsonl 渲染 |
| --- | --- | --- | --- |
| `current_intent` / `task_card` | `task_understanding` | 任务卡表格 | 结构化 intent |
| `db_task_plan` | `plan_ready` | 步骤表格 | plan JSON |
| `sql_safety_reports` | `safety_check` | 风险卡 | safety report JSON |
| `pending_approval` | `approval_required` | SQL 审批卡 | approval JSON |
| `tool_invocation_records` | `tool_running` | 工具执行摘要 | tool event JSON |
| `db_observations` | `observation_ready` | 观察摘要 | observation JSON |
| `verification_results` | `verification_ready` | 验证结果 | verification JSON |
| `delivery_packages` | `delivery_ready` | 交付包路径 | delivery JSON |
| `error_reports` | `blocked/error` | 阻塞原因和下一步 | error JSON |

## 10. 安全与脱敏要求

1. CLI 永远不打印数据库密码、API key、完整连接串。
2. 生产环境连接卡必须明确显示 `target_environment=prod`。
3. 非交互模式下，如果 `approval_mode=never`，只能执行安全策略允许自动执行的只读任务。
4. JSON/JSONL 输出也必须脱敏，不能因为机器可读就输出秘密。
5. SQL 审批卡可以显示 SQL preview，但长 SQL 要截断并使用 artifact 引用。
6. 任何 write/DDL/maintenance SQL 的 approve 决策都要带上 `sql_hash`，避免用户批准 A、实际执行 B。
7. CLI 不缓存明文 `database_url`；如需保存 profile，只保存 profile 名和脱敏 fingerprint。

## 11. 实施计划

### 第一阶段：CLI 协议与展示升级

1. 新增 CLI 配置对象和数据库连接卡。
2. 新增 PostgreSQL 场景化启动参数。
3. 新增 `CliEventAdapter`，把节点输出映射为阶段事件。
4. 改造 `display.py`，增加数据库连接卡、SQL 审批卡、交付包详情。
5. 扩展 `/db`、`/risk`、`/approvals`、`/artifacts`、`/doctor`。

### 第二阶段：非交互 exec

1. 增加 `exec` 子命令。
2. 支持 prompt 参数、stdin 输入、`--json`、`--jsonl`、`--output-file`。
3. 统一 exit code：成功为 0，安全阻塞/审批缺失/执行失败使用不同错误码。
4. 支持最后交付包输出。

### 第三阶段：会话索引与恢复

1. 从单个 session 文件升级为 session index。
2. `/sessions` 显示数据库、环境、标题、更新时间和状态。
3. 支持 archive/resume。
4. 支持按当前 workspace 和 database fingerprint 过滤会话。

### 第四阶段：权限体验增强

1. 支持会话级只读授权。
2. 支持拒绝原因和重新规划输入。
3. 支持审批卡中的 `dry_run_more`、`explain_more`、`report_only`。

## 12. 测试策略

1. 参数解析测试：覆盖交互模式、exec 模式、数据库参数、输出参数和冲突参数。
2. 事件适配测试：给定节点输出，验证 CLI 事件类型稳定。
3. 脱敏测试：确保连接串、password、token 不会出现在 human/json/jsonl 输出。
4. SQL 审批测试：验证审批卡包含 sql_hash、risk、rollback、verification。
5. 非交互输出测试：验证 json/jsonl 可解析，且 exit code 正确。
6. 会话索引测试：验证保存、恢复、归档、按数据库过滤。
7. doctor 测试：用 mock server 和 mock PostgreSQL 检查诊断结果。

## 13. 最终权衡总结

Codex 给 mini-agent 的主要参考价值是：清晰的多子命令结构、交互/非交互分离、共享配置参数、JSONL 机器输出、doctor 诊断、resume/archive/fork 等 session 管理，以及 approval/sandbox 入口边界。

Claude 给 mini-agent 的主要参考价值是：强 REPL 体验、丰富 slash commands、权限 UI 场景化、session discovery/history、远程 session 中断恢复、启动阶段集中加载上下文和权限模式。

最终方案不完全照搬任何一方。mini-agent 的定位是 PostgreSQL 管理智能体，所以 CLI 的核心不是“代码编辑体验”，而是“数据库操作驾驶舱”。因此最终设计选择：

- 用 Codex 的命令分层和 exec 输出协议解决自动化问题。
- 用 Claude 的交互式权限体验和会话恢复思路解决人机协作问题。
- 用 PostgreSQL 场景化的连接卡、SQL 审批卡、数据库 slash commands 和交付包展示体现 mini-agent 的领域特性。

这样可以让 CLI 从普通聊天入口升级为可用于真实数据库管理任务的终端控制面。
