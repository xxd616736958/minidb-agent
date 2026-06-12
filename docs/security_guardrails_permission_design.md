# PostgreSQL 管理智能体：安全护栏与权限控制模块设计

## 1. 背景

mini-agent 已经具备任务理解、规划、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 工具、执行环境与工作区管理、状态管理等能力。随着目标从通用 Agent 转向 PostgreSQL 数据库管理智能体，安全护栏不能再停留在提示词层面。

PostgreSQL 管理任务会触达真实数据库对象：

- schema、table、index、view、function。
- 生产库、测试库、本地库。
- 只读查询、诊断查询、执行计划。
- DML、DDL、权限变更、维护命令。
- 连接信息、业务数据、慢 SQL、锁等待和统计信息。

这些动作的风险差异非常大。一个只读 `SELECT` 可能只是普通诊断，一个无条件 `DELETE` 可能直接造成数据丢失，一个 `CREATE INDEX` 可能造成锁等待，一个 `EXPLAIN ANALYZE` 可能真实执行 SQL，一个 shell 中的 `psql -c` 可能绕过所有数据库工具策略。

因此，本模块的目标是把 mini-agent 的安全能力从“模型应该谨慎”升级为“代码层强制执行的安全策略链”。

## 2. 模块定位

安全护栏与权限控制模块位于 Agent Loop、工具调用、执行环境、状态管理之间，是所有高风险动作的统一门禁。

```text
User Request
  -> Task Understanding
  -> Planning / TaskStep
  -> Tool Pool Builder
  -> LLM Tool Call
  -> Security Guardrails / Permission Control
       -> Environment Policy
       -> Tool Capability Policy
       -> SQL Safety Policy
       -> Approval Policy
       -> Workspace Policy
       -> Memory Safety Policy
       -> Replay Policy
       -> Output Sensitivity Policy
  -> Tool Executor
  -> Observation / Verification / Audit
```

安全模块不负责生成 SQL，不负责执行 SQL，也不负责替代用户决策。它负责回答五个问题：

1. 当前环境是否允许这类动作？
2. 当前步骤是否允许这个工具？
3. 当前 SQL 是否符合安全规则？
4. 这个动作是否需要用户审批？
5. 执行结果如何限制、脱敏、审计和恢复？

## 3. 设计目标

1. 所有工具调用都经过统一安全门禁，不能被 shell、插件或恢复流程绕过。
2. PostgreSQL 环境分级：`local / dev / staging / production / unknown`。
3. 生产环境和未知环境默认保守，优先只读和诊断。
4. SQL 执行前必须结构化分类，不能只靠提示词约束。
5. 写操作必须绑定具体审批，审批不能泛化复用。
6. 写操作执行前必须有 dry-run、影响说明、回滚说明和验证标准。
7. 工具可见性、可调用性和可执行性分层控制。
8. 工具输出需要限制规模、脱敏、摘要化和审计化。
9. 状态恢复和重试不能自动重放有副作用动作。
10. 用户的长期安全约束必须进入策略判断。
11. 默认失败策略是拒绝或澄清，而不是猜测执行。

## 4. 非目标

1. 不实现完整数据库权限系统，不替代 PostgreSQL 自身的 role、grant、RLS、审计插件。
2. 不保证任意 SQL 都能完美静态判断，第一阶段采用保守分类。
3. 不把所有生产库变更都完全禁止，而是默认禁止，后续可通过显式高权限模式扩展。
4. 不让模型自行解释安全规则后直接执行，所有高风险动作必须经过代码层策略。
5. 不把原始敏感数据、大结果集和真实连接串写入长期记忆。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经有以下安全相关结构：

- `RuntimePolicy`：控制 shell database client、网络工具、文件写、数据库写、审批要求、工具时长等。
- `DatabaseEnvironmentProfile`：记录目标环境、数据库名、安全 host/user label、是否生产库、超时、最大返回行数、是否允许写工具。
- `ToolCapability` / `RegisteredToolSpec`：表达工具领域、操作类型、风险等级、是否只读、是否 destructive、是否需要审批。
- `ToolCallPolicyDecision`：表达 `allow / deny / require_approval / require_clarification`。
- `ApprovalDecision`：记录审批状态、step、环境、SQL preview、SQL hash、影响说明、回滚说明、验证标准。
- `ToolInvocationRecord`：记录工具调用、参数摘要、策略决策、审批、状态、耗时和观察结果。
- PostgreSQL SQL classifier 和 normalized SQL hash。
- 执行环境中已经默认禁止 shell database client。

### 5.2 主要不足

这些能力目前分散在工具注册、执行环境、状态管理和 Agent Loop 中，还缺少一个明确的“安全护栏与权限控制模块”来统一表达：

1. 哪些策略属于硬规则，哪些策略只是模型提示。
2. 哪些字段必须参与审批绑定。
3. SQL 分类结果如何影响工具策略。
4. 输出脱敏、结果截断和记忆写入之间如何联动。
5. 恢复、重试、并发和插件工具如何遵守同一安全规则。
6. 安全拒绝后 Agent 应该如何反馈用户和重新规划。

## 6. 推荐核心对象

### 6.1 SecurityPolicyDecision

```python
class SecurityPolicyDecision(TypedDict):
    id: str
    scope: Literal[
        "tool_visibility",
        "tool_call",
        "sql_execution",
        "workspace_access",
        "output_handling",
        "state_replay",
    ]
    subject: str
    decision: Literal["allow", "deny", "require_approval", "require_clarification"]
    risk_level: Literal["low", "medium", "high", "critical"]
    reasons: list[str]
    matched_rules: list[str]
    approval_payload: Optional[dict[str, Any]]
    created_at: str
```

说明：`ToolCallPolicyDecision` 可以继续用于工具调用层，`SecurityPolicyDecision` 是更通用的安全决策对象，用于 SQL、工作区、输出、恢复等场景。

### 6.2 SQLSafetyReport

```python
class SQLSafetyReport(TypedDict):
    sql_hash: str
    normalized_sql_preview: str
    classification: Literal[
        "read_only",
        "diagnostic",
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
        "unsafe",
        "unknown",
    ]
    contains_multiple_statements: bool
    contains_dangerous_constructs: list[str]
    target_objects: list[dict[str, Any]]
    requires_approval: bool
    requires_rollback_plan: bool
    requires_backup_check: bool
    can_run_in_readonly_transaction: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    denial_reason: Optional[str]
```

### 6.3 ApprovalBinding

```python
class ApprovalBinding(TypedDict):
    approval_id: str
    step_id: str
    tool_name: str
    target_environment: str
    target_database: Optional[str]
    sql_hash: Optional[str]
    impact_summary: str
    rollback_summary: str
    verification_criteria: list[str]
    expires_at: Optional[str]
```

说明：`ApprovalDecision` 记录用户审批结果，`ApprovalBinding` 表达一次审批能授权的精确边界。

### 6.4 OutputSafetyPolicy

```python
class OutputSafetyPolicy(TypedDict):
    max_rows: int
    max_chars: int
    mask_sensitive_fields: bool
    sensitive_field_patterns: list[str]
    allow_raw_result_in_context: bool
    allow_raw_result_in_memory: bool
    artifact_required_for_large_output: bool
```

### 6.5 SafetyAuditRecord

```python
class SafetyAuditRecord(TypedDict):
    id: str
    event_type: Literal[
        "tool_visible",
        "tool_hidden",
        "tool_allowed",
        "tool_denied",
        "approval_requested",
        "approval_resolved",
        "sql_allowed",
        "sql_denied",
        "output_masked",
        "replay_blocked",
    ]
    step_id: Optional[str]
    tool_name: Optional[str]
    decision_id: Optional[str]
    summary: str
    created_at: str
```

## 7. 策略执行流程

推荐统一流程：

```text
1. bootstrap security context
   读取 current_intent、current_step、database_environment、runtime_policy、retrieved_memories。

2. build tool visibility
   根据 phase、tool_policy、environment、memory 生成本轮模型可见工具。

3. evaluate tool call
   模型发起工具调用后，先检查工具是否注册、是否启用、是否允许当前 phase 使用。

4. evaluate sql safety
   如果工具包含 SQL 参数，先生成 SQLSafetyReport。

5. evaluate approval
   如果动作需要审批，检查 ApprovalBinding 是否匹配 step、environment、tool、sql_hash。

6. evaluate execution environment
   检查生产库策略、只读事务、超时、锁等待、文件边界、shell database client。

7. execute or block
   allow 才进入 ToolExecutor；deny / require_approval / require_clarification 返回结构化原因。

8. normalize output
   工具结果脱敏、截断、摘要化，生成 Observation、Digest、AuditRecord。

9. update replay policy
   标记工具调用是否可恢复重放，写操作默认不可自动重放。
```

## 8. 详细设计点

### 8.1 设计点一：安全策略必须成为代码层硬门禁

**Codex 的设计方案**

Codex 的核心思路是将高风险动作放到沙箱、审批和工具编排之后执行。模型可以提出 shell 命令、文件修改或其他工具调用，但是否真正执行取决于 runtime policy、sandbox、approval 和 workspace 边界。它的好处是安全不完全依赖提示词；局限是通用 shell 场景太自由，很多风险需要靠命令级规则、审批和沙箱兜底。

**Claude 的设计方案**

Claude 的工具体系强调 permission context、tool permission、deny rules、工具输入校验和用户可见的 permission prompt。模型可以调用工具，但工具本身和权限上下文会决定是否允许。它的好处是交互透明，用户能看到敏感动作；局限是如果工具本身过宽，比如一个万能 SQL 工具，权限提示仍然可能太粗。

**最终方案**

mini-agent 增加独立的 `SecurityPolicyEngine`，所有工具调用都必须经过它。它不只返回普通布尔值，而是返回结构化 `SecurityPolicyDecision`，明确 `allow / deny / require_approval / require_clarification`、风险等级、命中的规则和用户需要看的审批 payload。

**权衡原因**

Codex 证明安全必须在执行层强制，Claude 证明权限决策必须可解释、可交互。mini-agent 选择二者结合：策略判断由代码强制，策略结果对用户和模型都可见。

**效果**

模型可以自由分析和建议，但不能绕过安全门禁执行数据库动作。新增工具时只要接入 `SecurityPolicyEngine`，就会被统一约束。

### 8.2 设计点二：建立数据库环境分级，生产库和未知库默认保守

**Codex 的设计方案**

Codex 会根据当前 workspace、sandbox mode、network policy、approval policy 决定工具能做什么。同一个工具在不同执行环境中风险不同。它的好处是把环境作为权限判断的一部分；局限是 Codex 面向代码工作区，数据库环境的生产/测试语义需要业务系统自己补足。

**Claude 的设计方案**

Claude 的 permission context 会带入当前会话、项目、工具来源和权限配置。工具是否可用不是全局固定，而是受上下文影响。它的好处是权限可以跟当前上下文绑定；局限是数据库环境识别仍需要 Agent 自己实现。

**最终方案**

mini-agent 使用 `DatabaseEnvironmentProfile` 明确记录：

```text
environment_name: local / dev / staging / production / unknown
target_database
safe_host_label
safe_user_label
access_mode
is_production
allow_write_tools
require_backup_check_for_writes
```

`production` 和 `unknown` 默认只允许只读、诊断和报告工具。写工具默认隐藏和拒绝，除非后续明确进入高权限模式并满足审批、备份检查和 SQL hash 绑定。

**权衡原因**

Codex 的环境策略适合做底层边界，Claude 的上下文权限适合做交互边界。mini-agent 面对真实数据库，必须把数据库环境提升为一等安全输入。

**效果**

用户没有明确说明环境时，系统不会冒险写库；连接到生产库时，模型即使请求写工具也会被拒绝。

### 8.3 设计点三：工具可见性、工具可调用性、工具可执行性分层控制

**Codex 的设计方案**

Codex 的工具 router 会根据当前 turn context、工具模式、MCP 状态、动态工具等生成模型可见工具；工具被模型调用后，还要经过执行编排、审批和沙箱。它的好处是工具不是全局静态暴露；局限是工具越通用，后置安全判断压力越大。

**Claude 的设计方案**

Claude 会通过 `assembleToolPool`、deny rules 和 permission context 过滤工具池。被策略禁用的工具不应出现在模型可见工具列表里。它的好处是降低模型误调用概率；局限是过滤规则必须跟工具能力元数据保持一致。

**最终方案**

mini-agent 把工具权限分成三层：

```text
tool_visibility:
  这一轮模型能不能看到工具。

tool_call:
  模型调用后，当前 step 和 policy 是否允许这个工具。

tool_execution:
  工具真正执行前，SQL、安全审批、环境和状态是否允许。
```

例如 `observe` 阶段只显示 schema、index、readonly query、explain 工具；`execute` 阶段只有在审批通过后才显示或允许 `postgres_execute_write`。

**权衡原因**

Codex 的动态工具路由和 Claude 的工具过滤都说明：不要把所有能力一次性暴露给模型。mini-agent 进一步区分“可见、可调用、可执行”，适合数据库这种高风险场景。

**效果**

模型在只读阶段不容易误选写工具；即使工具可见，仍要过后续策略；即使策略允许，还要过执行层安全检查。

### 8.4 设计点四：SQL 分类器是硬规则，不是辅助说明

**Codex 的设计方案**

Codex 对 shell 命令不是直接裸执行，而是结合命令、工作区、sandbox 和审批策略判断风险。它的好处是不会把命令字符串当成普通文本；局限是 shell 命令语义非常开放，很难像 SQL 一样做领域级分类。

**Claude 的设计方案**

Claude 工具通常有 input schema、validateInput、checkPermissions。工具可以在执行前拒绝非法或危险输入。它的好处是工具边界清晰；局限是如果 schema 只定义 `sql: string`，仍然不足以表达 SQL 风险。

**最终方案**

mini-agent 的 PostgreSQL 工具执行前必须生成 `SQLSafetyReport`。分类至少包括：

```text
read_only
diagnostic
data_change
schema_change
permission_change
maintenance
transaction_control
unsafe
unknown
```

只读工具只允许 `read_only` 和明确安全的 `diagnostic`；写工具必须要求审批；`unsafe` 和 `unknown` 默认拒绝或澄清。多语句、`COPY PROGRAM`、`ALTER SYSTEM`、`DROP`、`TRUNCATE`、无 WHERE 的 `UPDATE/DELETE`、危险函数、事务控制语句都要提升风险等级。

**权衡原因**

Codex 的命令风险识别提供了通用思路，Claude 的工具输入校验提供了边界位置。mini-agent 选择在 PostgreSQL 工具边界做 SQL 领域级分类。

**效果**

即使模型把危险 SQL 塞进只读工具，系统也会在数据库连接前拒绝。安全不依赖模型是否“记得规则”。

### 8.5 设计点五：写操作必须绑定具体审批和 SQL hash

**Codex 的设计方案**

Codex 的审批针对具体工具动作或命令请求。用户批准的是当前动作，不是给模型永久开权。它的好处是审批和具体执行有绑定；局限是对于数据库写操作，仅绑定命令文本还不够，需要额外绑定环境、SQL hash、影响和回滚。

**Claude 的设计方案**

Claude 的 permission prompt 会展示工具名、输入和原因，让用户知道将要发生什么。它的好处是交互透明；局限是如果权限批准范围过宽，用户可能以为批准的是一个小动作，系统却执行了变体动作。

**最终方案**

mini-agent 的审批必须绑定：

```text
approval_id
step_id
tool_name
target_environment
target_database
sql_hash
impact_summary
rollback_summary
verification_criteria
```

写工具执行时必须传入 `approval_id` 和 `approved_sql_hash`，并重新计算 SQL hash。只要 SQL 文本、目标环境或执行步骤变化，就必须重新审批。

**权衡原因**

Codex 的审批生命周期适合做动作授权，Claude 的 permission prompt 适合做用户展示。数据库写操作的真实风险由 SQL 内容和目标环境共同决定，所以 mini-agent 必须把审批绑定做得更细。

**效果**

用户不会因为批准“优化慢查询”而间接批准“创建索引”或“更新数据”。审批不能被模型改写 SQL 后复用。

### 8.6 设计点六：写操作必须先 dry-run、影响评估、回滚说明和验证标准

**Codex 的设计方案**

Codex 在代码任务中通常会先读文件、查看状态、运行测试，再修改和验证。它的好处是先观察再行动；局限是 Codex 的代码修改可以通过 git diff 和测试回滚，数据库写操作的回滚更复杂。

**Claude 的设计方案**

Claude 在敏感工具调用前倾向于向用户展示工具输入、意图和结果，让用户参与确认。它的好处是用户能在关键动作前介入；局限是如果没有结构化影响评估，用户仍然难以判断是否该批准。

**最终方案**

mini-agent 写操作前必须产出 dry-run 报告，至少包含：

```text
SQL classification
target objects
expected affected rows or unknown
lock risk
transaction behavior
rollback summary
backup check need
verification criteria
```

DML 需要建议或执行对应的行数估算；DDL 需要说明锁风险和是否支持 `CONCURRENTLY`；维护操作需要说明对连接、锁和性能的影响。

**权衡原因**

Codex 的先观察后修改适合变更前证据收集，Claude 的用户确认适合变更前交互。mini-agent 需要把二者变成数据库写操作协议。

**效果**

用户审批时看到的是证据和影响，而不是模型一句“我准备执行”。执行后也能按验证标准检查是否达成目标。

### 8.7 设计点七：shell database client 默认禁止，防止绕过专用工具

**Codex 的设计方案**

Codex 强依赖 shell，但通过 sandbox、审批、工作区边界和危险命令识别控制风险。它的好处是能力强、通用性高；局限是 shell 能绕过领域工具的专门安全逻辑。

**Claude 的设计方案**

Claude 也提供 Bash，但同时提供 Read、Write、Edit、MCP 等专用工具，并通过权限系统限制工具调用。它的好处是专用工具能表达更清晰的安全边界；局限是 Bash 仍可能成为旁路。

**最终方案**

mini-agent 保留 shell 能力用于项目脚本、测试和文件任务，但 `RuntimePolicy.allow_shell_database_clients` 默认 false。检测到以下命令时默认拒绝：

```text
psql
pg_dump
pg_restore
createdb
dropdb
createuser
dropuser
vacuumdb
reindexdb
clusterdb
```

如需数据库操作，引导模型使用 PostgreSQL 专用工具。

**权衡原因**

Codex 的 shell 模式适合代码 Agent，但 mini-agent 的数据库动作必须经过 SQL 分类、审批、脱敏、超时和审计。Claude 的专用工具边界更适合数据库管理。

**效果**

模型不能用 `psql -c` 绕开 SQL classifier、ApprovalBinding、ToolInvocationRecord 和输出脱敏。

### 8.8 设计点八：读操作也要受限，避免拖垮数据库和泄露数据

**Codex 的设计方案**

Codex 对命令输出、上下文注入和文件读取有规模控制和摘要化倾向。它的好处是避免大量无关数据污染上下文；局限是通用输出截断不理解数据库字段敏感性。

**Claude 的设计方案**

Claude 工具结果会被映射为结构化 tool_result，并面向 transcript 和模型上下文展示。它的好处是结果可控；局限是数据库结果脱敏仍需要领域规则。

**最终方案**

mini-agent 的只读工具也必须设置：

```text
statement_timeout
lock_timeout
max_result_rows
max_output_chars
result truncation
sensitive field masking
digest instead of raw large result
```

字段名命中 `password/token/secret/key/email/phone/id_card` 等模式时默认脱敏。大结果只进入 artifact，不直接进入 LLM 上下文和长期记忆。

**权衡原因**

Codex 的上下文控制适合限制输出规模，Claude 的 tool_result 思路适合规范展示。数据库 Agent 需要增加敏感字段识别和结果 digest。

**效果**

只读任务不会因为大查询拖垮数据库，也不会把敏感业务数据大面积写入上下文、日志或记忆。

### 8.9 设计点九：安全记忆和用户约束必须参与权限判断

**Codex 的设计方案**

Codex 会遵循系统、开发者和用户指令的优先级，并在任务过程中避免覆盖用户已有工作。它的好处是用户约束能持续影响行为；局限是如果约束只在提示词中存在，工具层不一定能硬性执行。

**Claude 的设计方案**

Claude 的记忆和偏好可以影响后续回答和工具使用，权限上下文也能参与工具过滤。它的好处是长期协作更贴合用户规则；局限是记忆内容需要分类，否则普通偏好和安全禁令会混在一起。

**最终方案**

mini-agent 将长期记忆中的 `prohibition`、`policy`、`preference` 区分开。`prohibition` 和明确安全策略进入硬门禁，例如：

```text
这个库只允许查询。
不要修改 public schema。
任何索引创建都必须先让我确认。
生产环境禁止 DELETE。
```

工具策略在判断写操作时必须检索相关 SafetyMemory。如果命中禁止规则，直接 `deny` 或 `require_clarification`。

**权衡原因**

Codex 的指令优先级适合处理当前会话约束，Claude 的记忆适合处理跨会话约束。mini-agent 需要把安全类记忆提升为策略输入，而不是普通上下文。

**效果**

用户不用每次重复组织规则；Agent 在长会话和跨任务中也不会忘记关键安全边界。

### 8.10 设计点十：恢复、重试和并发不能自动重放副作用动作

**Codex 的设计方案**

Codex 会记录工具执行过程、审批和状态，避免把外部副作用当成普通文本重新生成。它的好处是执行过程可恢复；局限是具体哪些动作可重放，需要每类工具自己声明。

**Claude 的设计方案**

Claude 的工具调用历史、tool_use、tool_result 和 permission decision 都是会话过程的一部分。它的好处是用户和模型能看到已发生动作；局限是数据库副作用动作仍需要专门 replay policy。

**最终方案**

mini-agent 在状态管理中为每个 `ToolInvocationRecord` 生成 replay policy：

```text
readonly / diagnostic:
  可在恢复时按需重放。

write / maintenance / backup_restore:
  不可自动重放。

unknown / partial failure:
  必须人工确认数据库实际状态。
```

Agent Loop 恢复时，如果上一次停在写操作之后，必须先进入状态核验或用户确认，而不是直接再次执行。

**权衡原因**

Codex 和 Claude 都把工具事件作为一等状态，但数据库动作对重复执行特别敏感。mini-agent 必须把副作用动作标为不可自动重放。

**效果**

系统具备 checkpoint 恢复能力，同时不会因为重试、断线、进程重启导致重复写库或重复维护。

### 8.11 设计点十一：工作区和文件写入也要进入安全策略

**Codex 的设计方案**

Codex 的工作区边界非常核心：只能在允许的根目录读写，不能随意破坏用户未授权的文件；对危险文件操作需要谨慎。它的好处是保护用户项目；局限是数据库 Agent 还需要把文件产物和数据库任务绑定。

**Claude 的设计方案**

Claude 将 Read、Write、Edit 等文件工具拆分，并通过 permission context 和工具校验控制路径和操作。它的好处是文件能力粒度清晰；局限是长期任务中的 SQL 草稿、报告和审计产物需要 Agent 自己管理目录。

**最终方案**

mini-agent 使用 `WorkspaceProfile` 和 `TaskWorkspace` 作为文件安全边界。SQL 草稿、EXPLAIN、审批记录、报告和日志只能写入 `.mini_agent/tasks/{task_id}` 或允许的 workspace 路径。任何写入 workspace 外的动作默认拒绝或要求审批。

**权衡原因**

Codex 的 workspace sandbox 适合保护项目目录，Claude 的文件工具边界适合表达读写能力。mini-agent 还需要把数据库任务产物集中到任务工作区，方便审计和清理。

**效果**

数据库任务产生的 SQL、报告、日志不会散落到系统任意位置，也不会覆盖用户无关文件。

### 8.12 设计点十二：输出脱敏、摘要和记忆写入要统一处理

**Codex 的设计方案**

Codex 会把工具输出转换成模型可消费的结构，避免无限制输出进入上下文。它的好处是上下文更稳定；局限是它不天然理解数据库业务字段的敏感等级。

**Claude 的设计方案**

Claude 的工具结果有 UI 展示和模型输入两套映射思路，可以对结果做不同形态的展示。它的好处是面向人和模型的输出可以分层；局限是具体脱敏策略仍依赖工具实现。

**最终方案**

mini-agent 定义 `OutputSafetyPolicy`，工具结果先归一化为 `ToolExecutionResult`，再生成：

```text
DBObservation:
  给 Agent Loop 推理和验证。

ResultDigest:
  给上下文和报告。

ArtifactRecord:
  保存大结果或完整证据引用。

MemoryCandidate:
  只允许写入脱敏摘要和经验，不允许写入原始敏感数据。
```

**权衡原因**

Codex 的结构化输出适合机器处理，Claude 的分层展示适合人机协作。mini-agent 必须将“输出给模型、输出给用户、写入文件、写入记忆”分开处理。

**效果**

上下文不会被大结果污染，长期记忆不会保存敏感数据，最终报告仍能引用证据 artifact。

### 8.13 设计点十三：拒绝和澄清也是正常结果，不是异常

**Codex 的设计方案**

Codex 在遇到权限不足、沙箱限制或危险操作时，会请求审批、降级执行或明确说明无法继续。它的好处是流程不会假装成功；局限是如果拒绝原因不结构化，后续重规划会困难。

**Claude 的设计方案**

Claude 的 permission decision 可以是允许、拒绝或请求用户确认，并将原因反馈给用户。它的好处是人机协作自然；局限是 Agent Loop 需要理解这些结果并调整计划。

**最终方案**

mini-agent 把 `deny` 和 `require_clarification` 当作一等策略结果。安全拒绝后，Agent Loop 应根据原因选择：

```text
缺少环境信息:
  询问用户。

当前步骤不允许:
  重规划。

生产库写操作:
  提供只读替代诊断。

SQL unknown:
  请求用户拆分 SQL 或由 Agent 重写为安全版本。

审批缺失:
  生成审批请求。
```

**权衡原因**

Codex 的执行失败处理适合工具编排，Claude 的权限交互适合用户参与。mini-agent 需要让安全结果驱动后续流程，而不是简单报错结束。

**效果**

安全策略不会让 Agent 卡死；系统能从拒绝中恢复为澄清、重规划、只读替代或审批流程。

### 8.14 设计点十四：所有安全决策必须可审计

**Codex 的设计方案**

Codex 的工具调用、审批、sandbox attempt、命令输出和错误都可以形成事件流。它的好处是问题可复盘；局限是数据库管理还需要把 SQL hash、环境和审批绑定纳入审计。

**Claude 的设计方案**

Claude 的 tool_use、tool_result、permission decision 对用户可见，交互过程透明。它的好处是用户知道 Agent 做了什么；局限是审计粒度是否足够，需要应用层定义。

**最终方案**

mini-agent 增加 `SafetyAuditRecord` 或扩展现有 `ToolInvocationRecord`，记录：

```text
工具为什么可见或隐藏。
工具为什么允许、拒绝或需要审批。
SQL 为什么被判定为高风险。
审批绑定了哪个 SQL hash。
输出做了哪些脱敏和截断。
恢复时为什么禁止重放。
```

**权衡原因**

Codex 重视执行事件，Claude 重视用户可见工具过程。数据库 Agent 需要兼顾工程复盘和用户信任。

**效果**

出问题后可以追踪“系统为什么这么做”，也可以用审计数据优化策略规则和测试用例。

### 8.15 设计点十五：默认失败策略必须 fail-closed

**Codex 的设计方案**

Codex 面对未授权写入、沙箱外访问、危险命令时会要求审批或拒绝。它的好处是边界明确；局限是可能牺牲一部分流畅度。

**Claude 的设计方案**

Claude 的权限系统在不确定时倾向于询问用户或遵守 deny rule。它的好处是用户能控制敏感动作；局限是频繁确认会增加交互成本。

**最终方案**

mini-agent 对以下情况默认拒绝或澄清：

```text
数据库环境 unknown 且请求写操作。
SQL 分类 unknown。
工具没有 ToolSpec。
审批记录缺少 SQL hash。
当前 step 和工具风险不匹配。
生产库请求 DDL/DML。
输出敏感等级无法判断。
恢复状态显示上一步可能有副作用但结果未知。
```

**权衡原因**

Codex 和 Claude 都用权限边界换取安全性。mini-agent 的目标是管理真实 PostgreSQL，默认保守比默认执行更合理。

**效果**

系统会牺牲少量自动化流畅度，但显著降低误写、误删、泄露和重复执行风险。

## 9. 推荐策略规则矩阵

| 场景 | 默认决策 | 触发依据 | 后续动作 |
| --- | --- | --- | --- |
| local/dev 只读 SELECT | allow | SQLSafetyReport=read_only | 执行只读事务，限制行数 |
| production 只读 SELECT | allow | read_only + timeout + row limit | 执行只读事务，脱敏结果 |
| unknown 环境写操作 | deny | env=unknown + data/schema change | 询问用户确认环境 |
| production DML/DDL | deny | env=production + mutating SQL | 提供 dry-run 或只读诊断 |
| staging/dev DML | require_approval | data_change | dry-run + ApprovalBinding |
| staging/dev DDL | require_approval | schema_change | 影响评估 + 回滚说明 |
| 无 WHERE UPDATE/DELETE | require_approval 或 deny | high risk pattern | 要求用户明确确认范围 |
| 多语句 SQL | deny | multiple_statements=true | 要求拆分 |
| SQL 分类 unknown | require_clarification | classification=unknown | 解释无法判断原因 |
| shell psql | deny | shell database client | 引导使用 PostgreSQL 工具 |
| 大结果集 | allow with truncation | output policy | 保存 artifact，注入 digest |
| 恢复后写操作未确认 | require_clarification | replay policy non-replayable | 先核验数据库状态 |

## 10. 与现有模块的关系

### 10.1 与任务理解模块

任务理解模块输出 `operation_nature`、`target_environment`、`risk_level`、`requires_approval`。安全模块不能完全相信这些字段，但会把它们作为初始风险信号。

### 10.2 与规划模块

规划模块通过 `TaskStep.phase`、`operation_type`、`risk_level`、`tool_policy` 和 `requires_approval` 限制每一步。安全模块负责检查实际工具调用是否符合当前步骤。

### 10.3 与 Agent Loop

Agent Loop 在 `tool_policy_gate` 节点调用安全模块。如果返回 `allow`，进入工具执行；如果返回 `require_approval`，进入审批等待；如果返回 `deny` 或 `require_clarification`，进入重规划或用户澄清。

### 10.4 与工具注册与调用模块

工具注册模块提供 `ToolCapability` 和 `RegisteredToolSpec`。安全模块基于这些元数据判断工具风险、阶段适配和审批要求。

### 10.5 与工具实现模块

工具实现模块必须在内部再次校验 SQL 安全、审批 hash、执行环境和输出策略。安全模块是前置门禁，工具内部校验是最后防线。

### 10.6 与执行环境模块

执行环境模块提供 workspace、database environment、runtime policy 和 session 限制。安全模块读取这些信息，并将决策落实到只读事务、超时、锁等待、写会话等执行配置。

### 10.7 与状态管理模块

状态管理模块保存安全决策、审批、工具调用、恢复策略和审计记录。安全模块依赖状态进行审批匹配和 replay 判断。

### 10.8 与记忆系统

记忆系统提供 SafetyMemory。安全模块把 `prohibition` 和显式安全规则作为硬约束，把普通偏好作为软约束。

## 11. 推荐实现步骤

### 11.1 第一阶段：收敛现有策略

1. 新增 `safety/` 模块。
2. 新增 `SecurityPolicyEngine`。
3. 将 `tools/policy.py` 中的策略判断逐步迁移到安全模块。
4. 保留 `ToolCallPolicyDecision` 对外兼容。
5. 新增 `SQLSafetyReport` 数据结构，并复用现有 SQL classifier。

### 11.2 第二阶段：审批绑定强化

1. 引入 `ApprovalBinding` helper。
2. 写工具执行前校验 `approval_id + sql_hash + step_id + environment`。
3. 审批请求展示 dry-run、影响、回滚和验证标准。
4. 旧审批不允许跨 SQL、跨 step、跨环境复用。

### 11.3 第三阶段：输出安全和审计

1. 新增 `OutputSafetyPolicy`。
2. 工具结果统一脱敏、截断、摘要化。
3. 增加 `SafetyAuditRecord` 或扩展 `ToolInvocationRecord`。
4. 将输出安全结果写入 `DBObservation`、`ResultDigest`、`ArtifactRecord`。

### 11.4 第四阶段：恢复和并发安全

1. 为所有工具声明 replay policy。
2. 写操作、维护操作、备份恢复默认不可自动重放。
3. Agent Loop 恢复时检查上一次副作用动作状态。
4. 写工具默认禁止并发。

## 12. 验收标准

1. 只读任务中，模型看不到或无法调用写工具。
2. production / unknown 环境下，写工具默认被拒绝。
3. 只读工具收到 DML/DDL SQL 时，在连接数据库前拒绝。
4. 写工具没有 approval_id 或 SQL hash 不匹配时拒绝执行。
5. 审批只对当前 step、环境、工具和 SQL hash 有效。
6. shell 中的 `psql`、`pg_dump`、`dropdb` 等默认被拒绝。
7. 大查询结果被截断和摘要化，敏感字段被脱敏。
8. 安全记忆中的禁止规则能阻断对应工具调用。
9. checkpoint 恢复后不会自动重放写操作。
10. 每次安全拒绝、审批请求、输出脱敏和 replay 阻断都有审计记录。

## 13. 最终结论

Codex 给 mini-agent 的核心启发是：安全必须落实在执行环境、沙箱、审批和状态恢复里，不能只写在提示词中。Claude 给 mini-agent 的核心启发是：工具权限要与上下文、用户确认、工具 schema 和可见交互结合，不能让模型在不透明的权限边界里行动。

最终 mini-agent 的安全护栏与权限控制模块采用折中方案：底层学习 Codex 的硬边界和审批执行链，上层学习 Claude 的工具权限上下文和人机确认体验，再结合 PostgreSQL 的领域风险，形成“环境分级、工具分层、SQL 分类、审批绑定、输出脱敏、状态防重放、审计可复盘”的完整安全策略链。

