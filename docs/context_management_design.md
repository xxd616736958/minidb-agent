# PostgreSQL 管理智能体：上下文管理模块设计

## 1. 背景

mini-agent 当前已经具备：

- `DBTaskIntent`：结构化任务理解。
- `DBTaskPlan` / `TaskStep`：结构化规划。
- `DBObservation`：结构化工具观察。
- `ApprovalDecision`：审批记录。
- `VerificationResult`：步骤验证结果。

但是如果上下文管理仍然停留在“聊天历史 + 简单记忆 + 全量工具结果塞进 prompt”的模式，PostgreSQL 管理任务会出现几个问题：

1. 长任务中模型容易忘记当前步骤和用户约束。
2. EXPLAIN、schema、索引、影响行数等证据会混在自然语言历史里，难以复用。
3. 大结果集会撑爆上下文窗口。
4. 审批记录、SQL、回滚方案不能被随意摘要。
5. 敏感信息可能被长期保存或重复注入 prompt。

因此，上下文管理模块需要从“聊天上下文管理”升级为“数据库任务上下文系统”。

## 2. 模块定位

上下文管理模块位于所有核心模块之间：

```text
用户输入
  -> 任务理解需要上下文
  -> 规划需要上下文
  -> Agent Loop 每步需要上下文
  -> 工具策略需要上下文
  -> 最终报告需要上下文
```

它不是单独的一个节点，而是一组能力：

- 收集上下文。
- 结构化保存上下文。
- 压缩上下文。
- 检索当前步骤需要的上下文。
- 控制哪些上下文进入 prompt。
- 控制哪些上下文参与工具权限判断。
- 控制哪些上下文可以长期保存。

## 3. 设计目标

1. 让模型每轮只看到当前步骤需要的信息。
2. 让数据库证据结构化保存，而不是埋在聊天历史里。
3. 让用户约束、审批、风险等级成为高优先级上下文。
4. 长任务中支持压缩、恢复和审计。
5. 对 SQL、EXPLAIN、审批和敏感数据采用不同压缩策略。
6. 防止凭证、敏感数据和生产库细节进入长期记忆。
7. 让上下文同时服务 prompt、tool policy、planner、final report。

## 4. 非目标

1. 本模块不实现 PostgreSQL 查询工具。
2. 本模块不替代记忆系统。
3. 本模块不决定是否执行 SQL，而是给权限模块提供上下文依据。
4. 本模块不把所有历史永久保存。
5. 本模块不保证模型不会犯错，但要减少模型从错误上下文中推理的概率。

## 5. 上下文分层设计

推荐把上下文拆成以下层：

```text
SystemContext
  系统安全规则、工具策略、数据库操作边界

UserConstraintContext
  用户明确约束：只读、不要执行、环境、影响行数阈值

IntentContext
  当前 DBTaskIntent

PlanContext
  当前 DBTaskPlan、current_step_id、TaskStep

ObservationContext
  DBObservation：EXPLAIN、schema、索引、行数、错误、影响行数

ApprovalContext
  ApprovalDecision：审批了什么、拒绝了什么、是否过期

VerificationContext
  VerificationResult：哪些成功标准已验证

ConversationContext
  最近用户对话和模型回复

MemoryContext
  稳定偏好、默认环境、长期事实
```

这几层的优先级不同，生命周期也不同。

## 6. 推荐数据结构

### 6.1 StepContextPacket

每次进入 `llm_reason` 前，应构造一个当前步骤上下文包：

```python
class StepContextPacket(TypedDict):
    step_id: str
    phase: str
    description: str
    risk_level: str
    tool_policy: str
    success_criteria: list[str]
    user_constraints: list[str]
    relevant_observations: list[DBObservation]
    relevant_approvals: list[ApprovalDecision]
    relevant_verifications: list[VerificationResult]
    allowed_actions: list[str]
    blocked_actions: list[str]
    missing_context: list[str]
```

### 6.2 DBWorkingSet

保存当前数据库任务相关对象：

```python
class DBWorkingSet(TypedDict):
    target_environment: str
    target_database: Optional[str]
    schemas: list[str]
    tables: list[str]
    columns: dict[str, list[str]]
    indexes: dict[str, list[str]]
    known_queries: list[dict]
    row_counts: dict[str, int]
    statistics_refs: list[str]
    last_refreshed_at: str
```

### 6.3 ResultDigest

用于大结果集摘要：

```python
class ResultDigest(TypedDict):
    observation_id: str
    row_count: int
    column_names: list[str]
    column_types: dict[str, str]
    sample_rows: list[dict]
    aggregates: dict
    truncation_applied: bool
    sensitive_fields_masked: list[str]
```

### 6.4 ContextSnapshot

用于恢复和审计：

```python
class ContextSnapshot(TypedDict):
    intent_id: str
    plan_id: str
    current_step_id: Optional[str]
    user_constraints: list[str]
    observation_ids: list[str]
    approval_ids: list[str]
    verification_ids: list[str]
    db_working_set_ref: Optional[str]
    replan_trigger: Optional[str]
    created_at: str
```

## 7. 设计点与参考权衡

### 7.1 设计点一：上下文分层，而不是全量历史堆叠

**Codex 的做法**

Codex 使用 session/turn state 保存计划、审批、用户输入、工具状态等信息，不完全依赖聊天历史。它会把执行过程中的关键状态显式建模。

**Claude 的做法**

Claude 使用 AppState、todo/task、permission context 等结构保存复杂交互状态。任务状态、权限状态和 UI 状态并不只是聊天文本。

**最终方案**

mini-agent 将上下文拆为 System、UserConstraint、Intent、Plan、Observation、Approval、Verification、Conversation、Memory 多层。每一层有不同优先级、生命周期和压缩策略。

**权衡原因**

Codex 的结构化 turn state 适合执行严谨性，Claude 的 task/AppState 适合复杂交互和任务恢复。数据库 Agent 同时需要二者。

**效果**

模型不需要从整段聊天里猜当前状态；系统也能清楚知道哪些信息用于 prompt，哪些用于权限，哪些用于审计。

### 7.2 设计点二：每轮构造 StepContextPacket

**Codex 的做法**

Codex 会围绕当前 turn、计划和工具状态构造模型上下文，使模型知道当前任务和执行边界。

**Claude 的做法**

Claude 的 task/todo 状态让模型和 UI 都能围绕当前任务推进，而不是处理一团历史消息。

**最终方案**

mini-agent 每轮进入 `llm_reason` 前生成 `StepContextPacket`，只包含当前 `TaskStep` 所需信息：

- 当前 phase。
- 当前 risk。
- 当前 tool_policy。
- 当前 success_criteria。
- 相关 observations。
- 用户约束。
- 允许/禁止动作。

**权衡原因**

全局上下文太多会干扰模型；只给当前步骤上下文能降低跑偏概率。

**效果**

在 `observe` 阶段，模型只关注收集证据；在 `verify` 阶段，模型只关注成功标准和证据是否匹配。

### 7.3 设计点三：数据库证据结构化保存

**Codex 的做法**

Codex 的工具调用和结果通过事件与状态承载，系统可以知道发生了什么。

**Claude 的做法**

Claude 的工具执行结果、任务状态和 UI 展示会进入结构化状态，方便后续使用。

**最终方案**

mini-agent 继续使用 `DBObservation` 保存数据库证据：

```text
EXPLAIN -> explain_plan
schema query -> schema_summary
index query -> index_summary
COUNT -> row_count_estimate
DML result -> affected_rows
SQL exception -> sql_error
```

**权衡原因**

数据库诊断依赖证据。自然语言历史不适合自动验证和重规划。

**效果**

`verify_step` 可以检查是否已有 EXPLAIN，`replan_gate` 可以根据 affected_rows 自动触发风险升级。

### 7.4 设计点四：维护 DBWorkingSet

**Codex 的做法**

Codex 做代码任务时会先读取相关文件、目录和配置，把工作区状态纳入当前任务。

**Claude 的做法**

Claude 会持续维护任务和项目上下文，让用户多轮交互时不必反复说明同一对象。

**最终方案**

mini-agent 增加 `DBWorkingSet`，记录当前数据库任务相关的库、schema、表、列、索引、行数、已知 SQL。

**权衡原因**

PostgreSQL 任务经常围绕同一批表和 SQL 多轮分析。把这些对象组织成 working set，比每轮重新查询和重新理解更稳定。

**效果**

用户说“那也看看第二张表”时，Agent 能知道“第二张表”来自当前 working set。

### 7.5 设计点五：类型化压缩，而不是整段摘要

**Codex 的做法**

Codex 有上下文压缩机制，但会保留任务继续所需的信息。

**Claude 的做法**

Claude 的 todo/task 等结构化状态不会被普通聊天摘要替代。

**最终方案**

mini-agent 采用 typed compaction：

```text
Conversation -> 自然语言摘要
DBObservation -> 结构化摘要
SQL -> 保留原文或 hash，不随意改写
ApprovalDecision -> 精确保存
EXPLAIN -> 保留关键节点、cost、rows、filter、index usage
ResultSet -> ResultDigest
```

**权衡原因**

不同上下文类型的重要性不同。SQL 和审批不能被模型随意摘要，聊天可以摘要。

**效果**

节省 token 的同时，不会丢失关键证据或改变 SQL 语义。

### 7.6 设计点六：按当前步骤检索上下文

**Codex 的做法**

Codex 会根据当前任务动态读取相关上下文，而不是把所有文件都塞进去。

**Claude 的做法**

Claude 的 task 驱动上下文让模型围绕当前工作项获取信息。

**最终方案**

mini-agent 的上下文检索以 `current_step.phase` 为中心：

```text
observe -> 目标对象、连接环境、schema/index 元数据
diagnose -> EXPLAIN、统计信息、慢查询样本
propose -> 诊断结果、用户约束、风险边界
approve -> SQL、影响评估、回滚方案
execute -> 已批准 SQL、审批记录、目标环境
verify -> success_criteria、执行结果、相关 observation
report -> plan、observation、approval、verification
```

**权衡原因**

同一个任务不同阶段需要的上下文不同。按步骤检索比按用户原始问题检索更准确。

**效果**

verify 阶段不会被大量无关 schema 干扰，approve 阶段不会缺少回滚方案。

### 7.7 设计点七：用户约束高优先级保存

**Codex 的做法**

Codex 强调遵守用户指令、sandbox 和审批模式。用户约束会影响执行行为。

**Claude 的做法**

Claude 的 permission context 会影响工具调用和交互确认。

**最终方案**

mini-agent 增加 `UserConstraintContext`，保存：

- 只读。
- 不执行。
- 只针对 staging。
- 影响超过阈值停止。
- 执行前必须确认。
- 不允许锁表。

这些约束要同时进入 prompt、planner validator、tool_policy_gate。

**权衡原因**

用户约束在数据库任务里就是安全边界，不能只放在聊天历史里。

**效果**

用户说“只读分析”后，后续 planner 和 tool gate 都不会允许写 SQL。

### 7.8 设计点八：审批和执行上下文精确保留

**Codex 的做法**

Codex 有 pending approvals 和 approval response 状态，审批不是一句自然语言聊天。

**Claude 的做法**

Claude 使用 permission request/decision 机制，权限请求和用户决策是结构化对象。

**最终方案**

mini-agent 使用 `ApprovalDecision` 精确保存：

- 哪个 step。
- 哪个 SQL。
- 哪个环境。
- 风险等级。
- 影响预估。
- 回滚摘要。
- 用户批准/拒绝/编辑。

**权衡原因**

数据库变更需要审计。审批不能被压缩成“用户同意了”。

**效果**

执行时可以校验 SQL 是否就是用户批准的 SQL；最终报告也能清楚说明审批内容。

### 7.9 设计点九：区分事实、假设、推断、用户确认

**Codex 的做法**

Codex 在执行前通常会读取事实，并在不确定时询问用户，而不是把猜测当事实。

**Claude 的做法**

Claude 的任务和交互系统会把用户确认与模型生成内容区分开。

**最终方案**

上下文中显式维护：

```text
facts: 工具查询得到的信息
assumptions: planner 或模型暂时假设
inferences: 基于证据推断的结论
confirmed_by_user: 用户确认的信息
```

**权衡原因**

数据库任务里，把推断当事实会导致错误 SQL 或错误报告。

**效果**

最终报告能写清楚“已查询到”和“推测可能”，模型也不会把未验证索引当成真实存在。

### 7.10 设计点十：大结果集使用 ResultDigest

**Codex 的做法**

Codex 会控制上下文长度，并对长输出进行摘要或截断。

**Claude 的做法**

Claude 对工具输出和 UI 展示也会做截断、摘要和状态化处理。

**最终方案**

mini-agent 对大结果集不直接入 prompt，而是生成 `ResultDigest`：

- 行数。
- 列名。
- 类型。
- 前 N 行样例。
- 聚合统计。
- 异常值摘要。
- 是否截断。
- 哪些字段已脱敏。

**权衡原因**

数据库查询结果可能非常大，也可能包含敏感数据。

**效果**

模型理解数据形态，但不会被海量原始数据淹没，也降低敏感数据暴露。

### 7.11 设计点十一：敏感信息默认不进入长期记忆

**Codex 的做法**

Codex 的执行环境和权限边界会限制敏感操作，敏感信息不应被随意扩散到长期上下文。

**Claude 的做法**

Claude 的权限上下文和工具交互把敏感决策与普通聊天区分开。

**最终方案**

mini-agent 增加 `SensitiveContextPolicy`：

```text
连接串、密码、token -> 仅运行时配置，不进 memory
生产库主机 -> 默认不进长期记忆
PII 查询结果 -> 脱敏后摘要
审批 SQL -> 可保存 SQL hash 和摘要，必要时保存原文
大结果集 -> 默认只保存 digest
```

**权衡原因**

数据库 Agent 可能接触真实生产数据，安全策略必须默认保守。

**效果**

降低凭证泄漏、敏感数据长期残留和 prompt 反复暴露的风险。

### 7.12 设计点十二：恢复任务状态，而不是复读完整聊天

**Codex 的做法**

Codex 使用 session/turn state，使任务可以围绕状态恢复，而不是只靠聊天文本。

**Claude 的做法**

Claude 的 AppState/task state 可以恢复任务、权限和 UI 状态。

**最终方案**

mini-agent 使用 `ContextSnapshot` 持久化：

- intent_id
- plan_id
- current_step_id
- observations
- approvals
- verifications
- user_constraints
- replan_trigger

**权衡原因**

数据库任务中断后，最重要的是恢复“执行到哪里、批准过什么、证据有哪些”，不是恢复所有聊天文本。

**效果**

用户第二天回来，Agent 能继续当前计划，而不是重新问所有问题。

### 7.13 设计点十三：最终回答基于结构化上下文

**Codex 的做法**

Codex 最终总结通常会说明完成了什么、验证了什么、还有什么风险。

**Claude 的做法**

Claude 可以基于 todo/task 状态输出完成情况。

**最终方案**

mini-agent 的 final report 应读取：

- `DBTaskPlan`
- `DBObservation`
- `ApprovalDecision`
- `VerificationResult`
- `UserConstraintContext`

而不是只让 LLM 总结聊天历史。

**权衡原因**

数据库用户最终关心的是证据、变更、审批、风险和验证结果。

**效果**

最终报告可以明确说明“只做了只读检查，没有修改数据；发现慢查询来自顺序扫描；建议创建索引，但尚未执行”。

### 7.14 设计点十四：上下文服务工具权限

**Codex 的做法**

Codex 的上下文、权限模式和 sandbox 会共同决定工具能否执行。

**Claude 的做法**

Claude 的 permission context 会参与工具使用判断。

**最终方案**

mini-agent 的 `tool_policy_gate` 不只看工具名，还要读取：

- 当前 `TaskStep`
- `StepContextPacket`
- 用户约束
- 审批记录
- 目标环境

**权衡原因**

如果上下文只服务 prompt，不服务工具策略，安全边界会变弱。

**效果**

上下文成为系统行为控制依据，而不只是给模型看的信息。

### 7.15 设计点十五：上下文优先级与 token 预算

**Codex 的做法**

Codex 会控制上下文窗口，并在长任务中压缩和选择信息。

**Claude 的做法**

Claude 保留 task/todo 等关键状态，而不是无限堆历史。

**最终方案**

mini-agent 采用优先级预算：

```text
P0: 系统安全规则、用户约束、当前步骤、审批状态
P1: 当前步骤相关 observations、success_criteria、tool_policy
P2: 当前 DBTaskPlan、DBWorkingSet
P3: 最近对话
P4: 已完成步骤详情、旧工具输出摘要
```

超预算时，从低优先级开始压缩或移除。

**权衡原因**

上下文窗口有限，必须稳定保留安全和当前执行所需信息。

**效果**

长任务不会因为上下文膨胀导致模型忘记当前安全边界。

## 8. Prompt 构造流程

进入 `llm_reason` 前：

```text
1. 读取 SystemContext
2. 读取 UserConstraintContext
3. 读取当前 StepContextPacket
4. 检索当前 step 相关 observations
5. 加入必要 approval / verification
6. 加入短期 conversation summary
7. 检查 token budget
8. 输出最终 prompt context
```

推荐最终结构：

```text
System safety rules
User constraints
Current task intent
Current plan step
Allowed / blocked actions
Relevant database observations
Approval and verification state
Recent conversation summary
```

## 9. 压缩策略

### 9.1 Conversation 压缩

自然语言摘要即可，但必须保留：

- 用户明确约束。
- 用户确认。
- 用户改口。

### 9.2 Observation 压缩

按类型压缩：

- EXPLAIN：保留 scan type、cost、rows、filter、index usage。
- Schema：保留表、列、类型、约束。
- Index：保留索引名、列、唯一性、使用建议。
- ResultSet：转 `ResultDigest`。
- Error：保留错误码、错误类型、SQL hash。

### 9.3 Approval 压缩

默认不压缩语义，只可隐藏敏感值。

保留：

- SQL hash。
- 风险等级。
- 环境。
- 用户决策。
- 时间。

### 9.4 Plan 压缩

已完成步骤可摘要，但当前步骤、未完成步骤、失败步骤不能丢。

## 10. 与现有模块的关系

### 10.1 与任务理解模块

任务理解读取：

- 最近用户输入。
- 用户约束。
- 当前任务上下文。
- DBWorkingSet。

输出 `DBTaskIntent` 后写入 IntentContext。

### 10.2 与规划模块

规划读取：

- `DBTaskIntent`
- UserConstraintContext
- DBWorkingSet
- 相关 observations

输出 `DBTaskPlan` 后写入 PlanContext。

### 10.3 与 Agent Loop

Agent Loop 每轮读取 `StepContextPacket`，并写入：

- DBObservation
- VerificationResult
- ApprovalDecision
- ContextSnapshot

### 10.4 与工具权限模块

工具权限读取：

- 当前 step 的 tool_policy。
- 用户约束。
- 审批记录。
- 目标环境。

### 10.5 与记忆系统

长期记忆只保存稳定、非敏感、可复用信息：

- 用户偏好的报告格式。
- 默认只读习惯。
- 常用非敏感环境别名。

不保存：

- 密码。
- token。
- 原始大结果集。
- 未脱敏 PII。

## 11. 实施计划

### 阶段一：StepContextPacket

1. 增加 context builder。
2. `llm_reason` 不直接拼全局 state，而是注入当前步骤上下文包。
3. 增加 token priority 规则。

验收标准：

- prompt 中明确包含当前 step、tool_policy、success_criteria。
- 无关 observation 不进入 prompt。

### 阶段二：Observation 和 ResultDigest

1. 完善 `DBObservation`。
2. 增加 ResultDigest。
3. 大结果集自动摘要。

验收标准：

- EXPLAIN、schema、index、row count 能按类型摘要。
- 大结果集不直接进入 prompt。

### 阶段三：用户约束和审批上下文

1. 增加 UserConstraintContext。
2. ApprovalDecision 精确保留。
3. tool_policy_gate 读取上下文。

验收标准：

- “只读”约束跨轮生效。
- 未审批 SQL 无法执行。

### 阶段四：类型化压缩

1. Conversation typed compaction。
2. Observation typed compaction。
3. Approval no-loss compaction。
4. Plan status compaction。

验收标准：

- 长任务压缩后仍能继续当前步骤。
- SQL 和审批语义不被摘要改写。

### 阶段五：ContextSnapshot 和恢复

1. 增加 ContextSnapshot。
2. resume 时恢复任务状态。
3. final report 基于结构化上下文生成。

验收标准：

- 中断后能恢复当前 step。
- 最终报告能列出证据、审批和验证。

## 12. 最终取舍总结

Codex 值得借鉴的是：

- turn/session state。
- 计划和权限边界进入上下文。
- 上下文压缩。
- 用户输入和审批状态结构化。

Claude 值得借鉴的是：

- AppState/task/todo 式上下文。
- permission context。
- 工具结果和 UI 状态联动。
- 长任务状态恢复。

mini-agent 的最终方案是：

```text
分层上下文
  -> 当前步骤上下文包
  -> 类型化数据库观察
  -> 用户约束高优先级保存
  -> 审批和 SQL 精确保留
  -> 大结果集摘要
  -> 敏感信息不进长期记忆
  -> 按 step 检索和压缩
  -> final report 基于结构化状态
```

这个方案避免两种极端：

- 不把所有聊天历史无脑塞进 prompt。
- 不把上下文切得过碎导致模型缺少当前任务信息。

最终目标是让 mini-agent 在长时间 PostgreSQL 管理任务中持续知道：当前要做什么、已经观察到什么、用户限制了什么、批准过什么、验证了什么，以及哪些信息不能被长期保存。

