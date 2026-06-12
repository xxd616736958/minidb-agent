# PostgreSQL 管理智能体：执行环境与工作区管理模块设计

## 1. 背景

mini-agent 已经具备任务理解、规划、上下文管理、记忆系统、工具注册与调用、具体 PostgreSQL 工具等能力。随着系统从通用终端 Agent 转向 PostgreSQL 管理智能体，执行环境与工作区管理会成为安全性和可恢复性的核心。

当前系统至少同时面对三类环境：

```text
代码/文档工作区
  -> 仓库文件、报告文件、SQL 草稿、测试脚本

PostgreSQL 目标环境
  -> local / dev / staging / production 数据库

任务运行环境
  -> 本次任务的临时产物、工具结果、审批证据、执行日志
```

如果这些环境没有统一建模，Agent 很容易出现以下问题：

1. 不知道当前任务连接的是哪个数据库环境。
2. 把生产库当作本地库处理。
3. 用 shell 绕过 PostgreSQL 专用工具和 SQL 安全策略。
4. 把 SQL 草稿、报告、日志随意写入工作区。
5. 长任务中断后无法恢复执行轨迹。
6. 工具结果、审批、验证证据分散在消息文本里，难以审计。

因此，本模块的核心目标是：让 mini-agent 明确知道“我在哪个文件工作区工作、我连接的是哪个数据库、我当前任务产生了哪些产物、哪些操作允许执行、哪些操作必须审批、任务中断后如何恢复”。

## 2. 模块定位

执行环境与工作区管理模块位于 Agent Loop、工具系统、安全护栏、上下文管理和人机协作之间。

推荐关系：

```text
User Request
  -> Task Understanding / DBTaskIntent
  -> DBTaskPlan / TaskStep
  -> ExecutionEnvironmentManager
       -> WorkspaceManager
       -> DatabaseEnvironmentManager
       -> TaskWorkspace
       -> ArtifactStore
       -> RuntimePolicy
  -> ToolCatalog / ToolCallPolicyGate
  -> Tool Execution
  -> ToolInvocationRecord / DBObservation / VerificationResult / Report
```

核心思想是：执行环境不是工具自己的局部变量，而是 Agent 状态的一部分；每一次工具调用都必须知道它运行在哪个工作区、哪个数据库环境、哪个任务步骤和哪套权限策略下。

## 3. 设计目标

1. 将文件工作区、数据库目标环境、任务运行环境分层建模。
2. 对文件读写、报告生成、SQL 草稿、工具日志建立明确边界。
3. 对 PostgreSQL 环境建立脱敏画像和风险级别。
4. 根据环境和任务阶段动态约束工具可见性和执行权限。
5. 禁止通过 shell/psql 绕过 PostgreSQL 专用工具链。
6. 为每次任务创建独立 `TaskWorkspace`，保存产物、证据和执行轨迹。
7. 所有执行产物按生命周期分类：临时、会话级、持久。
8. 支持中断、恢复、超时、取消和失败后复盘。
9. 确保连接串、密码、敏感查询结果不会进入 prompt、日志或长期记忆。

## 4. 非目标

1. 不重新实现容器沙箱或操作系统级虚拟化。
2. 不把 mini-agent 改造成完整 CI/CD 平台。
3. 不让 shell 成为数据库管理的主入口。
4. 不在本模块中重新设计所有工具实现细节。
5. 不将所有原始工具输出永久保存。

## 5. 当前 mini-agent 的主要问题

### 5.1 工作区边界还不够明确

当前文件工具可以读写路径，但系统没有统一的 `WorkspaceProfile` 来描述工作区根目录、允许写入目录、报告目录、临时目录和产物目录。

### 5.2 数据库环境只有连接串，没有环境画像

当前 PostgreSQL 工具可以通过环境变量连接数据库，但状态中还没有明确保存 target environment、是否生产、默认访问模式、超时策略、只读/写入权限等信息。

### 5.3 任务产物没有独立工作区

EXPLAIN 结果、SQL 草稿、健康检查报告、审批证据、执行日志还没有统一的 `TaskWorkspace` 和 `ArtifactStore` 管理。

### 5.4 shell 与数据库工具边界还需要加强

shell 工具仍可能执行 `psql -c` 这类命令，从而绕过 SQL classifier、只读事务、审批和审计。

### 5.5 中断恢复依赖消息历史，不够结构化

长任务中断后，系统需要能从状态中恢复当前工作区、数据库环境、已生成产物、已审批操作和验证结果，而不是只靠消息文本推断。

## 6. 核心数据结构

### 6.1 WorkspaceProfile

```python
class WorkspaceProfile(TypedDict):
    root_path: str
    read_allowed_paths: list[str]
    write_allowed_paths: list[str]
    artifact_root: str
    report_root: str
    temp_root: str
    default_cwd: str
    git_repo: Optional[str]
    dirty_state_known: bool
```

### 6.2 DatabaseEnvironmentProfile

```python
class DatabaseEnvironmentProfile(TypedDict):
    environment_name: Literal["local", "dev", "staging", "production", "unknown"]
    target_database: Optional[str]
    safe_host_label: Optional[str]
    safe_user_label: Optional[str]
    access_mode: Literal["read_only", "diagnostic", "write_after_approval", "admin_maintenance"]
    is_production: bool
    default_statement_timeout_ms: int
    default_lock_timeout_ms: int
    max_result_rows: int
    allow_write_tools: bool
    require_backup_check_for_writes: bool
    credential_ref: str
```

### 6.3 TaskWorkspace

```python
class TaskWorkspace(TypedDict):
    task_id: str
    intent_id: Optional[str]
    plan_id: Optional[str]
    root_path: str
    artifact_ids: list[str]
    report_paths: list[str]
    sql_draft_paths: list[str]
    execution_log_ref: Optional[str]
    created_at: str
    updated_at: str
```

### 6.4 ArtifactRecord

```python
class ArtifactRecord(TypedDict):
    id: str
    task_id: str
    kind: Literal[
        "sql_draft",
        "explain_json",
        "health_report",
        "query_result_digest",
        "approval_snapshot",
        "execution_log",
        "verification_evidence",
        "final_report",
    ]
    path: Optional[str]
    payload_ref: Optional[str]
    summary: str
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    lifecycle: Literal["ephemeral", "session", "persistent"]
    created_at: str
```

### 6.5 RuntimePolicy

```python
class RuntimePolicy(TypedDict):
    allow_shell_database_clients: bool
    allow_network_tools: bool
    allow_file_writes: bool
    allow_database_writes: bool
    require_approval_for_workspace_write: bool
    require_approval_for_database_write: bool
    max_tool_duration_seconds: int
    max_artifact_size_bytes: int
```

## 7. 设计点与参考权衡

### 7.1 设计点一：将执行环境拆成文件工作区、数据库环境、任务运行环境三层

**Codex 的设计方案**

Codex 的执行模型非常强调工作区、当前目录、命令执行环境、sandbox 和审批策略。它不会把所有工具调用都视为无上下文动作，而是围绕当前会话、工作区和权限配置执行。

**Claude 的设计方案**

Claude Code 更强调项目上下文和工具权限上下文。Read、Write、Edit、Bash 等工具都运行在一个受用户会话和权限规则约束的项目环境里，用户交互也围绕当前项目展开。

**最终权衡方案**

mini-agent 将执行环境拆成三层：`WorkspaceProfile` 管文件边界，`DatabaseEnvironmentProfile` 管 PostgreSQL 目标环境，`TaskWorkspace` 管本次任务产物和证据。三者都进入 AgentState，而不是散落在工具参数中。

**达成效果**

Agent 能明确区分“我要修改项目文件”“我要查询 staging 数据库”“我要保存本次诊断报告”。这会显著降低误写文件、误连生产库和任务证据丢失的风险。

### 7.2 设计点二：文件工作区必须有根目录和写入边界

**Codex 的设计方案**

Codex 默认围绕一个工作目录操作，文件读写和命令执行都与工作区相关。它强调不要越界修改用户没有要求的文件，也不会随意回滚用户已有改动。

**Claude 的设计方案**

Claude 将文件读取和写入拆成不同工具，并通过权限提示和工具语义区分只读、编辑和写入。用户可以更清楚地看到什么时候只是读文件，什么时候会修改项目。

**最终权衡方案**

mini-agent 引入 `WorkspaceManager`，明确 `root_path`、`read_allowed_paths`、`write_allowed_paths`、`artifact_root`、`report_root`、`temp_root`。所有文件工具和报告生成都必须先经过路径归一化，确认目标路径在允许边界内。

**达成效果**

Agent 可以安全生成 SQL 草稿、诊断报告和执行日志，但不会误写用户主目录、系统目录或仓库外文件。后续如果需要写项目代码，也能区分“报告产物写入”和“源代码修改”。

### 7.3 设计点三：数据库环境要有脱敏画像，而不是只保存连接串

**Codex 的设计方案**

Codex 的 sandbox 和 approval policy 会根据执行环境差异改变权限。高风险环境下的命令不能和普通读取动作一样处理。

**Claude 的设计方案**

Claude 的 permission context 会影响工具是否可用、是否需要用户确认、是否被 deny rule 拦截。工具权限不是只由工具名决定，也由上下文决定。

**最终权衡方案**

mini-agent 引入 `DatabaseEnvironmentProfile`，保存 `environment_name`、`target_database`、`safe_host_label`、`safe_user_label`、`access_mode`、`is_production`、超时、最大返回行数、是否允许写工具、是否写前需要备份检查。真实连接串只通过 `credential_ref` 指向配置层，不进入模型上下文。

**达成效果**

Agent 可以在 prompt 和报告中说“当前目标是 production/appdb”，但不会暴露密码。生产环境会自动提升风险，写入工具默认不可见，诊断工具默认限制行数和超时。

### 7.4 设计点四：数据库会话按只读、诊断、写入分级

**Codex 的设计方案**

Codex 对不同命令会选择不同 sandbox 和审批路径。普通读取、运行测试、危险命令并不共享同一执行权限。

**Claude 的设计方案**

Claude 的工具有 read-only、destructive 等风险语义。工具调用时会根据工具类型和输入内容决定是否需要权限检查。

**最终权衡方案**

mini-agent 在 `DatabaseEnvironmentManager` 中提供 `readonly_session`、`diagnostic_session`、`write_session`。只读会话强制 read-only transaction；诊断会话允许 EXPLAIN、pg_stat、lock inspect；写入会话必须绑定审批、SQL hash、回滚方案和验证标准。

**达成效果**

数据库工具的执行路径天然分级。观察阶段无法写库，诊断阶段不会默认提交变更，写入阶段必须可审计、可验证。

### 7.5 设计点五：每个任务创建独立 TaskWorkspace

**Codex 的设计方案**

Codex 的执行轨迹可以从工具调用、输出和状态中恢复。对于复杂任务，保留每一步证据对调试和继续执行很重要。

**Claude 的设计方案**

Claude 的协作体验强调用户能看到工具使用过程和结果。长任务中，用户需要理解 Agent 产生了哪些文件、报告和中间结果。

**最终权衡方案**

mini-agent 为每个 `DBTaskPlan` 或用户任务创建 `TaskWorkspace`。目录结构建议：

```text
.mini_agent/tasks/{task_id}/
  sql/
  explain/
  health/
  approvals/
  logs/
  reports/
```

其中敏感大结果只保存摘要，原始大结果默认不落盘。

**达成效果**

一次数据库诊断或变更任务可以完整复盘：看过哪些对象、跑过哪些 EXPLAIN、生成了哪些 SQL、用户审批了什么、最终验证结果是什么。

### 7.6 设计点六：所有执行动作都经过 ExecutionEnvironmentManager

**Codex 的设计方案**

Codex 的工具调用不是直接裸执行，而是经过统一工具编排层，处理 sandbox、审批、事件记录、错误和状态更新。

**Claude 的设计方案**

Claude 的工具调用会进入 permission context、deny rules、tool-specific validation 和用户交互路径。工具执行前有统一权限判断。

**最终权衡方案**

mini-agent 新增 `ExecutionEnvironmentManager`，作为文件工具、shell 工具、PostgreSQL 工具执行前的统一检查入口。它读取当前 `WorkspaceProfile`、`DatabaseEnvironmentProfile`、`TaskWorkspace`、`RuntimePolicy` 和当前 `TaskStep`，再决定是否允许执行。

**达成效果**

工具权限不再分散在各个工具内部。所有执行动作都可以统一记录“在哪里执行、为什么允许、使用了什么环境、产物保存在哪里”。

### 7.7 设计点七：生产环境默认保守，写工具默认不可见

**Codex 的设计方案**

Codex 对危险命令和需要越权的操作会触发审批，不能因为模型想执行就直接执行。

**Claude 的设计方案**

Claude 的 destructive 工具和 permission prompt 会向用户展示具体风险，并要求用户确认。高风险动作需要更强的人机协作。

**最终权衡方案**

当 `DatabaseEnvironmentProfile.is_production=True` 时，mini-agent 默认只暴露只读和诊断工具。写工具只有在 execute 阶段、dry run 产出证据、审批通过且 SQL hash 匹配后才可见或可执行。

**达成效果**

用户说“帮我修生产库”时，Agent 会先观察、诊断、提出方案、请求审批，而不会直接执行 DDL/DML。

### 7.8 设计点八：禁止 shell/psql 绕过数据库工具链

**Codex 的设计方案**

Codex 提供 shell 能力，但也会通过命令白名单、危险命令识别、sandbox 和审批控制风险。

**Claude 的设计方案**

Claude 将 Bash 与其他工具分开，并通过权限上下文限制 Bash。Bash 不应该绕过更专业工具的安全边界。

**最终权衡方案**

mini-agent 保留 shell 工具用于文件、测试和项目脚本，但 `RuntimePolicy.allow_shell_database_clients` 默认 false。shell 工具检测到 `psql`、`pg_dump`、`pg_restore`、`createdb`、`dropdb` 等数据库客户端命令时，默认拒绝并引导使用 PostgreSQL 专用工具。

**达成效果**

数据库访问必须经过 SQL classifier、只读事务、审批和审计。不会因为一条 `psql -c "DROP TABLE ..."` 绕过工具系统。

### 7.9 设计点九：执行产物按生命周期管理

**Codex 的设计方案**

Codex 会控制上下文和执行历史，避免无限膨胀。长输出需要摘要化，关键证据要能保留。

**Claude 的设计方案**

Claude 工具结果会控制大小和展示形式，不会把所有原始内容无限塞进上下文。

**最终权衡方案**

mini-agent 的 `ArtifactRecord.lifecycle` 分为：

```text
ephemeral
  临时 EXPLAIN 原始 JSON、临时查询样本

session
  当前会话需要的 SQL 草稿、诊断中间结果

persistent
  审批快照、执行日志、最终报告、验证证据摘要
```

敏感结果默认只保留 digest，不保留完整原始数据。

**达成效果**

系统既能保留审计和恢复所需证据，又不会长期保存大量敏感查询结果或撑爆上下文。

### 7.10 设计点十：数据库凭据只能存在配置层和 driver 层

**Codex 的设计方案**

Codex 在执行环境中会区分系统配置、工具输入和模型上下文。敏感配置不应被普通文本输出泄露。

**Claude 的设计方案**

Claude 的工具输出和权限展示需要避免泄露密钥。敏感字段应被隐藏或只以引用形式出现。

**最终权衡方案**

mini-agent 中连接串、密码、token 只存在环境变量、配置对象和 PostgreSQL driver。状态、prompt、报告、记忆中只允许出现 `credential_ref` 和脱敏环境画像。任何错误信息和工具输出都必须经过脱敏。

**达成效果**

Agent 可以连接真实 PostgreSQL，但不会把 `postgresql://user:password@host/db` 写进 prompt、日志、报告或长期记忆。

### 7.11 设计点十一：所有执行动作必须支持超时和取消

**Codex 的设计方案**

Codex 的命令执行有 timeout、进程控制和失败反馈，避免长命令卡住整个 Agent。

**Claude 的设计方案**

Claude 的工具执行会向用户反馈状态。长时间或危险操作需要用户能够理解当前进度和风险。

**最终权衡方案**

mini-agent 在 `RuntimePolicy` 中定义最大工具时长，并在 PostgreSQL 环境中定义 `statement_timeout` 和 `lock_timeout`。Agent Loop 中断时，当前工具应尽量取消；数据库查询必须通过数据库超时机制兜底。

**达成效果**

慢查询、锁等待、维护操作不会无限挂起。用户可以中断长任务，系统也能记录超时原因并进入恢复或重规划。

### 7.12 设计点十二：写入操作必须绑定事务边界和回滚策略

**Codex 的设计方案**

Codex 的高风险执行会走审批和受控执行路径，并保留执行结果，方便用户确认和复盘。

**Claude 的设计方案**

Claude 的 destructive 工具权限提示会把工具名、输入和风险展示给用户，用户确认的是具体动作。

**最终权衡方案**

mini-agent 的 `write_session` 要求 `approval_id`、SQL hash、影响说明、回滚说明和验证标准。能放入事务的写操作使用显式事务；不能普通事务回滚的操作，例如 `CREATE INDEX CONCURRENTLY`，必须在审批材料中明确说明。

**达成效果**

数据库写入不再是“执行一段 SQL”，而是一个可审批、可审计、可验证的变更动作。

### 7.13 设计点十三：任务状态必须可恢复

**Codex 的设计方案**

Codex 的工作流通过状态和事件轨迹支持多轮协作，长任务可以在后续回合继续。

**Claude 的设计方案**

Claude 的协作体验也强调上下文延续，用户可能中途补充信息、批准或拒绝某个操作。

**最终权衡方案**

mini-agent 在 AgentState 中保存 `workspace_profile`、`database_environment`、`task_workspace`、`artifact_records`、`runtime_policy`。恢复时，系统能重建当前文件边界、数据库目标、当前步骤、已生成证据和审批状态。

**达成效果**

长时间数据库诊断或变更任务即使中断，也能从“已观察”“待审批”“已执行待验证”等状态继续，而不是重新开始。

### 7.14 设计点十四：执行环境要生成用户可读运行摘要

**Codex 的设计方案**

Codex 的 TUI/事件流会展示工具执行状态，让用户知道系统正在做什么、结果如何。

**Claude 的设计方案**

Claude 的工具使用消息会给用户展示工具调用和结果摘要，便于用户参与协作。

**最终权衡方案**

mini-agent 在重要工具调用后生成运行摘要：目标环境、工具名、是否只读、耗时、返回行数、是否截断、是否脱敏、产物路径、是否需要审批。摘要进入 CLI/UI，也进入 `ToolInvocationRecord`。

**达成效果**

用户能清楚看到 Agent 的动作路径。对数据库任务尤其重要，因为用户需要知道“是否真的写库、写了什么、在哪个环境写的”。

### 7.15 设计点十五：执行环境管理拆成四个核心对象

**Codex 的设计方案**

Codex 将工具运行、工作区和权限管理拆成多个内部职责，而不是让工具自己处理所有上下文。

**Claude 的设计方案**

Claude 的工具权限和协作上下文也是独立于具体工具的系统能力。工具只实现能力，权限由上下文和规则决定。

**最终权衡方案**

mini-agent 拆出四个对象：

```text
WorkspaceManager
  管文件根目录、读写边界、路径安全

DatabaseEnvironmentManager
  管数据库环境画像、连接策略、会话类型

TaskWorkspaceManager
  管每个任务的产物目录和任务恢复信息

ArtifactStore
  管产物记录、生命周期、敏感级别和摘要
```

`ExecutionEnvironmentManager` 作为门面统一协调它们。

**达成效果**

执行环境与工作区管理成为基础设施，而不是散落在文件工具、shell 工具、PostgreSQL 工具中的临时代码。

## 8. 第一阶段落地顺序

### 8.1 状态结构

1. 增加 `WorkspaceProfile`。
2. 增加 `DatabaseEnvironmentProfile`。
3. 增加 `TaskWorkspace`。
4. 增加 `ArtifactRecord`。
5. 增加 `RuntimePolicy`。

### 8.2 管理器

1. 实现 `WorkspaceManager`。
2. 实现 `DatabaseEnvironmentManager`。
3. 实现 `TaskWorkspaceManager`。
4. 实现 `ArtifactStore`。
5. 实现 `ExecutionEnvironmentManager`。

### 8.3 工具接入

1. 文件工具接入路径边界检查。
2. shell 工具接入数据库客户端命令拦截。
3. PostgreSQL driver 接入 `DatabaseEnvironmentProfile`。
4. 工具执行记录补充环境摘要和产物引用。

### 8.4 恢复与展示

1. AgentState 持久化执行环境状态。
2. CLI 展示当前工作区和数据库环境。
3. 任务中断后恢复 `TaskWorkspace`。
4. 报告生成引用 `ArtifactRecord`。

## 9. 验收标准

1. 所有文件写入都必须在允许工作区内。
2. 所有数据库工具都能读取当前 `DatabaseEnvironmentProfile`。
3. 生产环境下写工具默认不可见。
4. shell 工具默认拒绝 `psql`、`pg_dump`、`pg_restore`、`createdb`、`dropdb`。
5. PostgreSQL 连接串不会进入 prompt、报告、日志或记忆。
6. 每个数据库任务都有独立 `TaskWorkspace`。
7. EXPLAIN、health、approval、execution、verification 都能生成 `ArtifactRecord`。
8. 工具调用记录包含工作区、数据库环境、任务产物引用。
9. 超时、取消和失败都会写入执行日志。
10. 任务中断后可以从状态恢复当前环境和产物。

## 10. 关键取舍总结

1. 采用 Codex 的工作区、执行环境、sandbox、审批和事件轨迹思想，但不照搬通用 shell 作为数据库管理主入口。
2. 采用 Claude 的工具权限上下文和人机协作展示方式，但不把权限完全交给用户即时判断，而是结合 TaskStep、RuntimePolicy 和 DatabaseEnvironmentProfile 自动约束。
3. mini-agent 的最终方案是“面向数据库任务的受控执行环境”，让文件、shell、PostgreSQL 工具都在同一套工作区和环境策略下运行。
