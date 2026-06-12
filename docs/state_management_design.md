# PostgreSQL 管理智能体：状态管理模块设计

## 1. 背景

mini-agent 已经逐步引入了任务理解、规划与任务分解、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 具体工具、执行环境与工作区管理等模块。随着系统从通用工具 Agent 变成 PostgreSQL 管理智能体，状态管理不再只是保存聊天消息，而是要保存一个可恢复、可审计、可验证的数据库任务现场。

当前系统已经有 `AgentState` 和 LangGraph checkpoint，但状态字段开始增多：

```text
messages
current_intent / intent_history
db_task_plan / task_stack / current_step_id
db_observations / result_digests / db_working_set
approval_decisions / pending_approval
verification_results
tool_invocation_records / tool_execution_results
workspace_profile / database_environment / task_workspace / artifact_records
memory_candidates / retrieved_memories / memory_records_written
```

如果没有状态管理模块统一约束，系统会出现以下问题：

1. 节点随意写字段，导致状态来源不清楚。
2. `task_stack`、`db_task_plan.steps`、`current_step_id` 可能不一致。
3. 工具结果有了，但没有归一化成 `DBObservation`。
4. 审批记录、SQL hash、工具调用之间缺少强绑定。
5. checkpoint 能恢复聊天消息，但不能可靠恢复数据库任务现场。
6. 大量敏感数据或连接信息可能进入长期状态。
7. 状态结构升级后，旧 checkpoint 可能无法继续使用。

因此，本模块的核心目标是：让 mini-agent 的状态从“聊天上下文容器”升级为“数据库任务状态机”，所有关键动作都能被恢复、校验、审计和安全重放。

## 2. 模块定位

状态管理模块位于所有核心模块之下，是 Agent 的运行底座：

```text
User Request
  -> Task Understanding
  -> Planning
  -> Agent Loop
  -> Tool Policy / Approval / Execution
  -> Observation / Verification / Report
  -> State Management
       -> State Schema
       -> State Normalization
       -> State Snapshot
       -> State Validation
       -> State Migration
       -> Recovery Policy
  -> LangGraph Checkpoint
```

状态管理模块不替代业务节点，而是给业务节点提供统一的数据契约、更新规范、校验规则和恢复语义。

## 3. 设计目标

1. 保留 `AgentState` 作为唯一主状态，避免多个状态源互相同步。
2. 将状态按职责分区：会话、意图、计划、步骤、工具、数据库观察、审批、验证、上下文、记忆、执行环境。
3. 对关键状态更新建立统一入口，减少节点随意拼 dict。
4. 让状态能准确表达 PostgreSQL 任务现场：当前库、当前步骤、已查证据、待审批动作、已执行工具和产物。
5. 建立状态一致性校验，发现错位后进入阻塞、澄清或重规划。
6. 建立 checkpoint 恢复策略，恢复的不只是聊天消息，还包括数据库任务现场。
7. 控制状态中的敏感数据和大结果，只保存摘要、digest 和 artifact 引用。
8. 支持状态版本升级和旧 checkpoint 迁移。
9. 区分可重放和不可重放动作，避免恢复后重复写库。

## 4. 非目标

1. 不替代 LangGraph checkpoint。
2. 不在状态管理模块中实现 PostgreSQL 工具逻辑。
3. 不把长期记忆和 checkpoint 合并为一个存储。
4. 不把所有原始工具输出永久保存到状态。
5. 不引入复杂事件溯源系统，第一阶段以轻量结构化状态为主。

## 5. 当前 mini-agent 的主要问题

### 5.1 状态已经变大，但缺少分区规范

当前 `AgentState` 已经包含意图、规划、上下文、记忆、工具、执行环境等字段，但字段之间的边界主要靠注释和节点约定，缺少统一状态管理层。

### 5.2 状态更新分散在多个节点

`intent_analyzer`、`task_planner`、`llm_reason`、`tool_policy_gate`、`execute_tools`、`normalize_observation`、`verify_step` 都会更新状态。如果没有统一 helper，容易出现字段漏写、重复追加或互相覆盖。

### 5.3 当前状态和历史状态混在一起

例如 `current_intent` 是当前态，`intent_history` 是历史态；`current_step_id` 是当前态，`db_observations` 是历史证据。当前系统需要更明确地区分“下一步决策用的当前状态”和“审计复盘用的历史记录”。

### 5.4 checkpoint 恢复还不够面向数据库任务

LangGraph 可以恢复线程状态，但 mini-agent 还需要在恢复时知道哪些工具结果已归一化、哪些写操作不可重放、当前数据库工作集是否过期、是否还有 pending approval。

### 5.5 状态缺少版本和迁移机制

随着模块持续优化，`AgentState` 字段会继续增加。如果没有 `state_schema_version` 和迁移逻辑，旧 checkpoint 很容易在恢复时出现字段缺失或语义不一致。

## 6. 推荐状态分区

### 6.1 Session State

```python
session_id: str
state_schema_version: int
created_at: str
updated_at: str
step_count: int
loop_status: Literal[
    "running",
    "waiting_for_user",
    "waiting_for_approval",
    "replanning",
    "completed",
    "blocked",
]
error: Optional[str]
retry_count: int
max_retries: int
```

### 6.2 Task State

```python
current_intent: Optional[DBTaskIntent]
intent_history: list[DBTaskIntent]
db_task_plan: Optional[DBTaskPlan]
plan_history: list[DBTaskPlan]
current_step_id: Optional[str]
current_task_index: int
task_stack: list[TaskStep]
replan_trigger: Optional[str]
```

### 6.3 Database Evidence State

```python
db_observations: list[DBObservation]
result_digests: list[ResultDigest]
db_working_set: Optional[DBWorkingSet]
context_snapshots: list[ContextSnapshot]
```

### 6.4 Control State

```python
approval_decisions: list[ApprovalDecision]
pending_approval: Optional[ApprovalDecision]
verification_results: list[VerificationResult]
policy_violation: Optional[dict]
```

### 6.5 Tool State

```python
available_tools: list[str]
available_tool_specs: list[RegisteredToolSpec]
tool_policy_decisions: list[ToolCallPolicyDecision]
tool_invocation_records: list[ToolInvocationRecord]
tool_execution_results: list[ToolExecutionResult]
tool_calls_pending: list[dict]
```

### 6.6 Environment State

```python
workspace_profile: Optional[WorkspaceProfile]
database_environment: Optional[DatabaseEnvironmentProfile]
task_workspace: Optional[TaskWorkspace]
artifact_records: list[ArtifactRecord]
runtime_policy: Optional[RuntimePolicy]
```

### 6.7 Memory State

```python
short_term: list[dict]
working_memory: dict[str, str]
retrieved_memories: list[MemoryRecord]
memory_candidates: list[MemoryCandidate]
memory_records_written: list[MemoryRecord]
long_term_refs: list[str]
```

## 7. 新增核心对象建议

### 7.1 StateMetadata

```python
class StateMetadata(TypedDict):
    schema_version: int
    session_id: str
    created_at: str
    updated_at: str
    last_node: Optional[str]
    last_transition: Optional[str]
    recovery_mode: Literal["normal", "resumed", "forked", "migrated"]
```

### 7.2 DBTaskRuntimeState

```python
class DBTaskRuntimeState(TypedDict):
    intent_id: Optional[str]
    plan_id: Optional[str]
    current_step_id: Optional[str]
    current_phase: Optional[str]
    target_environment: str
    target_database: Optional[str]
    risk_level: str
    task_status: Literal["new", "planning", "running", "waiting", "blocked", "completed"]
    blocked_reason: Optional[str]
```

### 7.3 StateIntegrityReport

```python
class StateIntegrityReport(TypedDict):
    ok: bool
    errors: list[str]
    warnings: list[str]
    repair_actions: list[str]
    created_at: str
```

### 7.4 ReplayPolicy

```python
class ReplayPolicy(TypedDict):
    tool_call_id: str
    replayable: bool
    reason: str
    requires_new_approval: bool
```

## 8. 设计点与参考权衡

### 8.1 设计点一：保留单一主状态 AgentState，但按职责分区

**Codex 的设计方案**

Codex 的运行过程围绕当前会话、工作区、工具调用、权限和任务进度维护统一执行状态。它不会让不同模块各自维护无法同步的状态源。

**Claude 的设计方案**

Claude Code 的协作体验围绕一个项目会话展开，用户消息、工具调用、权限确认、结果展示都挂在同一个会话上下文里。

**最终权衡方案**

mini-agent 保留 `AgentState` 作为唯一主状态，但在类型定义和文档中明确分区：Session、Task、Evidence、Control、Tool、Environment、Memory。第一阶段不拆多个独立 store，避免同步复杂度。

**达成效果**

所有节点仍然通过 LangGraph 传递同一个状态，但开发者能清楚知道字段归属，减少“随便往 state 里塞字段”的混乱。

### 8.2 设计点二：引入状态元信息和 schema version

**Codex 的设计方案**

Codex 这类长期演进的执行系统需要处理不同版本的配置、工具能力和会话状态，状态结构不能假设永远不变。

**Claude 的设计方案**

Claude 的项目上下文和长会话也要面对上下文延续问题。旧对话不能因为内部能力升级就完全不可用。

**最终权衡方案**

mini-agent 在状态中增加 `state_metadata` 或至少增加 `state_schema_version`、`created_at`、`updated_at`、`last_node`、`recovery_mode`。恢复旧 checkpoint 时，先经过 `StateMigration` 补齐默认值和迁移字段。

**达成效果**

以后新增审批字段、工具记录字段、数据库环境字段时，旧会话可以平滑恢复，不会因为缺字段导致 Agent Loop 崩溃。

### 8.3 设计点三：状态更新走 StateManager，而不是节点手写大 dict

**Codex 的设计方案**

Codex 的工具执行和事件更新有明确通道，执行结果、错误和权限状态不会完全由模型自由拼接。

**Claude 的设计方案**

Claude 的工具调用、权限提示和结果消息有统一生命周期，用户看到的是一致的工具使用过程。

**最终权衡方案**

mini-agent 新增 `StateManager`，提供 `set_intent`、`set_plan`、`select_step`、`record_tool_decision`、`record_tool_result`、`record_observation`、`record_approval`、`record_verification`、`record_artifact`、`mark_blocked` 等 helper。节点只调用这些 helper 生成 partial state。

**达成效果**

状态更新逻辑可复用、可测试，减少字段漏写和 reducer 使用错误。例如工具结果写入时，可以同时更新 `tool_execution_results`、`artifact_records` 和 `task_workspace.artifact_ids`。

### 8.4 设计点四：当前态和历史态分离

**Codex 的设计方案**

Codex 需要同时知道当前要做什么，以及之前执行过什么。当前任务进度和历史命令/文件变更不是同一类信息。

**Claude 的设计方案**

Claude 的会话里既有当前回答需要的上下文，也有用户可以回看的工具历史和权限确认历史。

**最终权衡方案**

mini-agent 明确区分：

```text
当前态:
  current_intent, db_task_plan, current_step_id, pending_approval,
  db_working_set, step_context, runtime_policy

历史态:
  intent_history, plan_history, db_observations, approval_decisions,
  verification_results, tool_invocation_records, artifact_records,
  context_snapshots
```

**达成效果**

LLM prompt 可以优先读取当前态和摘要，不必塞入所有历史；审计和恢复仍然能查询完整历史。

### 8.5 设计点五：计划状态和步骤状态必须保持一致

**Codex 的设计方案**

Codex 的计划更新会持续反映当前任务进度，避免计划和实际执行脱节。

**Claude 的设计方案**

Claude 的 todo/task 状态会标记当前任务项和完成情况，复杂任务不只靠自然语言描述进度。

**最终权衡方案**

mini-agent 将 `db_task_plan.steps` 作为结构化计划主来源，`task_stack` 作为兼容旧逻辑的执行栈。`StateManager.sync_plan_and_stack()` 负责同步 `current_step_id`、`current_task_index`、step status、plan status。

**达成效果**

不会出现 `task_stack` 显示 step A running，但 `db_task_plan.steps` 显示 step B running 的情况。恢复 checkpoint 时也能准确定位下一步。

### 8.6 设计点六：数据库任务现场要有专门 runtime state

**Codex 的设计方案**

Codex 恢复工作时，不只是恢复聊天消息，还要知道当前目录、任务目标、文件状态和下一步动作。

**Claude 的设计方案**

Claude 的项目会话能延续上下文，用户中途回来继续协作时，不需要重新解释全部背景。

**最终权衡方案**

mini-agent 增加 `DBTaskRuntimeState`，从 `current_intent`、`db_task_plan`、`current_step_id`、`database_environment` 派生，记录当前 phase、target env、target database、risk、task status、blocked reason。

**达成效果**

用户说“继续刚才那个慢查询优化”，Agent 可以恢复到“diagnose 已完成，propose/approve 待处理”，而不是只看到聊天历史。

### 8.7 设计点七：工具调用状态记录完整生命周期

**Codex 的设计方案**

Codex 对工具/命令执行会记录输入、输出、错误、权限、耗时和执行环境，方便调试和继续执行。

**Claude 的设计方案**

Claude 的工具调用对用户可见，工具名、参数、权限确认和结果构成协作过程的一部分。

**最终权衡方案**

mini-agent 继续强化 `ToolInvocationRecord`，记录 `args_digest`、`policy_decision`、`approval_id`、`started_at`、`ended_at`、`status`、`duration_ms`、`result_ref`、`observation_ids`、`artifact_ids`、`environment_summary`、`error_type`、`error_message`。

**达成效果**

每次数据库动作都能回答：哪个工具、什么参数摘要、在哪个步骤、哪个环境、是否审批、结果是什么、证据在哪里。

### 8.8 设计点八：工具结果必须归一化成 DBObservation

**Codex 的设计方案**

Codex 不会只把工具输出当成普通聊天文本，而是会把结果用于后续决策和验证。

**Claude 的设计方案**

Claude 会把工具结果纳入后续上下文，但需要控制展示长度和信息密度。

**最终权衡方案**

mini-agent 要求 `execute_tools` 之后必须经过 `normalize_observation`，将 PostgreSQL 工具结果转成 `DBObservation`。只有 observation 和 digest 才能作为后续诊断、验证、报告的主要证据。

**达成效果**

EXPLAIN、锁等待、健康检查、写入结果不再散落在 `ToolMessage.content`，而是变成可查询、可引用、可验证的结构化证据。

### 8.9 设计点九：审批状态必须绑定具体 SQL、step 和环境

**Codex 的设计方案**

Codex 对危险动作会走审批或权限提升路径，批准的是具体动作，而不是无限授权。

**Claude 的设计方案**

Claude 的权限确认会围绕具体工具调用和输入展示，用户知道自己正在允许什么。

**最终权衡方案**

mini-agent 的 `ApprovalDecision` 增强为绑定 `step_id`、`target_environment`、`sql_preview`、`sql_hash`、`impact_summary`、`rollback_summary`、`verification_criteria`。写库工具执行时必须匹配 approval 和 SQL hash。

**达成效果**

用户批准的是“这条 SQL 在这个环境执行”，而不是批准 Agent 随后任意改数据库。

### 8.10 设计点十：状态中只保存摘要、digest 和引用

**Codex 的设计方案**

Codex 的执行环境会区分配置、工具输入、模型上下文和日志，敏感内容不能随便进入普通上下文。

**Claude 的设计方案**

Claude 的工具结果展示会控制内容大小，并避免泄露敏感凭据。

**最终权衡方案**

mini-agent 状态里禁止保存明文数据库密码、完整连接串和大批量查询结果。状态保存：

```text
credential_ref
safe_host_label / safe_user_label
ResultDigest
ArtifactRecord
payload_ref
masked sample rows
```

原始大结果默认不进 checkpoint，必要时写入受控 artifact，并标记 sensitivity 和 lifecycle。

**达成效果**

系统可以恢复和审计任务，但不会把生产库密码或大量业务数据长期写进 checkpoint。

### 8.11 设计点十一：状态一致性校验进入 Agent Loop 前置环节

**Codex 的设计方案**

Codex 执行前会基于当前 sandbox、审批、工作区和任务状态判断是否能继续。

**Claude 的设计方案**

Claude 在缺少权限或上下文时会让用户确认或补充，而不是硬执行。

**最终权衡方案**

mini-agent 增加 `StateValidator`，在关键节点前检查：

```text
current_step_id 是否存在于 plan
task_stack 和 db_task_plan 是否一致
pending_approval 是否绑定当前 step
生产环境是否禁用写工具
tool_execution_results 是否已归一化成 DBObservation
artifact_records 是否被 task_workspace 引用
db_working_set 是否过期
```

**达成效果**

状态损坏时系统能明确阻塞或重规划，不会带着错位状态继续执行数据库操作。

### 8.12 设计点十二：恢复 checkpoint 时执行 StateRecovery

**Codex 的设计方案**

Codex 的继续执行能力依赖恢复工作区、任务上下文和执行轨迹，而不是只恢复对话文本。

**Claude 的设计方案**

Claude 的长会话恢复强调上下文连续性，用户不需要重复解释项目背景。

**最终权衡方案**

mini-agent 在恢复线程时执行 `StateRecovery`：

1. 运行状态迁移。
2. 重建 `workspace_profile`、`database_environment`、`runtime_policy`。
3. 检查 `task_workspace` 是否存在。
4. 找到当前 step。
5. 标记不可重放工具调用。
6. 生成恢复摘要注入 prompt。

**达成效果**

用户恢复会话时，Agent 能说清楚当前任务进度、已完成证据、待审批动作和下一步，而不是重新开始。

### 8.13 设计点十三：区分可重放和不可重放动作

**Codex 的设计方案**

Codex 对命令执行非常谨慎，危险命令不会因为状态恢复或重试而自动重复执行。

**Claude 的设计方案**

Claude 的破坏性工具调用需要用户确认，不能把一次确认理解成未来无限可重复执行。

**最终权衡方案**

mini-agent 为工具调用生成 `ReplayPolicy`：

```text
可重放:
  postgres_query_readonly
  postgres_explain
  postgres_health_check
  schema inspect

不可自动重放:
  postgres_execute_write
  postgres_vacuum_table
  postgres_create_index_concurrently
  shell_execute
  file_write
```

不可重放动作恢复后只能展示历史结果，或要求用户重新审批。

**达成效果**

checkpoint 恢复、重试或 fork 不会导致重复 UPDATE、重复 CREATE INDEX、重复 VACUUM。

### 8.14 设计点十四：DBWorkingSet 要有来源和新鲜度

**Codex 的设计方案**

Codex 的上下文需要知道哪些信息来自当前工作区和最新工具结果，过期信息不能长期当作事实。

**Claude 的设计方案**

Claude 会持续使用上下文，但复杂项目中也需要避免拿旧信息做当前判断。

**最终权衡方案**

mini-agent 的 `DBWorkingSet` 增加或约定 `last_refreshed_at`、`source_observation_ids`、`stale_reason`。当用户切换数据库、执行 schema 变更、执行写操作或 observation 过旧时，标记 working set 过期。

**达成效果**

Agent 不会拿旧表结构、旧索引状态或旧行数估计继续做生产库决策。

### 8.15 设计点十五：状态快照服务于恢复，不服务于无限扩上下文

**Codex 的设计方案**

Codex 会维护执行轨迹和上下文，但不会把所有历史无边界塞进模型输入。

**Claude 的设计方案**

Claude 的上下文需要经过选择和压缩，工具历史不应无限膨胀。

**最终权衡方案**

mini-agent 保留 `ContextSnapshot`，每次关键节点只保存恢复所需 ID：intent、plan、current_step、observation_ids、approval_ids、verification_ids、db_working_set_ref、replan_trigger。详细内容通过对应结构查找。

**达成效果**

checkpoint 可以恢复任务现场，但 prompt 仍然可控，不会因为保存了大量历史而污染模型输入。

### 8.16 设计点十六：状态管理和长期记忆保持分离

**Codex 的设计方案**

Codex 的会话状态和持久知识不是一回事。执行状态负责当前任务，长期信息需要更严格的写入规则。

**Claude 的设计方案**

Claude 的项目上下文和长期偏好也应区分，不能把所有当前任务临时结果都变成长期事实。

**最终权衡方案**

mini-agent checkpoint 保存任务执行状态；长期记忆只保存经过 `MemoryCandidate`、敏感性校验、置信度判断和作用域约束的内容。状态可以引用长期记忆 ID，但不把 checkpoint 当长期记忆库。

**达成效果**

当前任务的临时诊断不会污染长期记忆；长期记忆也不会让 checkpoint 体积和恢复逻辑变复杂。

### 8.17 设计点十七：状态变化要能面向用户展示

**Codex 的设计方案**

Codex 的交互会展示任务进度、工具调用和执行结果，让用户知道系统正在做什么。

**Claude 的设计方案**

Claude 的协作体验强调用户能看到工具使用、权限确认和结果摘要。

**最终权衡方案**

mini-agent CLI/UI 增加状态摘要展示：

```text
当前环境: production/appdb
当前阶段: diagnose
当前步骤: inspect-locks
已收集证据: 4
待审批: create-index
最近工具: postgres_explain ok 120ms
任务状态: waiting_for_approval
```

**达成效果**

用户不需要读原始 checkpoint，也能理解 Agent 当前处于什么状态、下一步为什么需要审批或验证。

## 9. 第一阶段落地顺序

### 9.1 状态结构补强

1. 增加 `StateMetadata` 或 `state_schema_version`。
2. 增加 `DBTaskRuntimeState`。
3. 增强 `ApprovalDecision`，补充 SQL hash、verification criteria。
4. 增强 `DBWorkingSet`，补充来源和新鲜度字段。
5. 增加 `StateIntegrityReport`。
6. 增加 `ReplayPolicy`。

### 9.2 StateManager

1. 实现 `StateManager.now()`、`new_id()`。
2. 实现 `sync_plan_and_stack()`。
3. 实现 `record_tool_result()`。
4. 实现 `record_observation()`。
5. 实现 `record_approval()`。
6. 实现 `record_verification()`。
7. 实现 `record_artifact()`。
8. 实现 `mark_blocked()`、`mark_waiting_for_approval()`、`mark_completed()`。

### 9.3 校验与恢复

1. 实现 `StateValidator.validate(state)`。
2. 实现 `StateMigration.migrate(state)`。
3. 实现 `StateRecovery.recover(state)`。
4. 在 `START` 或 Agent Loop 前置节点中执行状态恢复。
5. 在关键节点后执行轻量状态一致性检查。

### 9.4 展示与测试

1. CLI 展示状态摘要。
2. 增加状态一致性单元测试。
3. 增加旧状态迁移测试。
4. 增加不可重放工具恢复测试。
5. 增加敏感字段不进入状态测试。

## 10. 验收标准

1. `AgentState` 有明确 schema version 或 metadata。
2. 恢复旧 checkpoint 时能自动补齐新增字段。
3. 当前计划步骤、`current_step_id`、`task_stack`、`db_task_plan.steps` 始终一致。
4. 工具调用必定生成 `ToolInvocationRecord`。
5. PostgreSQL 工具结果必定归一化成 `DBObservation` 或明确记录失败原因。
6. 写库审批必须绑定 step、环境、SQL hash、影响说明、回滚说明。
7. 生产环境状态下写工具不可见或被策略门拒绝。
8. 状态中不保存明文连接串、密码或大批量原始查询结果。
9. checkpoint 恢复后能生成用户可读恢复摘要。
10. 不可重放工具不会因恢复、重试或 fork 自动重复执行。
11. `DBWorkingSet` 能标记来源和是否过期。
12. 长期记忆和 checkpoint 状态保持分离。

## 11. 关键取舍总结

1. 采用 Codex 的统一执行状态、工作区意识、工具事件、审批和恢复思想，但不照搬通用编程 Agent 的宽松状态模型，而是面向 PostgreSQL 风险场景增加审批、SQL hash、数据库环境和不可重放语义。
2. 采用 Claude 的会话连续性、工具可见性、用户确认和任务协作体验，但不让状态只停留在自然语言上下文，而是沉淀为可校验的结构化状态。
3. mini-agent 的最终方案是“单一 AgentState + 状态分区 + StateManager + StateValidator + StateRecovery”。这样既兼容 LangGraph checkpoint，又能支撑数据库任务的恢复、审计和安全执行。
