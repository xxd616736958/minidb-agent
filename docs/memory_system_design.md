# PostgreSQL 管理智能体：记忆系统模块设计

## 1. 背景

mini-agent 当前已有三层记忆雏形：

```text
short_term
  最近消息窗口

working_memory
  当前会话里的 key-value 工作记忆

long_term_refs
  长期记忆引用
```

这对通用终端 Agent 是一个不错起点，但如果要把 mini-agent 优化为 PostgreSQL 数据库管理智能体，记忆系统必须更加谨慎。数据库 Agent 会接触：

- 数据库结构。
- 查询结果。
- 慢 SQL。
- EXPLAIN。
- 权限信息。
- 审批记录。
- 用户安全偏好。
- 生产环境信息。
- 可能包含 PII 的业务数据。

这些信息不能简单地全部写进长期记忆。记忆系统需要回答三个问题：

1. 什么值得记？
2. 什么不能记？
3. 什么时候、以什么作用域、带着什么证据和有效期去记？

## 2. 模块定位

上下文管理解决的是“当前任务怎么组织给模型看”，记忆系统解决的是“跨步骤、跨会话、跨任务，哪些信息可以安全复用”。

推荐关系：

```text
Agent Loop / Final Report
  -> Memory Candidate
  -> memory_write_gate
  -> MemoryRecord Store
  -> memory_read_gate
  -> Context Builder / Planner / Tool Policy
```

记忆系统不直接替代上下文。它只提供可复用知识，进入当前上下文前仍要经过过滤。

## 3. 设计目标

1. 记住用户偏好、数据库非敏感结构摘要、历史诊断经验和安全禁令。
2. 默认不长期保存凭证、token、PII、原始大结果集和敏感业务数据。
3. 记忆写入必须经过筛选、脱敏、作用域判断和可信度判断。
4. 记忆检索必须结合当前 intent、step、数据库对象、环境和风险等级。
5. 记忆不能绕过当前审批。
6. 记忆需要支持过期、更新、冲突处理和遗忘。
7. 长期记忆存储要和 checkpoint 状态分离。

## 4. 非目标

1. 记忆系统不作为数据库事实的唯一来源。
2. 记忆系统不替代实时 schema / statistics 查询。
3. 记忆系统不保存完整查询结果集。
4. 记忆系统不自动复用旧审批。
5. 记忆系统不把模型推理过程直接写入长期记忆。

## 5. 推荐记忆分层

```text
UserPreferenceMemory
  报告格式、语言偏好、默认输出风格

SafetyMemory
  默认只读、禁止 DROP/TRUNCATE、生产库必须审批

DBSchemaMemory
  非敏感 schema/index/row_count 摘要

TaskEpisodeMemory
  一次诊断或迁移任务的结论和证据引用

OptimizationMemory
  慢 SQL、索引、统计信息相关的经验

ProjectMemory
  项目级数据库命名习惯、环境别名、常用 schema
```

## 6. 推荐数据结构

### 6.1 MemoryRecord

```python
class MemoryRecord(TypedDict):
    id: str
    kind: Literal[
        "fact",
        "preference",
        "experience",
        "assumption",
        "prohibition",
        "schema_summary",
        "task_episode",
    ]
    scope: Literal["user", "project", "database", "schema", "session", "task"]
    namespace: str
    summary: str
    payload: dict
    source: Literal[
        "user_confirmed",
        "tool_observed",
        "agent_inferred",
        "report_generated",
        "system_policy",
    ]
    evidence_refs: list[str]
    confidence: float
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    ttl_seconds: Optional[int]
    observed_at: str
    expires_at: Optional[str]
    supersedes: list[str]
    status: Literal["active", "deprecated", "expired", "conflicted"]
```

### 6.2 MemoryCandidate

```python
class MemoryCandidate(TypedDict):
    id: str
    proposed_record: MemoryRecord
    reason: str
    requires_user_confirmation: bool
    write_decision: Literal["pending", "approved", "rejected", "auto_write"]
```

### 6.3 MemoryQuery

```python
class MemoryQuery(TypedDict):
    intent_type: str
    step_phase: str
    target_environment: str
    target_database: Optional[str]
    target_objects: list[str]
    risk_level: str
    allowed_scopes: list[str]
    max_sensitivity: Literal["public", "internal", "sensitive"]
```

## 7. 设计点与参考权衡

### 7.1 设计点一：记忆分层，而不是一个混合长期向量库

**Codex 的做法**

Codex 的长期能力更偏向用户偏好、项目指令、技能和可复用环境知识，不会把所有执行过程都混进一个无限长期记忆。

**Claude 的做法**

Claude 更强调 task/todo/AppState 和权限上下文，把任务状态、协作状态和工具权限结构化管理，而不是只依赖向量召回。

**最终方案**

mini-agent 将记忆分为 UserPreference、Safety、DBSchema、TaskEpisode、Optimization、Project 等类别，每类有不同写入规则和检索规则。

**权衡原因**

Codex 提供谨慎长期记忆思路，Claude 提供任务状态结构化思路。数据库 Agent 需要把“可长期复用”和“当前任务状态”分清。

**效果**

用户偏好不会和 schema 事实混在一起，安全禁令也不会被普通经验记忆淹没。

### 7.2 设计点二：写入必须经过 memory_write_gate

**Codex 的做法**

Codex 对长期记忆写入较谨慎，通常只保存稳定、可复用、用户允许或系统明确需要的信息。

**Claude 的做法**

Claude 把权限、任务状态和普通对话区分开，敏感决策不会随意变成普通记忆。

**最终方案**

mini-agent 增加 `memory_write_gate`，写入前判断：

- 是否稳定。
- 是否可复用。
- 是否敏感。
- 是否有证据。
- 是否需要用户确认。
- 是否有合适作用域。

**权衡原因**

数据库工具结果可能包含敏感数据，不能自动长期保存。

**效果**

查询结果、密码、token、PII 默认不写入长期记忆；用户偏好和安全禁令可以安全沉淀。

### 7.3 设计点三：长期记忆存结论和索引，不存完整原始数据

**Codex 的做法**

Codex 的记忆更偏摘要和可复用事实，而不是保存完整执行日志。

**Claude 的做法**

Claude 的工具结果和任务状态会被摘要或结构化，而不是无边界保留所有输出。

**最终方案**

mini-agent 的 `MemoryRecord` 保存 `summary`、`payload`、`evidence_refs`、`confidence`，不保存完整 result set。原始证据留在当前任务状态或审计存储中，长期记忆只保留引用。

**权衡原因**

长期记忆应支持未来推理，但不应成为敏感数据仓库。

**效果**

Agent 能记住“orders 慢查询曾由缺少复合索引导致”，但不会保存完整订单数据。

### 7.4 设计点四：按当前 intent 和 step 检索记忆

**Codex 的做法**

Codex 会根据当前任务和工作目录读取相关上下文，不会盲目加载全部历史知识。

**Claude 的做法**

Claude 的 task/todo 驱动让系统围绕当前任务状态组织信息。

**最终方案**

mini-agent 用 `MemoryQuery` 检索记忆：

```text
DBTaskIntent
current_step.phase
target_objects
risk_level
target_environment
allowed_scopes
```

**权衡原因**

不同阶段需要不同记忆。审批阶段需要安全和历史拒绝经验；诊断阶段需要 schema 和优化经验；报告阶段需要用户格式偏好。

**效果**

检索结果更准，减少无关记忆污染 prompt。

### 7.5 设计点五：记忆必须有作用域

**Codex 的做法**

Codex 区分用户级、项目级、工作区级信息，例如项目指令不应随意套到所有项目。

**Claude 的做法**

Claude 的会话、任务和工作区状态也有明确边界。

**最终方案**

mini-agent 的每条 `MemoryRecord` 必须带 `scope`：

```text
user
project
database
schema
session
task
```

**权衡原因**

数据库知识很容易跨环境误用。staging 的 schema 不一定等于 production。

**效果**

检索时可以避免把测试库事实当成生产库事实。

### 7.6 设计点六：区分事实、偏好、经验、假设、禁令

**Codex 的做法**

Codex 在执行中倾向区分观察到的信息、用户要求和推断。

**Claude 的做法**

Claude 的 task/permission 状态把用户确认和模型生成内容分开。

**最终方案**

`MemoryRecord.kind` 明确区分：

```text
fact
preference
experience
assumption
prohibition
schema_summary
task_episode
```

**权衡原因**

数据库任务不能把经验当事实，也不能把假设当用户确认。

**效果**

LLM 使用记忆时能更谨慎，例如把“可能需要复合索引”当经验，而不是已验证事实。

### 7.7 设计点七：记忆必须有可信度和证据来源

**Codex 的做法**

Codex 强调先观察再行动，事实应来自可追溯上下文。

**Claude 的做法**

Claude 的任务系统和工具结果可以提供完成状态和证据链。

**最终方案**

每条记忆保存：

- `source`
- `evidence_refs`
- `confidence`
- `observed_at`

来源可为：

```text
user_confirmed
tool_observed
agent_inferred
report_generated
system_policy
```

**权衡原因**

没有来源的数据库记忆容易过期或误用。

**效果**

Agent 检索到记忆时可以判断“这是用户确认的安全偏好”还是“模型推断的优化经验”。

### 7.8 设计点八：schema 记忆必须有 TTL

**Codex 的做法**

Codex 不应把动态环境状态当永久事实。

**Claude 的做法**

Claude 的任务状态会随着执行更新，而不是把旧状态永久有效化。

**最终方案**

`DBSchemaMemory` 必须有：

```text
observed_at
ttl_seconds
expires_at
target_environment
target_database
```

过期后只能作为历史参考，不能作为当前事实。

**权衡原因**

schema、索引、行数和统计信息都会变化。

**效果**

Agent 不会因为几天前的 schema 记忆而跳过实时检查。

### 7.9 设计点九：SafetyMemory 优先级最高

**Codex 的做法**

Codex 的安全护栏和权限边界优先于普通任务便利性。

**Claude 的做法**

Claude 的 permission context 会优先影响工具调用。

**最终方案**

`SafetyMemory` 在检索结果中优先级最高，并进入：

- intent validator
- planner validator
- tool_policy_gate
- approval_gate

**权衡原因**

用户安全禁令必须压过历史经验和模型建议。

**效果**

跨会话记住“生产库默认只读”“禁止 DROP/TRUNCATE”。

### 7.10 设计点十：审批历史可记忆，但不能复用成审批

**Codex 的做法**

Codex 的审批是具体 turn/action 的状态，不应被旧审批自动替代。

**Claude 的做法**

Claude 的 permission request 需要围绕当前工具调用和上下文决策。

**最终方案**

mini-agent 可以记住审批经验，例如：

```text
用户曾拒绝无 WHERE DELETE
用户要求 CREATE INDEX 前必须看到影响评估
```

但旧 approval 不能让新 SQL 自动执行。

**权衡原因**

数据库审批强依赖 SQL、环境、影响行数和回滚方案。

**效果**

记忆会提高风险判断质量，但不会绕过实时审批。

### 7.11 设计点十一：检索结果进入 prompt 前要二次过滤

**Codex 的做法**

Codex 对上下文注入有选择，不会把所有历史信息都注入当前 turn。

**Claude 的做法**

Claude 的 task 状态只展示和当前任务相关的信息。

**最终方案**

增加 `memory_read_gate`，根据以下字段过滤：

- scope
- sensitivity
- ttl
- confidence
- current intent
- step phase
- target environment

**权衡原因**

长期记忆可能过期、跨作用域或敏感。

**效果**

减少旧记忆污染推理，也降低敏感信息暴露。

### 7.12 设计点十二：写入发生在验证后或任务结束后

**Codex 的做法**

Codex 更适合在任务完成或上下文压缩时沉淀稳定信息，而不是每轮都记。

**Claude 的做法**

Claude 的 todo/task 完成状态可以作为写入记忆的触发条件。

**最终方案**

mini-agent 在以下时机触发 `memory_consolidator`：

```text
verify_step passed
final_report generated
task_completed
user explicitly says remember
```

**权衡原因**

中间推理可能是错的，过早写入会污染长期记忆。

**效果**

长期记忆更干净，更多来自验证过的事实和用户确认。

### 7.13 设计点十三：支持遗忘、更新和冲突处理

**Codex 的做法**

Codex 的记忆应能被更新和覆盖，避免旧偏好长期污染行为。

**Claude 的做法**

Claude 的任务状态会被持续更新，冲突状态不会简单并存。

**最终方案**

`MemoryRecord` 支持：

```text
supersedes
status=deprecated
status=expired
status=conflicted
conflict_resolution
```

**权衡原因**

数据库结构会变，用户偏好会变，旧经验会失效。

**效果**

新观察和旧记忆冲突时，旧记忆会被标记而不是继续注入 prompt。

### 7.14 设计点十四：长期记忆与 checkpoint 分离

**Codex 的做法**

Codex 区分会话状态、turn 状态和长期可复用记忆。

**Claude 的做法**

Claude 区分 AppState/task state 和更长期的配置/偏好。

**最终方案**

mini-agent 使用：

```text
LangGraph checkpoint:
  当前任务恢复状态

Memory Store:
  跨任务长期 MemoryRecord
```

**权衡原因**

checkpoint 和长期记忆生命周期不同。

**效果**

恢复任务和跨任务学习不会互相污染。

### 7.15 设计点十五：final report 生成 MemoryCandidate

**Codex 的做法**

Codex 对长期记忆写入应谨慎，适合生成候选后再确认。

**Claude 的做法**

Claude 的用户确认和任务完成状态很适合判断是否应该沉淀记忆。

**最终方案**

final report 后生成 `MemoryCandidate`：

```text
是否记住用户偏好 Markdown 报告？
是否记住 production 默认只读？
是否记住本次慢 SQL 诊断经验？
```

高敏感或高影响记忆需要用户确认。

**权衡原因**

让系统能学习，但不擅自保存用户不想保存的信息。

**效果**

记忆系统可控、透明、可审计。

## 8. 记忆读写流程

### 8.1 写入流程

```text
verify_step / final_report
  -> memory_consolidator
  -> MemoryCandidate
  -> memory_write_gate
  -> user confirmation if needed
  -> MemoryRecord store
```

### 8.2 读取流程

```text
DBTaskIntent + current_step + target_objects
  -> MemoryQuery
  -> memory_store.search
  -> memory_read_gate
  -> ranked MemoryRecord
  -> Context Builder
```

## 9. 安全策略

默认禁止长期保存：

- 数据库密码。
- token。
- 连接串。
- 原始大结果集。
- 未脱敏 PII。
- 生产用户数据。
- 临时审批 payload 的完整敏感值。

默认允许保存：

- 用户偏好。
- 用户安全禁令。
- 非敏感 schema 摘要。
- 已验证诊断结论。
- 报告格式偏好。
- 环境别名，但不包含凭证。

需要用户确认后保存：

- 项目级数据库约定。
- 生产环境默认策略。
- 长期优化经验。
- 审批偏好。

## 10. 与现有模块的关系

### 10.1 与上下文管理

记忆检索结果必须先进入 context builder，再由 token budget 和 sensitivity 过滤后进入 prompt。

### 10.2 与任务理解

任务理解可读取用户偏好、安全禁令和项目记忆，但不能读取过期 schema 作为当前事实。

### 10.3 与规划

规划可读取 SafetyMemory、OptimizationMemory 和 DB schema 摘要，但高风险计划仍必须实时验证。

### 10.4 与 Agent Loop

Agent Loop 在 `verify_step` 后可产生 MemoryCandidate；不能在未验证步骤后写长期记忆。

### 10.5 与审批

审批历史可作为风险提示，但不能自动批准当前操作。

## 11. 实施计划

### 阶段一：MemoryRecord schema 和 gates

1. 增加 `MemoryRecord`、`MemoryCandidate`、`MemoryQuery`。
2. 增加 `memory_write_gate`。
3. 增加 `memory_read_gate`。
4. 支持 sensitivity、scope、kind、source、confidence。

验收标准：

- 敏感数据不会写入长期记忆。
- 用户偏好可以生成候选记忆。

### 阶段二：Memory Store

1. 建立独立 long-term memory store。
2. 支持 namespace 和 scope。
3. 支持 search / upsert / deprecate。

验收标准：

- checkpoint 和长期记忆分离。
- 可按 user/project/database 检索。

### 阶段三：任务结束后记忆沉淀

1. `verify_step passed` 触发候选。
2. `final_report` 触发候选。
3. 用户确认后写入。

验收标准：

- 未验证推断不进入长期记忆。
- 用户确认偏好可跨会话复用。

### 阶段四：TTL 和冲突处理

1. schema memory 增加 TTL。
2. 过期记忆降级为历史参考。
3. 新旧冲突时标记 conflicted/deprecated。

验收标准：

- 过期 schema 不作为当前事实。
- 冲突记忆不会同时注入 prompt。

### 阶段五：记忆接入上下文和工具策略

1. context builder 读取 memory_read_gate 结果。
2. SafetyMemory 接入 planner/tool_policy_gate。
3. final report 展示使用过的关键记忆。

验收标准：

- “生产库默认只读”跨会话生效。
- 记忆不会绕过审批。

## 12. 最终取舍总结

Codex 值得借鉴的是：

- 长期记忆应谨慎写入。
- 动态环境状态不能当永久事实。
- 会话状态和长期记忆要分离。
- 安全边界优先于便利性。

Claude 值得借鉴的是：

- task/todo/AppState 式结构化任务状态。
- permission context。
- 用户确认和权限决策结构化。
- 长任务状态可更新。

mini-agent 的最终方案是：

```text
MemoryCandidate
  -> memory_write_gate
  -> scoped MemoryRecord
  -> memory_read_gate
  -> context builder
  -> planner / agent loop / tool policy
```

这个方案避免两种极端：

- 不把所有数据库输出都长期保存，避免敏感数据污染。
- 不完全失忆，能够跨会话复用用户偏好、安全禁令和已验证经验。

最终目标是让 mini-agent 形成一个安全、可控、可审计的 PostgreSQL 任务记忆系统。

