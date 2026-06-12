# PostgreSQL 管理智能体：工具注册与调用模块设计

## 1. 背景

mini-agent 当前已经有一个基础工具系统：

```text
tools/base.py
  AgentTool 基类，继承 LangChain BaseTool

tools/registry.py
  自动扫描 tools.builtin 和 plugins，注册 BaseTool 子类

agent/nodes/llm_node.py
  将 registry.get_all() 返回的所有工具绑定给 LLM

agent/nodes/agent_loop.py
  tool_policy_gate 根据当前 step.tool_policy 阻断部分越权调用

agent/nodes/tool_executor.py
  使用 LangGraph ToolNode 执行工具
```

这套设计适合通用 Agent，但如果 mini-agent 要成为 PostgreSQL 数据库管理智能体，工具模块必须更严格。数据库工具会访问：

- schema、index、statistics。
- EXPLAIN / ANALYZE。
- 查询结果。
- 慢 SQL 和锁等待。
- DDL、DML、权限变更。
- 备份恢复或维护操作。
- 生产环境数据。

因此工具注册与调用不能只是“把所有工具暴露给模型，再执行模型调用”。它需要回答：

1. 哪些工具在当前任务阶段可见？
2. 哪些工具在当前权限下可调用？
3. 工具调用是否需要审批？
4. 工具结果如何结构化、脱敏和审计？
5. 外部插件或 MCP 工具如何接入但不绕过安全策略？

## 2. 模块定位

工具注册与调用模块位于规划、Agent Loop、安全护栏和上下文管理之间。

推荐关系：

```text
DBTaskIntent
  -> DBTaskPlan / TaskStep
  -> ToolPoolBuilder
  -> LLM bind_tools
  -> ToolCallPolicyGate
  -> Human Approval
  -> ToolExecutor
  -> ToolExecutionResult
  -> DBObservation / ResultDigest / MemoryCandidate
```

核心思想是：工具不再是全局静态能力，而是当前任务状态下的一组受控能力。

## 3. 设计目标

1. 工具注册时带上能力、风险、作用域和输出类型元数据。
2. 每轮只把当前 step 允许的工具暴露给 LLM。
3. PostgreSQL 读工具和写工具彻底分离。
4. 写工具必须走预览、审批、执行、验证流程。
5. 工具调用前统一经过策略门禁。
6. 工具执行结果统一结构化，方便验证、报告和记忆沉淀。
7. 插件和外部工具可以扩展，但不能绕过权限控制。
8. 工具调用全程可审计、可追踪、可回放。

## 4. 非目标

1. 不在本模块里重新设计所有 PostgreSQL adapter 的底层连接实现。
2. 不让模型直接通过 shell 调用 psql 作为主要数据库访问方式。
3. 不把旧审批复用成新 SQL 的执行许可。
4. 不让插件仅凭被扫描到就自动拥有数据库写权限。
5. 不把原始大结果集直接长期保存在上下文或记忆中。

## 5. 当前 mini-agent 的主要问题

### 5.1 工具注册缺少数据库安全语义

当前 `SkillRegistry` 只知道工具名、description 和 BaseTool 实例，不知道这个工具是不是只读、是否会写库、是否需要审批、适合哪个 task phase。

### 5.2 工具池是全局暴露

`llm_reason` 当前通过 `registry.get_all()` 把所有注册工具绑定给模型。即使当前 step 是 report 或 diagnose，模型仍可能看到不该使用的工具。

### 5.3 工具调用策略和执行混杂

当前 `tool_policy_gate` 已经能拦截一部分 PostgreSQL 写 SQL，但逻辑仍集中在 `agent_loop.py`，主要依赖工具名和 SQL 正则判断。

### 5.4 PostgreSQL 工具边界不够清晰

当前设计还没有形成明确的 PostgreSQL 工具体系，例如只读查询、EXPLAIN、schema inspect、index inspect、写 SQL 执行、dry run、锁诊断等。

### 5.5 工具结果还不是统一领域对象

`ToolNode` 返回 ToolMessage，后续 `normalize_observation` 再从文本里猜 observation type。对数据库 Agent 来说，结果应该从工具层开始就是结构化的。

## 6. 推荐数据结构

### 6.1 ToolCapability

```python
class ToolCapability(TypedDict):
    domain: Literal[
        "postgresql",
        "filesystem",
        "shell",
        "code",
        "memory",
        "human",
        "external",
    ]
    operation_type: Literal[
        "read_only",
        "diagnostic",
        "schema_change",
        "data_change",
        "permission_change",
        "backup_restore",
        "documentation",
        "none",
    ]
    risk_level: Literal["low", "medium", "high", "critical"]
    read_only: bool
    destructive: bool
    requires_approval: bool
    requires_transaction: bool
    supports_parallel: bool
```

### 6.2 ToolSpec

```python
class ToolSpec(TypedDict):
    name: str
    description: str
    args_schema: dict
    capability: ToolCapability
    allowed_phases: list[str]
    allowed_policies: list[str]
    output_type: str
    result_sensitivity: Literal["public", "internal", "sensitive", "secret"]
    plugin_source: Optional[str]
    enabled: bool
```

### 6.3 ToolCallPolicyDecision

```python
class ToolCallPolicyDecision(TypedDict):
    call_id: str
    tool_name: str
    decision: Literal["allow", "deny", "require_approval", "require_clarification"]
    reason: str
    risk_level: str
    approval_required: bool
    approval_payload: Optional[dict]
```

### 6.4 ToolInvocationRecord

```python
class ToolInvocationRecord(TypedDict):
    id: str
    call_id: str
    tool_name: str
    step_id: Optional[str]
    intent_id: Optional[str]
    args_digest: dict
    policy_decision: ToolCallPolicyDecision
    approval_id: Optional[str]
    started_at: str
    ended_at: Optional[str]
    status: Literal["pending", "running", "succeeded", "failed", "denied", "cancelled"]
    duration_ms: Optional[int]
    result_ref: Optional[str]
    observation_ids: list[str]
    error_type: Optional[str]
    error_message: Optional[str]
```

### 6.5 ToolExecutionResult

```python
class ToolExecutionResult(TypedDict):
    tool_call_id: str
    tool_name: str
    success: bool
    result_type: Literal[
        "query_result",
        "explain_plan",
        "schema_summary",
        "index_summary",
        "row_count_estimate",
        "lock_wait",
        "affected_rows",
        "sql_error",
        "tool_error",
        "policy_denied",
    ]
    summary: str
    payload: dict
    row_count: Optional[int]
    affected_rows: Optional[int]
    sqlstate: Optional[str]
    duration_ms: int
    truncated: bool
    sensitive_fields_masked: list[str]
```

## 7. 推荐 PostgreSQL 工具体系

```text
postgres_schema_inspect
  只读，读取 schema/table/column/view 摘要

postgres_index_inspect
  只读，读取索引、约束、使用情况

postgres_query_readonly
  只读，只允许 SELECT/SHOW/WITH 等安全查询

postgres_explain
  诊断，执行 EXPLAIN 或 EXPLAIN (FORMAT JSON)

postgres_lock_inspect
  只读，读取 pg_locks / pg_stat_activity 摘要

postgres_stats_inspect
  只读，读取统计信息、行数估算、pg_stat_statements 摘要

postgres_transaction_dry_run
  高风险前置工具，在事务中验证 SQL 形态和影响估算，不 commit

postgres_execute_write
  写工具，只能在审批通过后执行 DDL/DML/权限变更
```

## 8. 设计点与参考权衡

### 8.1 设计点一：工具注册从实例列表升级为带元数据的 Tool Catalog

**Codex 的做法**

Codex 的工具系统不是简单保存函数列表，而是通过 ToolRouter、ToolRegistry、ToolSpec 和 runtime handler 组合工具。工具有模型可见 spec，也有运行时 executor；同一个工具池可以包含内置工具、MCP 工具、动态工具和扩展工具。

**Claude 的做法**

Claude 的 Tool 类型包含丰富元数据和行为接口，例如 name、aliases、inputSchema、isEnabled、isReadOnly、isDestructive、checkPermissions、validateInput、renderToolUseMessage、maxResultSizeChars 等。工具不是裸函数，而是带描述、权限、显示、校验和结果映射的对象。

**最终方案**

mini-agent 保留当前自动发现机制，但注册结果不再只是 `BaseTool` 实例，而是 `ToolSpec + AgentTool`。`ToolSpec` 必须声明 capability、risk_level、read_only、requires_approval、allowed_phases、allowed_policies、output_type、plugin_source。

**权衡原因**

Codex 的路由和 runtime 分离适合多来源工具；Claude 的 Tool 接口适合表达每个工具自身的安全语义。mini-agent 作为数据库 Agent，需要在工具注册阶段就知道风险和能力。

**效果**

系统可以在绑定、审批、执行、审计和记忆沉淀时统一使用工具元数据，不需要到执行阶段靠工具名和字符串猜测风险。

### 8.2 设计点二：每轮动态构建工具池，而不是全局暴露所有工具

**Codex 的做法**

Codex 会根据 turn context 构建工具 router。不同模型能力、工具模式、MCP 状态、动态工具、扩展工具都会影响最终 model-visible specs。工具是否出现在模型请求里，是运行时计算结果。

**Claude 的做法**

Claude 通过 `getTools(permissionContext)`、`filterToolsByDenyRules`、`assembleToolPool` 动态过滤工具。被 deny rule 覆盖的工具不会出现在模型可见工具池里。

**最终方案**

mini-agent 增加 `ToolPoolBuilder`，输入为当前 `DBTaskIntent`、`TaskStep`、`StepContextPacket`、审批状态、SafetyMemory 和环境配置，输出本轮可绑定给 LLM 的工具列表。

```text
observe step:
  postgres_schema_inspect
  postgres_index_inspect
  postgres_query_readonly
  postgres_explain

diagnose/report step:
  no_tools 或只读上下文工具

execute step:
  postgres_execute_write 仅在审批通过后可见
```

**权衡原因**

Codex 证明工具可见性应由 turn context 决定；Claude 证明权限过滤应该在模型看到工具前发生。mini-agent 不能让模型在只读阶段看到写工具。

**效果**

减少危险误调用，降低 prompt 工具噪声，让模型更容易选择正确 PostgreSQL 工具。

### 8.3 设计点三：PostgreSQL 读工具和写工具彻底分离

**Codex 的做法**

Codex 对 shell 执行有审批和 sandbox，但 shell 仍是高自由度工具。它通过执行环境、审批和策略弥补通用命令工具的风险。

**Claude 的做法**

Claude 将 Bash、Read、Edit、Write 等能力拆成不同工具，并通过 `isReadOnly`、`isDestructive`、`checkPermissions` 等方法区分风险。

**最终方案**

mini-agent 不应只提供一个 `postgres_execute(sql)`。应拆成只读查询、EXPLAIN、schema inspect、index inspect、lock inspect、stats inspect、dry run、write execute 等多个工具。

**权衡原因**

Codex 的通用 shell 模式适合代码任务，但数据库管理更需要领域边界。Claude 的细粒度工具边界更适合 PostgreSQL 安全。

**效果**

工具名本身就传递风险。只读步骤可以完全不暴露写工具，写工具可以强制审批和事务控制。

### 8.4 设计点四：调用前统一经过 ToolCallPolicyGate

**Codex 的做法**

Codex 的 ToolOrchestrator 集中处理审批、sandbox 选择、执行尝试、失败后的升级或拒绝。工具执行不是直接调用 runtime，而是先经过统一编排。

**Claude 的做法**

Claude 的工具调用会经过 permission context、permission rules、tool-specific checkPermissions、hooks、classifier 等多层判断。工具自己也可以定义 validateInput 和 checkPermissions。

**最终方案**

mini-agent 增加独立 `ToolCallPolicyGate` 服务，取代散落在 `agent_loop.py` 中的策略判断。它输出 `ToolCallPolicyDecision`：

```text
allow
deny
require_approval
require_clarification
```

判断依据包括：

- 当前 step.tool_policy。
- ToolSpec.allowed_phases。
- ToolSpec.capability。
- SQL 静态分析结果。
- 当前审批状态。
- target_environment。
- SafetyMemory。
- 用户显式约束。

**权衡原因**

Codex 的集中 orchestrator 让审批和执行路径清晰；Claude 的 permission context 让规则可组合。mini-agent 需要二者结合，避免工具安全逻辑散落。

**效果**

新增工具时只需要声明 ToolSpec 和可选工具级校验，不需要到多个节点里复制安全判断。

### 8.5 设计点五：写工具必须走预览、审批、执行、验证协议

**Codex 的做法**

Codex 对需要权限的执行会生成 approval requirement，并可能缓存某些 session 范围内的审批，但审批绑定到具体工具请求或策略键，不是无限复用。

**Claude 的做法**

Claude 的 permission request 会把工具名、输入、规则来源和原因展示给用户。工具权限由当前 permission context 和当前调用共同决定。

**最终方案**

mini-agent 的 `postgres_execute_write` 必须要求：

```text
sql_preview
target_environment
impact_summary
rollback_plan
expected_affected_rows
approval_id
```

审批只对当前 step、当前 SQL 摘要、当前环境和当前回滚方案有效。旧审批只能作为风险记忆，不能自动批准新 SQL。

**权衡原因**

Codex 提供审批生命周期和缓存思路，Claude 提供用户可理解的 permission prompt。数据库写操作强依赖 SQL 内容、环境、影响范围和回滚方案，因此审批必须绑定这些字段。

**效果**

用户不会因为批准“分析慢 SQL”而间接批准“执行 CREATE INDEX”。系统也能在审计记录中还原审批和执行的对应关系。

### 8.6 设计点六：SQL 参数必须静态分析和强校验

**Codex 的做法**

Codex 对 shell 命令有规范化、sandbox 和审批策略，不仅把命令当普通字符串执行。

**Claude 的做法**

Claude 工具使用 inputSchema 和 validateInput，工具可以在执行前拒绝非法输入。

**最终方案**

mini-agent 的 PostgreSQL 工具参数使用 Pydantic schema 校验，同时增加 SQL classifier：

- 只读工具只允许 `SELECT`、`WITH`、`SHOW`、`EXPLAIN`。
- 禁止多语句。
- 禁止 `COPY TO PROGRAM`、`DO`、危险函数、DDL/DML。
- 写工具必须提供 approval_id。
- 生产环境写操作必须标记 critical 或 high。
- 无 WHERE 的 UPDATE/DELETE 默认 critical 并要求确认。

**权衡原因**

Codex 的命令规范化提醒我们不能裸执行模型字符串；Claude 的 schema/validateInput 适合放在工具边界。PostgreSQL SQL 需要专门分类。

**效果**

大量错误或危险调用会在数据库连接前被拒绝，减少误写、锁表和越权风险。

### 8.7 设计点七：工具执行环境内置数据库安全默认值

**Codex 的做法**

Codex 通过 sandbox manager、filesystem/network policy、approval policy 控制工具执行环境。执行是否 sandbox、是否允许网络、是否需要提升权限由工具编排器决定。

**Claude 的做法**

Claude 的 ToolUseContext 带有 permission context、working directories、abortController、AppState、tool decisions 等运行时上下文，工具执行不是脱离上下文的函数调用。

**最终方案**

mini-agent PostgreSQL adapter 默认设置：

```text
read-only transaction for read tools
statement_timeout
lock_timeout
idle_in_transaction_session_timeout
row limit / result truncation
masked sensitive fields
explicit transaction for writes
rollback on failure
```

生产环境默认只读，除非当前步骤、审批和策略都允许写。

**权衡原因**

Codex 的 sandbox 思路说明执行环境是安全边界；Claude 的 ToolUseContext 说明工具必须读取当前权限上下文。数据库 Agent 也要把安全默认值放在执行层。

**效果**

即使模型输出了有问题的 SQL，工具层也能限制运行时间、锁等待、返回规模和写入边界。

### 8.8 设计点八：工具结果从工具层开始结构化

**Codex 的做法**

Codex 的 exec 输出不是纯文本，而是带 stdout、stderr、exit code、duration、sandbox error 等结构化信息，并转换为模型可消费的 ResponseInputItem。

**Claude 的做法**

Claude 的 ToolResult 会包含 data、newMessages、mcpMeta，并且工具提供 mapToolResultToToolResultBlockParam、renderToolResultMessage 等结果映射方法。

**最终方案**

mini-agent 工具返回 `ToolExecutionResult`，再转换成 `DBObservation` 和 `ResultDigest`。文本摘要只是展示层，结构化字段才是后续验证和记忆的依据。

**权衡原因**

Codex 强调运行结果的机器可读性；Claude 强调结果既要给模型，也要给 UI 和 transcript。mini-agent 需要同时支持 Agent Loop 验证、CLI 展示、长期记忆和审计。

**效果**

验证节点不必再从自然语言里猜“这是 EXPLAIN 还是错误”。报告可以引用精确证据，记忆系统只沉淀脱敏摘要。

### 8.9 设计点九：工具错误分类，而不是统一字符串失败

**Codex 的做法**

Codex 区分 sandbox denial、exit code、timeout、network denial、approval rejection 等错误类型。

**Claude 的做法**

Claude 的 permission result 和 validation result 会区分 deny、ask、allow、validation error，并把原因反馈给模型。

**最终方案**

mini-agent 定义数据库工具错误类型：

```text
policy_denied
approval_required
connection_error
permission_denied
timeout
lock_timeout
syntax_error
constraint_violation
serialization_failure
deadlock_detected
result_too_large
tool_error
```

**权衡原因**

Codex 的错误分类适合执行层重试和降级；Claude 的 permission/validation 分类适合告诉模型下一步怎么办。PostgreSQL 错误需要被 Agent Loop 识别。

**效果**

Agent 可以对 lock_timeout 改成锁诊断，对 syntax_error 让模型修 SQL，对 permission_denied 请求用户确认权限，对 policy_denied 直接停止。

### 8.10 设计点十：工具调用必须有审计记录

**Codex 的做法**

Codex 的工具调用有 begin/end 事件、telemetry、approval decision、sandbox attempt 和 tool output。执行过程可以被追踪。

**Claude 的做法**

Claude 的工具调用会产生 tool_use、tool_result、progress message，并维护 tool decisions、permission decisions 等上下文。

**最终方案**

mini-agent 增加 `ToolInvocationRecord`，每次调用都记录：

- tool_call_id。
- tool_name。
- step_id / intent_id。
- args_digest。
- policy_decision。
- approval_id。
- started_at / ended_at。
- duration。
- status。
- observation_ids。
- error_type。

**权衡原因**

数据库管理需要可追责。Codex 和 Claude 都把工具事件当成一等事件，而不是隐藏在日志里。

**效果**

用户可以知道 Agent 具体执行过什么，出问题时可以审计，也能为最终报告提供证据链。

### 8.11 设计点十一：插件工具需要 manifest 和能力声明

**Codex 的做法**

Codex 支持 MCP、dynamic tools、extension tool executors，并通过 ToolSpec 和 ToolExecutor 统一进入 ToolRouter。

**Claude 的做法**

Claude 支持 MCP tools，并在 assembleToolPool 中和内置工具合并，再通过 deny rules 过滤和去重。

**最终方案**

mini-agent 保留 `plugins/`，但插件必须声明 manifest：

```json
{
  "name": "postgres_pg_stat_statements",
  "domain": "postgresql",
  "operation_type": "diagnostic",
  "risk_level": "low",
  "read_only": true,
  "requires_approval": false,
  "allowed_phases": ["observe", "diagnose"],
  "output_type": "query_result"
}
```

没有 manifest 的插件默认只作为普通工具，不能访问 PostgreSQL 写能力。

**权衡原因**

Codex 的扩展工具模型适合开放生态；Claude 的 deny/filter 模型适合管控外部工具。mini-agent 需要可扩展但默认保守。

**效果**

未来可以接入监控、备份、工单、告警平台，但每个插件都受统一 ToolPolicy 约束。

### 8.12 设计点十二：工具描述要包含边界，而不是只介绍功能

**Codex 的做法**

Codex 的系统提示和工具规范会告诉模型 shell 使用约束、sandbox 和审批边界。

**Claude 的做法**

Claude 的工具 prompt/description 会说明工具适用场景、输入要求、权限行为和用户可见显示。

**最终方案**

mini-agent 的工具 description 应包含：

- 这个工具什么时候用。
- 这个工具什么时候不能用。
- 是否只读。
- 是否执行 SQL。
- 是否需要审批。
- 返回规模限制。
- 结果是否脱敏。

例如 `postgres_query_readonly` 的描述必须明确“只允许 SELECT/SHOW/WITH，不允许 DDL/DML，不返回完整大结果集”。

**权衡原因**

Codex 和 Claude 都不是只依赖后置拦截，也会在工具描述里引导模型。mini-agent 需要让模型在选择工具前就理解边界。

**效果**

减少无效工具调用和策略拦截次数，让模型更自然地遵守数据库安全流程。

### 8.13 设计点十三：并发执行要按工具能力控制

**Codex 的做法**

Codex 的 ToolRouter 可以判断工具是否支持 parallel tool calls，以及工具取消时是否等待 runtime 清理。

**Claude 的做法**

Claude 的 Tool 定义有 `isConcurrencySafe` 和 interruptBehavior。工具可以声明是否并发安全、用户新输入时取消还是阻塞。

**最终方案**

mini-agent 的 ToolSpec 增加 `supports_parallel` 和 `interrupt_behavior`：

```text
schema inspect / index inspect:
  可并发

readonly query:
  谨慎并发，受连接池和 statement_timeout 限制

write execute:
  不可并发

backup / restore:
  不可并发，用户新输入默认 block
```

**权衡原因**

Codex 和 Claude 都明确工具并发不是全局默认能力。数据库操作尤其不能让多个写工具并发执行。

**效果**

提升读诊断效率，同时避免并发写入、长事务互相影响和连接池耗尽。

### 8.14 设计点十四：工具搜索和延迟加载作为后续扩展

**Codex 的做法**

Codex 支持 discoverable tools 和 tool_search，部分工具可以不在初始工具列表里完整暴露。

**Claude 的做法**

Claude 的 Tool 有 `shouldDefer`、`alwaysLoad`、`searchHint`。工具太多时，可以先让模型搜索工具，再加载具体工具。

**最终方案**

mini-agent 第一阶段不必实现完整 tool_search，但 ToolSpec 预留：

```text
search_hint
defer_loading
always_load
```

PostgreSQL 核心工具 always_load；低频外部插件可以 defer。

**权衡原因**

当前 mini-agent 工具数量不大，直接实现 tool_search 成本较高。但预留字段可以避免后续重构。

**效果**

未来插件增多后，可以控制 prompt 体积，同时让模型通过搜索发现低频工具。

### 8.15 设计点十五：shell 工具不能作为 PostgreSQL 管理主路径

**Codex 的做法**

Codex 强 shell 能力是核心能力，并通过 sandbox、审批、环境策略控制风险。

**Claude 的做法**

Claude 也有 Bash，但对文件读写、搜索、任务、MCP 等提供专用工具，避免所有行为都走 shell。

**最终方案**

mini-agent 可以保留 shell 工具用于开发和运维辅助，但 PostgreSQL 管理主路径必须走专用 PostgreSQL tools。`psql`、`pg_dump`、`pg_restore` 这类 shell 调用如果被模型请求，应进入高风险策略判断，必要时拒绝并建议使用专用工具。

**权衡原因**

Codex 的 shell 模式适合编程环境，但数据库 Agent 需要更细的事务、超时、脱敏、审计和审批控制。Claude 的专用工具思路更适合这里。

**效果**

数据库操作不会绕过 ToolSpec、ToolPolicy、ResultNormalizer 和审计记录。

## 9. 工具注册流程

推荐流程：

```text
registry.discover()
  -> load BaseTool / AgentTool
  -> load tool manifest / class metadata
  -> build ToolSpec
  -> validate ToolSpec
  -> register into ToolCatalog
```

注册校验规则：

1. 工具名唯一。
2. PostgreSQL 写工具必须 `requires_approval=true`。
3. destructive 工具必须 `risk_level` 至少 high。
4. read_only 工具不能声明写操作类型。
5. plugin 工具缺少 manifest 时默认高保守权限。
6. output_type 必须可映射为 ToolExecutionResult 或 DBObservation。

## 10. 工具绑定流程

推荐流程：

```text
llm_reason
  -> build_tool_pool(state)
  -> bind_tools(visible_tools)
  -> LLM emits tool_calls
```

`build_tool_pool(state)` 使用：

1. 当前 intent。
2. 当前 step。
3. step.tool_policy。
4. expected_tools。
5. SafetyMemory。
6. approval_decisions。
7. target_environment。
8. user_constraints。

输出：

```text
visible_tools:
  只包含当前步骤允许模型看到的工具

dispatchable_tools:
  包含可被执行器识别但不一定暴露给模型的工具
```

## 11. 工具调用流程

推荐流程：

```text
LLM tool_call
  -> ToolCallPolicyGate
  -> ToolCallPolicyDecision
  -> require_approval?
      -> HumanApproval
      -> approval_id
  -> ToolExecutor
  -> ToolExecutionResult
  -> ResultNormalizer
  -> DBObservation
  -> VerifyStep
```

写操作流程：

```text
propose SQL
  -> preview + impact + rollback
  -> request approval
  -> execute exact approved SQL
  -> verify
  -> report
```

## 12. 工具结果归一化流程

推荐流程：

```text
ToolExecutionResult
  -> mask sensitive fields
  -> truncate large rows
  -> classify result_type
  -> create DBObservation
  -> create ResultDigest
  -> attach evidence_ids to VerificationResult
```

注意：

1. 原始大结果集不进入长期记忆。
2. PII 字段默认脱敏。
3. SQL 错误保留 SQLSTATE 和摘要，不保留敏感参数。
4. EXPLAIN 结果可以保留结构化 JSON 摘要。

## 13. 与现有模块的关系

### 13.1 与任务理解

任务理解输出 intent 后，工具模块根据 domain、operation_nature、risk_level 和 target_environment 决定工具候选池。

### 13.2 与规划

规划模块中的 `expected_tools` 和 `tool_policy` 应成为 ToolPoolBuilder 的输入，不能只用于展示。

### 13.3 与 Agent Loop

Agent Loop 负责调度步骤，但工具授权、工具可见性和调用策略应下沉到 ToolCallPolicyGate。

### 13.4 与安全护栏

SafetyMemory、用户约束、审批记录和环境策略都应进入工具调用前检查。

### 13.5 与上下文管理

工具结果不能直接全部进入 prompt。必须经过 ResultDigest 和 Context Builder。

### 13.6 与记忆系统

只有验证通过后的工具观察结果可以生成 MemoryCandidate，且必须经过 memory_write_gate。

## 14. 实施计划

### 阶段一：ToolSpec 和 ToolCatalog

1. 增加 `ToolCapability`、`ToolSpec`、`ToolCallPolicyDecision`、`ToolInvocationRecord`、`ToolExecutionResult`。
2. 扩展 `AgentTool`，允许工具声明 capability metadata。
3. 扩展 `SkillRegistry`，注册时生成 ToolCatalog。

验收标准：

- 每个工具都有能力和风险元数据。
- 注册缺少必要 metadata 的 PostgreSQL 工具会失败或降级。

### 阶段二：ToolPoolBuilder

1. 根据 state 构建本轮可见工具。
2. `llm_reason` 不再直接 `registry.get_all()`。
3. 计划步骤的 `expected_tools` 和 `tool_policy` 影响工具可见性。

验收标准：

- read_only step 不暴露写工具。
- report step 默认不暴露数据库执行工具。
- 审批前不暴露 `postgres_execute_write`。

### 阶段三：ToolCallPolicyGate 独立化

1. 从 `agent_loop.py` 中抽离策略判断。
2. 工具调用前输出结构化 `ToolCallPolicyDecision`。
3. 支持 allow、deny、require_approval、require_clarification。

验收标准：

- 写 SQL 未审批时被 require_approval。
- SafetyMemory 禁令优先级高于普通 approval。
- 计划外高风险工具被拒绝或触发 replan。

### 阶段四：PostgreSQL 专用工具

1. 增加只读 schema/index/query/explain/lock/stats 工具。
2. 增加 `postgres_transaction_dry_run`。
3. 增加 `postgres_execute_write`。
4. 所有 PostgreSQL 工具返回 `ToolExecutionResult`。

验收标准：

- 只读工具不能执行 DDL/DML。
- 写工具必须有 approval_id。
- 工具结果可转成 DBObservation。

### 阶段五：审计与 CLI 展示

1. 增加 `tool_invocation_records` 状态字段。
2. CLI 展示工具调用决策、审批要求、结果摘要。
3. report 阶段引用关键 invocation 和 evidence。

验收标准：

- 每次工具调用都能追踪到 step、approval 和 observation。
- 用户能看到工具被拒绝或要求审批的具体原因。

### 阶段六：插件 manifest 和外部工具

1. 插件工具支持 manifest。
2. 缺少 manifest 的插件默认只读或不可用于 PostgreSQL。
3. 支持 future MCP adapter，但统一进入 ToolCatalog 和 ToolPolicyGate。

验收标准：

- 外部工具不能绕过权限过滤。
- 插件工具可按 capability 被动态暴露或隐藏。

## 15. 最终取舍总结

Codex 值得借鉴的是：

- ToolRouter 将模型可见工具和运行时执行器分开。
- ToolRegistry 支持多来源工具。
- ToolOrchestrator 集中处理审批、sandbox、执行和重试。
- MCP / dynamic tools 可以统一接入。
- 工具调用事件和结果结构化。

Claude 值得借鉴的是：

- Tool 接口包含完整元数据和行为方法。
- PermissionContext 决定工具可见性和可用性。
- `getTools` / `assembleToolPool` 先过滤再暴露给模型。
- 工具级 validateInput / checkPermissions。
- `isReadOnly`、`isDestructive`、`isConcurrencySafe` 等能力声明。
- 工具结果、进度和 UI 展示分层。

mini-agent 的最终方案是：

```text
ToolCatalog
  -> ToolPoolBuilder
  -> LLM bind_tools
  -> ToolCallPolicyGate
  -> ApprovalGate
  -> ToolExecutor
  -> ToolExecutionResult
  -> DBObservation / ResultDigest / MemoryCandidate
```

这个方案避免两种极端：

1. 不把所有工具全量暴露给模型，降低误调用风险。
2. 不把安全逻辑写死在单个节点里，方便后续扩展 PostgreSQL 专用工具、插件工具和外部 MCP 工具。

对 PostgreSQL 管理智能体来说，工具系统的目标不是“能执行更多命令”，而是“在正确任务阶段，以正确权限，执行正确范围内的数据库操作，并留下可验证证据”。
