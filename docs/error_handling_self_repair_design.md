# PostgreSQL 管理智能体：错误处理与自我修复模块设计

## 1. 背景

mini-agent 已经具备 PostgreSQL 管理智能体的主要骨架：

- `DBTaskIntent` 负责任务理解。
- `DBTaskPlan` / `TaskStep` 负责规划与任务分解。
- Agent Loop 负责按步骤推理、调用工具、归一化观察和验证。
- 上下文管理负责把当前步骤、状态、工具结果和记忆注入模型。
- 安全护栏负责 SQL、工具、环境和审批边界。
- 人机协作模块负责任务卡、计划确认、审批卡和协作事件。

但数据库管理任务中，错误是常态而不是异常：

- PostgreSQL 连接失败、认证失败、权限不足。
- SQL 语法错误、语义错误、对象不存在。
- 锁等待、语句超时、连接池耗尽。
- 工具执行失败、输出格式不合法。
- LLM 生成了不符合工具 schema 的调用。
- 状态恢复后 `current_step_id`、`pending_approval` 或 `approval_card` 不一致。
- 安全策略拒绝写库动作。
- 用户中途改变约束，例如“不要执行，只生成报告”。

当前 mini-agent 已有基础错误处理节点 `error_handler`，能根据文本模式判断 retryable / non-retryable，并通过 `retry_count` 做有限重试。但这对 PostgreSQL 管理智能体还不够，因为数据库错误必须结合当前步骤、SQL hash、审批状态、环境、安全策略、工具结果和恢复现场来判断。

因此，本模块的目标是把错误处理从“字符串重试器”升级为“数据库任务自我修复控制层”。

## 2. 模块定位

错误处理与自我修复模块位于 Agent Loop、工具执行、安全护栏、状态管理和人机协作之间。

```text
Node / Tool / Safety / State / User Feedback
  -> Error Detection
  -> ErrorRecord
  -> Error Classification
  -> RecoveryDecision
      -> retry
      -> rewrite_sql
      -> adjust_tool_args
      -> run_diagnostic_tool
      -> repair_state
      -> replan_step
      -> ask_user
      -> abort_safely
  -> RecoveryAttempt
  -> Updated AgentState / CollaborationEvent / MemoryCandidate / Final Report
```

输入：

- `error`
- `policy_violation`
- `ToolExecutionResult`
- `DBObservation`
- `StateIntegrityReport`
- `SecurityPolicyDecision`
- `SQLSafetyReport`
- `ApprovalDecision`
- 当前 `TaskStep`
- 当前 `DBTaskPlan`
- 用户反馈

输出：

- `ErrorRecord`
- `RecoveryDecision`
- `RecoveryAttempt`
- 更新后的 `retry_budget`
- 更新后的 `replan_trigger`
- 更新后的 `task_stack` / `db_task_plan`
- 必要的 `pending_clarification` 或 `approval_card`
- 协作事件
- 最终错误报告

本模块不替代工具执行、安全护栏或规划模块。它只负责识别错误、判断恢复路径、组织修复动作，并把结果写回状态。

## 3. 设计目标

1. 所有错误都结构化记录，避免只保存一段自然语言报错。
2. PostgreSQL 错误要按数据库语义分类，而不是只分可重试和不可重试。
3. 错误必须绑定当前计划步骤、工具、SQL hash、环境和审批状态。
4. 能自动修复的错误自动修复，不能自动修复的错误明确请求用户。
5. 重试必须有预算、退避和熔断，不能无限循环。
6. SQL 修复必须重新经过安全检查，写 SQL 修改后必须重新审批。
7. 锁等待、超时和性能失败要转为只读诊断，而不是盲目重试。
8. 状态不一致优先走状态修复，不让模型猜现场。
9. 安全拒绝不是普通错误，必须转成安全替代路径。
10. 所有修复过程可审计、可恢复、可解释。
11. 修复经验可以进入记忆候选，但必须区分偏好、经验和安全禁令。
12. 最终失败也要输出可交付的错误报告。

## 4. 非目标

1. 不绕过安全护栏自动执行被拒绝的数据库操作。
2. 不自动修复生产库写操作。
3. 不把所有错误都交给 LLM 自由判断。
4. 不对有副作用的写工具做无审批重放。
5. 不在第一阶段实现复杂的分布式任务队列或后台作业系统。
6. 不把错误处理模块变成新的规划模块；重规划仍由规划模块执行。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经具备以下错误处理基础：

- `error`：全局错误字段。
- `retry_count`：当前错误重试次数。
- `error_handler`：按字符串模式判断是否重试。
- `ToolExecutionResult.error_type` / `error_message`：工具结果中的错误摘要。
- `DBObservation.type` 支持 `sql_error`、`tool_error`、`policy_denied`。
- `StateValidator` 可以输出 `errors`、`warnings`、`repair_actions`。
- `StateRecovery` 可以恢复 checkpoint 并生成 `recovery_summary`。
- `policy_violation` 可以表达工具策略或安全策略阻塞。
- `replan_trigger` 可以触发后续重规划。
- 人机协作模块已有 `CollaborationEvent`，可以记录关键协作节点。

### 5.2 当前不足

1. 错误还是以字符串为主，缺少稳定的 `ErrorRecord`。
2. PostgreSQL 错误没有细分，例如语法错误、锁超时、权限不足、连接失败没有不同恢复路径。
3. 重试预算是全局计数，不区分 step、tool、error_type 和 SQL hash。
4. SQL 修复链不明确，尤其写 SQL 修改后必须重新审批这一点还没有进入错误处理协议。
5. 状态修复和错误处理还没有统一入口。
6. 安全拒绝、工具失败、验证失败、状态损坏还没有统一的恢复决策对象。
7. 错误处理过程没有完整进入协作事件和最终报告。

## 6. 推荐核心对象

### 6.1 ErrorRecord

```python
class ErrorRecord(TypedDict):
    id: str
    source: Literal[
        "llm",
        "tool",
        "postgresql",
        "safety_policy",
        "state",
        "approval",
        "user",
        "system",
    ]
    error_type: Literal[
        "connection_error",
        "auth_error",
        "permission_denied",
        "syntax_error",
        "sql_semantic_error",
        "object_not_found",
        "lock_timeout",
        "statement_timeout",
        "deadlock_detected",
        "constraint_violation",
        "policy_denied",
        "approval_missing",
        "approval_mismatch",
        "state_integrity_error",
        "tool_schema_error",
        "tool_runtime_error",
        "llm_output_error",
        "unknown",
    ]
    severity: Literal["info", "warning", "error", "critical"]
    node_name: Optional[str]
    step_id: Optional[str]
    tool_name: Optional[str]
    tool_call_id: Optional[str]
    sql_hash: Optional[str]
    sqlstate: Optional[str]
    target_environment: str
    target_database: Optional[str]
    message: str
    raw_excerpt: Optional[str]
    retryable: bool
    requires_user_action: bool
    created_at: str
```

### 6.2 RecoveryDecision

```python
class RecoveryDecision(TypedDict):
    id: str
    error_id: str
    action: Literal[
        "auto_retry",
        "rewrite_sql",
        "adjust_tool_args",
        "run_diagnostic_tool",
        "repair_state",
        "replan_step",
        "ask_user",
        "abort_safely",
    ]
    reason: str
    confidence: float
    safety_notes: list[str]
    requires_new_approval: bool
    next_node: Optional[str]
    created_at: str
```

### 6.3 RecoveryAttempt

```python
class RecoveryAttempt(TypedDict):
    id: str
    error_id: str
    decision_id: str
    step_id: Optional[str]
    attempt_no: int
    action: str
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    summary: str
    created_at: str
    completed_at: Optional[str]
```

### 6.4 RetryBudget

```python
class RetryBudget(TypedDict):
    scope_key: str
    step_id: Optional[str]
    tool_name: Optional[str]
    error_type: str
    sql_hash: Optional[str]
    attempts: int
    max_attempts: int
    exhausted: bool
    last_error_id: Optional[str]
```

### 6.5 StateRepairAction

```python
class StateRepairAction(TypedDict):
    id: str
    source_report_id: str
    action_type: Literal[
        "sync_plan_stack",
        "reset_current_step",
        "expire_pending_approval",
        "regenerate_approval_card",
        "normalize_tool_result",
        "refresh_step_context",
        "mark_step_blocked",
    ]
    description: str
    status: Literal["pending", "applied", "failed", "skipped"]
    created_at: str
```

### 6.6 ErrorReport

```python
class ErrorReport(TypedDict):
    id: str
    task_id: Optional[str]
    plan_id: Optional[str]
    step_id: Optional[str]
    status: Literal["recovered", "partially_recovered", "failed"]
    error_ids: list[str]
    recovery_attempt_ids: list[str]
    evidence_refs: list[str]
    user_summary: str
    next_options: list[str]
    created_at: str
```

## 7. 错误分类与默认恢复策略

| 错误类型 | 常见来源 | 默认恢复策略 | 是否可自动重试 | 是否需要用户 |
| --- | --- | --- | --- | --- |
| connection_error | PostgreSQL 连接 | 延迟重试；失败后请求用户检查连接 | 是 | 可能 |
| auth_error | 认证失败 | 停止并请求用户检查凭据 | 否 | 是 |
| permission_denied | SQL 权限不足 | 请求权限确认或改为只读报告 | 否 | 是 |
| syntax_error | SQL 语法错误 | 改写 SQL；只读验证 | 有条件 | 否 |
| sql_semantic_error | 字段/表/函数不匹配 | 查询 schema 后改写 SQL | 有条件 | 否 |
| object_not_found | 对象不存在 | 刷新 schema / 请求用户确认对象 | 有条件 | 可能 |
| lock_timeout | 锁等待 | 转锁诊断工具 | 否 | 可能 |
| statement_timeout | 查询超时 | 改成 EXPLAIN / 限制范围 / 诊断 | 有条件 | 可能 |
| deadlock_detected | 写操作冲突 | 停止写操作，报告风险 | 否 | 是 |
| constraint_violation | 约束冲突 | 诊断数据和约束，不自动写 | 否 | 是 |
| policy_denied | 安全护栏 | 给替代路径，不重试 | 否 | 可能 |
| approval_missing | 审批缺失 | 生成审批卡并等待用户 | 否 | 是 |
| approval_mismatch | SQL hash 或环境不匹配 | 重新审批 | 否 | 是 |
| state_integrity_error | 状态校验 | 自动修状态或阻塞恢复 | 有条件 | 可能 |
| tool_schema_error | 工具参数错误 | 改参数或重新生成工具调用 | 有条件 | 否 |
| tool_runtime_error | 工具异常 | 按工具类型诊断 | 有条件 | 可能 |
| llm_output_error | 模型输出不合规 | 要求模型按 schema 重试 | 有条件 | 否 |

## 8. 详细设计点

### 8.1 设计点一：所有错误先结构化为 ErrorRecord

**Codex 的设计方案**

Codex 在执行命令、工具或文件操作时，不会只把失败压缩成一句“出错了”，而是保留命令、退出码、stdout、stderr、执行环境、沙箱限制和权限结果等结构化信息。它的好处是错误能被程序判断，后续可以决定重试、改命令、请求权限或终止；局限是这些字段偏工程执行，对 PostgreSQL 的 sqlstate、SQL hash、审批状态、目标环境没有领域化表达。

**Claude 的设计方案**

Claude 更擅长把错误翻译成用户能理解的解释，并根据对话上下文说明“为什么失败”和“接下来应该怎么做”。它的好处是用户体验好，尤其适合解释复杂错误；局限是如果只依赖自然语言解释，状态机、工具策略和恢复逻辑很难稳定消费。

**最终方案**

mini-agent 新增 `ErrorRecord`，所有错误都先归一化成结构化对象，字段包括 source、error_type、severity、node_name、step_id、tool_name、tool_call_id、sql_hash、sqlstate、target_environment、message、retryable、requires_user_action。

**权衡原因**

底层采用 Codex 的结构化执行错误思想，保证系统能判断和路由；上层采用 Claude 的解释能力，把错误摘要写成人能理解的内容。

**效果**

Agent 不会再凭一段字符串盲目重试，而是能知道错误发生在哪里、影响哪个步骤、是否涉及 SQL、是否能自动修复。

### 8.2 设计点二：建立 PostgreSQL 专用错误分类器

**Codex 的设计方案**

Codex 的错误分类偏通用工程场景，例如命令失败、权限失败、沙箱失败、网络失败、测试失败。它的好处是能覆盖大多数工具执行问题；局限是不能区分 PostgreSQL 的锁、事务、权限、对象、约束、sqlstate 等数据库语义。

**Claude 的设计方案**

Claude 可以根据报错文本和上下文推断错误含义，例如“relation does not exist”意味着对象不存在，“permission denied”意味着权限问题。它的好处是理解能力强；局限是单靠模型推断不够稳定，同一个错误可能被不同轮次解释成不同类型。

**最终方案**

mini-agent 实现规则优先、模型补充的 PostgreSQL 错误分类器。优先解析结构化字段，例如 `sqlstate`、工具返回的 `result_type`、安全策略 decision、状态校验 errors；规则不能覆盖时，再让模型根据错误上下文给出候选分类，但模型结果必须落入允许的 error_type 枚举。

**权衡原因**

数据库错误有大量稳定信号，应该先用规则保证确定性；Claude 式理解只用于补充模糊错误。

**效果**

系统可以针对不同错误走不同路径：语法错误修 SQL，锁超时查锁，权限不足问用户，安全拒绝给替代方案。

### 8.3 设计点三：错误必须绑定当前计划步骤

**Codex 的设计方案**

Codex 的执行过程会围绕当前任务、当前工作区和当前工具调用推进，错误通常能关联到刚刚执行的命令或修改。它的好处是错误定位清楚；局限是数据库任务还需要绑定计划步骤、审批和 SQL hash。

**Claude 的设计方案**

Claude 在对话中会根据当前目标解释错误，但如果没有结构化计划，错误和“当前步骤”的绑定主要依赖上下文文本。它的好处是灵活；局限是长任务里容易把前一步错误和后一步目标混在一起。

**最终方案**

mini-agent 的每条 `ErrorRecord` 必须尽量绑定 `current_step_id`、`TaskStep.phase`、`tool_call_id`、`tool_name`、`sql_hash` 和 `ApprovalDecision.id`。如果无法绑定，就标记为 `state_integrity_error` 或 `unknown`，并优先触发状态恢复。

**权衡原因**

Codex 的执行绑定适合严谨定位，Claude 的上下文理解适合补充摘要。mini-agent 必须让错误处理和计划步骤一致，避免修错对象。

**效果**

“收集索引信息失败”只会修 observe 步骤，“执行变更失败”会回到 execute/approval/rollback 语境，不会影响整个任务目标。

### 8.4 设计点四：用 RecoveryDecision 统一恢复分支

**Codex 的设计方案**

Codex 遇到失败时会在继续执行、请求权限、调整命令、停止之间做选择。它的好处是执行控制明确；局限是这些选择通常内嵌在工具循环和交互流程里，缺少 PostgreSQL 领域恢复动作。

**Claude 的设计方案**

Claude 可以自然地提出“我可以重试、换一种方式、请你补充信息或先给报告”。它的好处是表达灵活；局限是如果不结构化，状态机无法稳定执行这些选择。

**最终方案**

mini-agent 新增 `RecoveryDecision`，枚举恢复动作：`auto_retry`、`rewrite_sql`、`adjust_tool_args`、`run_diagnostic_tool`、`repair_state`、`replan_step`、`ask_user`、`abort_safely`。每个决策必须说明 reason、confidence、safety_notes、requires_new_approval 和 next_node。

**权衡原因**

Codex 提供硬路由思想，Claude 提供自然协作表达。最终方案把自然语言建议落成状态机可执行的恢复决策。

**效果**

错误处理不再是“要不要 retry”这一种选择，而是能针对数据库任务选择最安全、最有效的下一步。

### 8.5 设计点五：重试必须有预算、退避和熔断

**Codex 的设计方案**

Codex 的执行循环会避免无限重复失败动作，并在权限、沙箱或危险操作处停止等待用户确认。它的好处是不会卡死，也不会反复执行危险命令；局限是数据库写操作需要更细粒度的重试边界。

**Claude 的设计方案**

Claude 在多次失败后通常会解释原因，并建议换一种方式或请求用户补充信息。它的好处是用户理解成本低；局限是如果没有程序化预算，仍可能在 Agent Loop 中重复尝试。

**最终方案**

mini-agent 新增 `RetryBudget`，预算维度为 `step_id + tool_name + error_type + sql_hash`。连接类错误最多自动重试 2 次；LLM 输出格式错误最多重试 1 次；SQL 语法错误必须改写后才算新的尝试；安全拒绝、审批缺失、审批不匹配、生产写阻塞永不自动重试。

**权衡原因**

Codex 的熔断适合控制执行风险，Claude 的解释适合失败后交代清楚。数据库 Agent 不能用全局 retry_count 简单解决所有错误。

**效果**

长任务不会死循环，写操作不会被重复执行，用户能看到为什么停止或为什么换路径。

### 8.6 设计点六：SQL 错误走诊断、改写、验证、重新审批链路

**Codex 的设计方案**

Codex 遇到测试失败或命令失败，会根据输出修改代码或命令，然后重新验证。它的好处是有修复闭环；局限是 SQL 写操作有副作用，不能像代码测试一样随意重跑。

**Claude 的设计方案**

Claude 擅长根据错误消息解释 SQL 问题并改写 SQL。它的好处是语义修复能力强；局限是模型改写后的 SQL 不能天然被信任，尤其是写 SQL。

**最终方案**

mini-agent 对 SQL 错误采用链路：分类错误 -> 如需查 schema 则只读观察 -> 生成修正 SQL -> 重新运行 SQL safety -> 如果是只读 SQL 可以受预算控制重试 -> 如果是写 SQL 必须重新计算 SQL hash、生成新的审批卡并等待用户确认。

**权衡原因**

Codex 的修复验证闭环适合自动化，Claude 的 SQL 改写能力适合生成候选修复。最终方案用安全护栏和审批绑定限制改写后的执行。

**效果**

SELECT 语法错误可以快速修复；UPDATE/ALTER 即使只改了一点点，也不会复用旧审批，避免“批准的是 A，执行的是 B”。

### 8.7 设计点七：锁等待和超时转为诊断任务

**Codex 的设计方案**

Codex 遇到测试超时或命令卡住时，会考虑超时、环境、命令参数和替代命令。它的好处是不会只看表面失败；局限是数据库锁等待需要专门工具和领域判断。

**Claude 的设计方案**

Claude 能解释锁等待、慢查询和超时可能意味着什么，并给出排查建议。它的好处是 DBA 语义表达清楚；局限是如果没有工具调用计划，建议可能停留在文本。

**最终方案**

mini-agent 遇到 `lock_timeout`、`statement_timeout`、`deadlock_detected`、慢查询类错误时，不直接重试原 SQL，而是生成 `RecoveryDecision(action="run_diagnostic_tool")`，切换到只读诊断工具，例如 lock inspect、top queries、EXPLAIN、schema/index summary。

**权衡原因**

Codex 的超时处理给出执行控制，Claude 的数据库解释给出诊断方向。最终方案把失败转成可执行的 observe/diagnose 步骤。

**效果**

Agent 更像 DBA：遇到锁先查阻塞关系，遇到超时先缩小范围或分析执行计划，而不是盲目加 timeout 或反复执行。

### 8.8 设计点八：状态损坏优先自修状态

**Codex 的设计方案**

Codex 的 checkpoint、工作区状态和工具调用历史让任务可以恢复，不必完全依赖模型记忆。它的好处是长任务可以继续；局限是它不天然包含数据库审批、SQL hash 和计划步骤一致性。

**Claude 的设计方案**

Claude 的上下文延续适合解释“我们之前做到哪里”，但如果状态结构损坏，仅靠对话上下文难以可靠恢复。它的好处是用户沟通自然；局限是不能替代一致性校验。

**最终方案**

mini-agent 遇到 `StateIntegrityReport.ok == false` 时，优先生成 `StateRepairAction`，例如同步 `db_task_plan.steps` 和 `task_stack`、重置 current_step、过期不匹配审批、重新生成 approval_card、刷新 step_context。只有状态无法自动修复时，才进入 `ask_user` 或 `abort_safely`。

**权衡原因**

Codex 的状态恢复适合机器可执行恢复，Claude 的解释适合向用户说明恢复结果。最终方案先修状态，再让模型继续推理。

**效果**

恢复 checkpoint、迁移旧状态或异常中断后，Agent 不会靠模型猜现场，能先把状态修到一致。

### 8.9 设计点九：安全拒绝不进入普通 retry

**Codex 的设计方案**

Codex 遇到沙箱、权限或危险动作限制时，会把边界当作硬约束，不会靠重复尝试绕过。它的好处是安全性强；局限是如果只拒绝不解释，用户可能不知道下一步怎么做。

**Claude 的设计方案**

Claude 擅长把拒绝转成解释和替代方案。它的好处是用户不会觉得任务卡死；局限是如果没有状态机，替代方案不一定能自动执行。

**最终方案**

mini-agent 对 `policy_denied`、`approval_missing`、`approval_mismatch`、`production_write_blocked` 直接生成安全分流决策，不做普通 retry。替代路径包括只读诊断、生成 SQL 草案、dry-run、请求审批、只生成报告。

**权衡原因**

Codex 的硬边界防止越权，Claude 的解释能力降低中断成本。最终方案把安全拒绝变成可继续推进的安全分支。

**效果**

安全护栏不会被错误处理模块绕过；用户能看到为什么不能做，以及还能怎么继续。

### 8.10 设计点十：工具 schema 和 LLM 输出错误走轻量自修

**Codex 的设计方案**

Codex 对工具调用和命令格式有明确协议，格式错误时会调整调用方式或停止。它的好处是工具边界清晰；局限是模型输出错误需要结合当前工具 schema 做修复。

**Claude 的设计方案**

Claude 能根据工具 schema 或错误提示重新生成符合格式的输入。它的好处是修复自然语言到结构化参数的能力强；局限是如果没有预算约束，可能反复生成无效调用。

**最终方案**

mini-agent 对 `tool_schema_error`、`llm_output_error` 允许一次轻量自修：把工具 schema、错误原因、当前 step_context 反馈给 LLM，要求只修参数或格式，不改变任务目标；第二次失败则进入 `ask_user` 或 `replan_step`。

**权衡原因**

Codex 的工具协议保证边界，Claude 的结构化重写能力提高成功率。最终方案限制自修范围，避免模型借格式修复改变任务。

**效果**

少量参数格式错误可以自动修复，不会因为一次 schema 错误中断整个数据库任务。

### 8.11 设计点十一：错误处理进入人机协作事件流

**Codex 的设计方案**

Codex 会展示命令、工具调用、失败和重试等过程信息。它的好处是执行透明；局限是数据库错误还需要展示 SQL hash、审批状态、风险和替代路径。

**Claude 的设计方案**

Claude 会用自然语言解释失败原因和下一步选择。它的好处是用户容易理解；局限是如果解释不进入状态，后续恢复和审计无法使用。

**最终方案**

mini-agent 扩展 `CollaborationEvent`，新增错误相关事件类型，例如 `error_explained`、`repair_attempted`、`retry_scheduled`、`repair_succeeded`、`repair_failed`、`user_action_required`。每次错误分类、恢复决策、修复尝试和最终失败都写入事件流。

**权衡原因**

Codex 的执行透明适合审计，Claude 的表达方式适合协作。最终方案让错误处理既可见又可恢复。

**效果**

用户可以看到“为什么失败、系统尝试了什么、现在需要我做什么”，恢复任务时也能复盘错误处理过程。

### 8.12 设计点十二：错误经验进入记忆候选，但不直接变成永久规则

**Codex 的设计方案**

Codex 会严格遵守当前任务和开发者约束，保护用户工作区，但长期偏好需要额外机制。它的好处是当前任务安全；局限是跨会话经验沉淀不足。

**Claude 的设计方案**

Claude 的记忆和偏好机制适合沉淀长期规则，例如用户偏好、组织规范和常见约束。它的好处是跨轮连续；局限是需要区分一次性错误、经验偏好和安全禁令。

**最终方案**

mini-agent 对重复出现的错误生成 `MemoryCandidate`，但不直接写入长期记忆。候选类型分为 experience、preference、prohibition。例如“这个库生产环境只允许只读”可以作为 prohibition 候选，“报告里要包含 SQLSTATE”可以作为 preference 候选，“某表经常锁等待”可以作为 experience 候选。

**权衡原因**

Codex 的当前约束适合即时生效，Claude 的记忆适合长期协作。最终方案通过候选和确认机制避免把偶然错误固化为永久规则。

**效果**

Agent 会越用越懂项目规则，但不会因为一次失败就错误地改变长期行为。

### 8.13 设计点十三：最终失败也输出 ErrorReport

**Codex 的设计方案**

Codex 完成或失败后会总结执行了什么、验证了什么、还有什么问题。它的好处是工程闭环清楚；局限是数据库失败报告需要包含审批、SQL hash、环境和风险字段。

**Claude 的设计方案**

Claude 擅长把复杂失败组织成清晰报告，告诉用户原因、影响和下一步。它的好处是可读性高；局限是必须绑定结构化证据，否则容易成为泛泛总结。

**最终方案**

mini-agent 在无法恢复时生成 `ErrorReport`，包含任务目标、失败步骤、错误类型、SQL hash、审批状态、已尝试修复、未执行动作、停止原因、证据引用和下一步选项。

**权衡原因**

Codex 的工程总结说明“做了什么”，Claude 的报告表达说明“意味着什么”。最终方案要求报告既可读又可审计。

**效果**

即使任务失败，用户也能拿到可交付、可复盘、可继续处理的数据库问题报告。

## 9. 推荐错误处理状态机

| 状态 | 触发来源 | 核心动作 | 下一步 |
| --- | --- | --- | --- |
| error_detected | node/tool/state/safety | 生成 ErrorRecord | classify_error |
| classify_error | ErrorRecord | 判断 error_type、severity、retryable | decide_recovery |
| decide_recovery | 分类结果和当前状态 | 生成 RecoveryDecision | recovery 分支 |
| retry_scheduled | transient error | 更新 RetryBudget、退避重试 | 原节点或 llm_reason |
| sql_repairing | syntax/semantic error | 查 schema、改写 SQL、安全检查 | verify_or_approval |
| diagnostic_running | lock/timeout/performance | 执行只读诊断工具 | normalize_observation |
| state_repairing | state_integrity_error | 应用 StateRepairAction | state_recovery 或 scheduler |
| waiting_for_user | auth/permission/approval | 生成澄清或审批卡 | END 等用户 |
| replanning | step blocked/recoverable | 设置 replan_trigger | task_planner |
| aborted_safely | unrecoverable/safety stop | 生成 ErrorReport | END |
| recovered | 修复成功 | 记录 RecoveryAttempt | 回到 step_scheduler |

## 10. 与现有模块的关系

### 10.1 与 Agent Loop

Agent Loop 负责发现错误来源，例如工具失败、验证失败、策略阻塞。错误处理模块负责把这些失败转成结构化错误和恢复决策。

### 10.2 与工具模块

工具模块应该尽量返回结构化 `ToolExecutionResult`，包含 `result_type`、`sqlstate`、`duration_ms`、`payload`。错误处理模块根据这些字段分类和修复。

### 10.3 与安全护栏

安全护栏输出的 deny / require_approval 不应被当成普通异常。错误处理模块只能提供替代路径，不能绕过安全决策。

### 10.4 与状态管理

状态管理提供 `StateValidator`、`StateRecovery` 和 checkpoint。错误处理模块消费 `StateIntegrityReport`，生成 `StateRepairAction`。

### 10.5 与人机协作

错误处理模块把错误解释、修复尝试、用户动作需求写入 `CollaborationEvent`，必要时生成澄清问题或审批卡。

### 10.6 与记忆系统

错误处理模块可以生成 `MemoryCandidate`，但是否写入长期记忆由记忆系统和用户确认策略决定。

### 10.7 与规划模块

当恢复决策是 `replan_step` 时，错误处理模块只设置 `replan_trigger` 和错误上下文，真正重规划仍由规划模块执行。

## 11. 推荐实现步骤

### 11.1 第一阶段：结构化错误协议

1. 在 `agent/state.py` 新增 `ErrorRecord`、`RecoveryDecision`、`RecoveryAttempt`、`RetryBudget`、`StateRepairAction`、`ErrorReport`。
2. 在 `AgentState` 中新增：
   - `error_records`
   - `recovery_decisions`
   - `recovery_attempts`
   - `retry_budgets`
   - `state_repair_actions`
   - `error_reports`
3. 状态迁移补默认字段。
4. 上下文管理注入最近错误和恢复决策。

### 11.2 第二阶段：错误分类器

1. 新增 `error_handling/classifier.py`。
2. 支持从 `error`、`ToolExecutionResult`、`DBObservation`、`SecurityPolicyDecision`、`StateIntegrityReport` 生成 `ErrorRecord`。
3. 解析 PostgreSQL 常见 `sqlstate` 和错误文本。
4. 增加测试覆盖连接、权限、语法、锁、超时、安全拒绝、状态错误。

### 11.3 第三阶段：恢复决策引擎

1. 新增 `error_handling/recovery.py`。
2. 根据 ErrorRecord、TaskStep、SQLSafetyReport、ApprovalDecision、RetryBudget 生成 RecoveryDecision。
3. 实现 retry budget 和熔断。
4. 安全拒绝、审批缺失、审批不匹配永不进入普通 retry。

### 11.4 第四阶段：接入 Agent Loop

1. 改造 `error_handler`，从字符串 retry 升级为结构化恢复。
2. `tool_executor` 工具失败时生成 ErrorRecord。
3. `verify_step` blocked 时生成 ErrorRecord 和 RecoveryDecision。
4. `route_after_error_handler` 根据 RecoveryDecision 路由到 llm_reason、task_planner、state_recovery 或 END。

### 11.5 第五阶段：人机协作和报告

1. 扩展 `CollaborationEvent` 错误事件类型。
2. CLI 展示错误摘要、修复动作、重试预算和用户动作需求。
3. 生成最终 `ErrorReport`。
4. 错误经验生成 `MemoryCandidate`。

## 12. 验收标准

1. 工具失败会生成 `ErrorRecord`，且包含 source、error_type、step_id、tool_name。
2. SQL 语法错误能进入 `rewrite_sql` 恢复决策。
3. 写 SQL 改写后必须重新计算 SQL hash，并要求新审批。
4. 锁超时不会直接重试原 SQL，而是进入只读锁诊断。
5. 安全拒绝不会进入 retry，而是生成替代路径。
6. 审批缺失会生成审批卡并停止等待用户。
7. 状态不一致会生成 `StateRepairAction`，可自动修复的自动修复。
8. 同一错误超过预算后触发熔断，不再重复尝试。
9. 错误处理过程写入 `CollaborationEvent`。
10. 最终失败生成 `ErrorReport`，包含证据、已尝试修复和下一步建议。
11. 全量测试覆盖错误分类、恢复决策、路由、状态迁移和 CLI 展示。

## 13. 最终结论

Codex 给 mini-agent 的核心启发是：错误处理必须绑定真实执行过程，保留结构化执行结果、权限边界、重试预算和可恢复状态，不能让模型凭一段错误文本自由发挥。Claude 给 mini-agent 的核心启发是：错误处理要能解释、能协作、能把失败转成用户可理解的下一步选择。

最终 mini-agent 的错误处理与自我修复模块采用折中方案：底层学习 Codex 的结构化错误、状态恢复、有限重试和硬安全边界；上层学习 Claude 的错误解释、SQL 修复建议、用户协作和报告表达。结合 PostgreSQL 管理场景，形成“错误结构化、数据库分类、步骤绑定、恢复决策、重试熔断、SQL 安全修复、状态自修、安全分流、协作事件、错误报告”的完整闭环。
