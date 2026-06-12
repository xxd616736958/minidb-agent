# PostgreSQL 管理智能体：人机交互与协作模块设计

## 1. 背景

mini-agent 已经逐步具备 PostgreSQL 管理智能体的核心能力：任务理解、规划与任务分解、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 工具、执行环境与工作区管理、状态管理、安全护栏与权限控制。

这些模块让 Agent “能做事”，但要真正管理数据库，还必须让人类用户能够理解、干预、确认、恢复和复盘 Agent 的行为。

PostgreSQL 管理任务不同于普通问答：

- 用户可能只说“帮我看看这个库有没有问题”，但没有说明目标环境、目标库、输出形式。
- Agent 可能需要先做只读观察，再做诊断，再提出修复建议。
- 写操作、DDL、维护操作必须让用户明确审批。
- 用户可能中途要求“不要执行，只生成 SQL”。
- 长任务可能被打断，下一轮需要恢复上下文。
- 最终结果可能要给开发、DBA、运维或管理者查看。

因此，人机交互与协作模块不是简单的 CLI 显示层，而是贯穿任务理解、规划、工具执行、安全审批、验证、报告和记忆沉淀的协作控制层。

## 2. 模块定位

人机交互与协作模块位于用户和 Agent 内部状态机之间：

```text
User
  -> Human Collaboration Layer
       -> Task Card
       -> Clarification
       -> Plan Review
       -> Progress Streaming
       -> Tool Call Display
       -> Approval Card
       -> Feedback Capture
       -> Result Explanation
       -> Pause / Resume
       -> Final Report
  -> Agent Loop / Tools / Safety / State / Memory
```

本模块不替代任务理解、规划、安全护栏或工具执行。它负责把这些模块的内部状态翻译成用户能理解和能操作的协作界面，同时把用户反馈转成 Agent 可以执行的结构化状态。

## 3. 设计目标

1. 让用户知道 Agent 当前理解的任务是什么。
2. 只在必要时澄清，不把用户拖进冗长表单。
3. 让计划、风险、工具、审批和结果对用户可见。
4. 让高风险数据库动作必须经过明确审批。
5. 让用户可以批准、拒绝、修改、降级或要求补充证据。
6. 让安全拒绝可以转化为可继续推进的替代路径。
7. 让用户反馈进入状态、记忆和安全策略，而不是只存在聊天文本里。
8. 让长任务可以暂停、恢复和复盘。
9. 让最终输出成为可交付、可审计、可分享的数据库任务报告。

## 4. 非目标

1. 不在第一阶段实现复杂 Web UI。
2. 不让用户手工填写所有内部状态字段。
3. 不绕过安全护栏直接执行用户批准之外的动作。
4. 不把人机交互模块变成新的业务逻辑中心。
5. 不把所有工具原始输出都直接展示给用户。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经有以下协作基础：

- `ClarificationRequest`：表达缺失信息和澄清问题。
- `DBTaskIntent`：表达任务理解结果。
- `DBTaskPlan` / `TaskStep`：表达结构化计划。
- `StepContextPacket`：表达当前步骤上下文。
- `ToolCallPolicyDecision` / `SecurityPolicyDecision`：表达工具和安全决策。
- `ApprovalDecision` / `pending_approval`：表达待审批数据库动作。
- `ToolInvocationRecord`：表达工具调用审计。
- `DBObservation` / `ResultDigest`：表达工具结果和摘要。
- `VerificationResult`：表达验证结果。
- CLI 中已有 `print_intent`、`print_db_plan`、`print_loop_status`、`print_tool_call`、`print_tool_result`。

### 5.2 主要不足

当前协作能力仍然偏“状态展示”，还没有形成完整的人机协作协议：

1. 任务理解结果还没有被包装成稳定的任务卡。
2. 计划展示和用户确认之间没有明确的反馈入口。
3. 审批展示还不够数据库化，缺少面向 DBA 的审批卡。
4. 用户拒绝或修改审批后的重规划路径还不够明确。
5. 安全拒绝后如何给替代路径还需要标准化。
6. 用户反馈如何沉淀为约束、审批、记忆和运行策略还需要统一。
7. 最终报告和审计证据之间还没有明确格式契约。

## 6. 推荐核心对象

### 6.1 TaskCard

```python
class TaskCard(TypedDict):
    id: str
    intent_id: str
    title: str
    goal: str
    target_environment: str
    target_database: Optional[str]
    target_objects: list[dict[str, Any]]
    risk_level: str
    expected_output: str
    missing_slots: list[str]
    assumptions: list[str]
    user_constraints: list[str]
    status: Literal["draft", "confirmed", "needs_clarification", "cancelled"]
```

### 6.2 PlanReview

```python
class PlanReview(TypedDict):
    id: str
    plan_id: str
    status: Literal["pending", "approved", "rejected", "edited"]
    reviewed_steps: list[str]
    user_message: Optional[str]
    created_at: str
    resolved_at: Optional[str]
```

### 6.3 CollaborationEvent

```python
class CollaborationEvent(TypedDict):
    id: str
    event_type: Literal[
        "task_card_shown",
        "clarification_requested",
        "clarification_answered",
        "plan_shown",
        "plan_reviewed",
        "tool_call_shown",
        "approval_requested",
        "approval_resolved",
        "safety_block_explained",
        "result_explained",
        "task_paused",
        "task_resumed",
        "final_report_shown",
    ]
    step_id: Optional[str]
    summary: str
    payload_ref: Optional[str]
    created_at: str
```

### 6.4 ApprovalCard

```python
class ApprovalCard(TypedDict):
    approval_id: str
    step_id: str
    tool_name: str
    target_environment: str
    target_database: Optional[str]
    sql_preview: Optional[str]
    sql_hash: Optional[str]
    risk_level: str
    impact_summary: Optional[str]
    rollback_summary: Optional[str]
    verification_criteria: list[str]
    replay_policy: str
    options: list[Literal[
        "approve",
        "reject",
        "edit",
        "dry_run_more",
        "report_only",
        "clarify",
    ]]
```

### 6.5 UserFeedback

```python
class UserFeedback(TypedDict):
    id: str
    feedback_type: Literal[
        "clarification_answer",
        "plan_edit",
        "approval_decision",
        "constraint",
        "preference",
        "correction",
        "stop",
        "resume",
    ]
    target_ref: Optional[str]
    content: str
    structured_delta: dict[str, Any]
    should_write_memory: bool
    created_at: str
```

## 7. 推荐交互流程

```text
1. 用户提出任务
   -> 任务理解模块生成 DBTaskIntent
   -> 人机协作模块展示 TaskCard

2. 关键信息缺失
   -> 展示 ClarificationRequest
   -> 用户回答后更新 confirmed_context / user_constraints

3. 计划生成
   -> 展示 DBTaskPlan / PlanReview
   -> 用户可确认、修改、降级为只读或报告模式

4. Agent 执行
   -> 流式展示当前节点、当前步骤、可用工具、工具调用和结果摘要

5. 安全策略触发
   -> 安全拒绝展示原因和替代路径
   -> 写操作展示 ApprovalCard 并停止当前 turn 等待用户审批

6. 用户反馈
   -> 转成 UserFeedback
   -> 更新 ApprovalDecision / RuntimePolicy / user_constraints / memory_candidates

7. 验证与报告
   -> 展示证据、结论、建议、验证结果和最终报告

8. 暂停与恢复
   -> 展示当前任务状态、已执行工具、pending approval、不可重放动作
```

## 8. 详细设计点

### 8.1 设计点一：任务开始时生成任务卡

**Codex 的设计方案**

Codex 在处理任务时，会基于当前工作区、用户请求、可用工具和安全边界形成一个清晰的执行上下文。它通常不会把这个上下文叫作“任务卡”，但会通过计划、状态更新、命令展示等方式让用户知道当前任务范围。Codex 的好处是执行目标和工作区边界比较清晰；局限是它更偏工程任务，对数据库环境、目标对象、风险等级这些领域字段没有天然结构。

**Claude 的设计方案**

Claude 更擅长把用户模糊表达转成自然语言可读的理解摘要，并在必要时用澄清问题确认意图。Claude 的好处是交互更像协作对话，用户更容易纠正它的理解；局限是如果只停留在自然语言摘要，后续工具和安全策略不容易消费。

**最终方案**

mini-agent 在任务理解后生成 `TaskCard`，把 `DBTaskIntent` 中的目标、环境、数据库、对象、风险、输出形式、缺失信息和假设展示给用户。任务卡不是固定意图枚举，而是用户当前数据库任务的“可确认摘要”。

**权衡原因**

Codex 的上下文边界适合保证执行不跑偏，Claude 的自然语言确认适合降低用户理解成本。mini-agent 需要把二者结合成结构化但可读的任务卡。

**效果**

用户在 Agent 连接数据库或执行工具前，就能看到系统理解是否正确。模糊任务可以先澄清，高风险任务可以提前暴露风险。

### 8.2 设计点二：澄清问题少而准

**Codex 的设计方案**

Codex 在遇到路径、权限、破坏性操作或目标不明确时，会停下来要求用户确认或给出可执行假设。它的好处是不会在关键不确定性上冒险；局限是有时更偏执行层确认，而不是领域语义澄清。

**Claude 的设计方案**

Claude 通常会在用户意图模糊时提出少量自然语言问题，尤其适合区分“要诊断、要修复、要报告”这类任务方向。它的好处是对话自然；局限是如果问题过多，会打断自动化体验。

**最终方案**

mini-agent 只在缺少关键槽位时澄清，例如目标环境、目标库、目标对象、是否允许写操作、预期输出、审批人意图。普通只读诊断可以用保守默认值继续推进；写库和生产库相关任务必须澄清。

**权衡原因**

Codex 的谨慎确认适合安全关键点，Claude 的简洁对话适合用户体验。mini-agent 不应该把数据库任务变成问卷，但不能跳过影响安全路径的问题。

**效果**

用户不会被不必要问题打断；真正影响数据库安全和执行结果的信息会被明确确认。

### 8.3 设计点三：计划可见、可解释、可确认

**Codex 的设计方案**

Codex 会通过计划、进度更新、命令和验证结果让用户看到任务推进过程。它的好处是用户可以理解 Agent 为什么这样做；局限是计划展示通常面向代码任务，不一定包含数据库风险、审批和回滚。

**Claude 的设计方案**

Claude 倾向于把复杂任务拆成清晰的步骤，有时会使用 todo/plan 形式展示待办项。它的好处是协作感强；局限是如果计划只是文本列表，后续状态机和工具策略难以直接使用。

**最终方案**

mini-agent 展示 `DBTaskPlan`，每个 `TaskStep` 都包含 phase、description、risk_level、tool_policy、expected_tools、requires_approval、success_criteria。用户可以确认计划，也可以要求“只读诊断”“不要执行”“只生成报告”。

**权衡原因**

Codex 的执行透明度适合过程控制，Claude 的步骤表达适合用户理解。mini-agent 用结构化计划作为两者之间的协议。

**效果**

用户可以在执行前发现计划偏差，安全模块也能根据计划阶段控制工具可见性和审批要求。

### 8.4 设计点四：执行过程流式展示

**Codex 的设计方案**

Codex 的工具调用、命令输出、状态变化会逐步展示给用户。它的好处是长任务不黑盒；局限是原始工程输出可能对普通用户过于嘈杂。

**Claude 的设计方案**

Claude 的工具调用过程会以 tool_use / tool_result 的形式进入对话，并通常配合自然语言解释。它的好处是对话可读性高；局限是过度自然语言化可能隐藏底层执行细节。

**最终方案**

mini-agent 在 CLI 或后续 UI 中展示节点级和步骤级状态：任务理解、规划、当前步骤、可用工具、工具调用、策略决策、执行结果、观察、验证、报告。默认展示摘要，专家模式可以展开工具名、参数、SQL hash、耗时和 artifact。

**权衡原因**

Codex 的实时执行透明性适合可信度，Claude 的解释性适合易用性。mini-agent 需要同时支持普通用户和 DBA。

**效果**

用户能及时知道 Agent 在做什么，发现问题可以中断或纠正，长时间数据库诊断不会像卡住。

### 8.5 设计点五：工具调用用人话解释，同时保留真实细节

**Codex 的设计方案**

Codex 会直接展示命令、文件修改和工具结果，这让工程师能审查真实动作。它的好处是透明；局限是非专家可能看不懂。

**Claude 的设计方案**

Claude 会把工具动作包装成较自然的描述，让用户理解当前动作意图。它的好处是易懂；局限是如果隐藏参数，审计性不足。

**最终方案**

mini-agent 展示工具调用时分两层：普通描述如“正在只读查询 orders 表行数”，专家详情如 `tool_name`、args digest、SQL preview、SQL hash、risk、duration、row_count、sqlstate。

**权衡原因**

Codex 的真实细节适合审计，Claude 的自然语言适合协作。数据库管理既需要用户能读懂，也需要可追责。

**效果**

普通用户知道 Agent 在做什么，DBA 可以精确审查工具参数和 SQL。

### 8.6 设计点六：数据库变更审批卡

**Codex 的设计方案**

Codex 对高风险命令会请求用户审批，审批绑定具体工具动作。它的好处是不会让模型任意执行危险操作；局限是数据库变更需要比命令审批更多字段，例如 SQL hash、影响范围、回滚方案和验证标准。

**Claude 的设计方案**

Claude 的 permission prompt 会展示工具名、输入和权限原因，让用户参与确认。它的好处是交互清晰；局限是普通 permission prompt 对数据库写操作仍然太泛。

**最终方案**

mini-agent 将 `pending_approval` 渲染为 `ApprovalCard`，展示目标环境、数据库、schema/table、SQL preview、SQL hash、风险、影响说明、回滚说明、验证标准、replay policy 和可选动作。

**权衡原因**

Codex 的审批生命周期适合硬门禁，Claude 的 permission prompt 适合用户理解。mini-agent 把审批从“是否继续”升级为“批准这个具体数据库变更”。

**效果**

用户审批的是具体 SQL、具体环境、具体步骤，审批不能被复用到其他 SQL 或其他环境。

### 8.7 设计点七：用户可以批准、拒绝、修改、降级

**Codex 的设计方案**

Codex 在命令或修改不合适时，可以通过用户反馈调整执行路径，重新生成命令或改方案。它的好处是执行可纠偏；局限是数据库审批需要更严格的状态绑定。

**Claude 的设计方案**

Claude 的对话式协作更自然，用户可以要求改写、降级、只输出方案、不执行。它的好处是反馈形式灵活；局限是需要把自然语言反馈变成结构化状态。

**最终方案**

mini-agent 的 `ApprovalCard` 支持 `approve / reject / edit / dry_run_more / report_only / clarify`。用户拒绝后，当前步骤进入重规划或降级；用户修改 SQL 后必须重新计算 SQL hash 和审批绑定；用户选择 report_only 后，不再执行写工具。

**权衡原因**

Codex 的执行纠偏适合工具流程，Claude 的自然反馈适合用户表达。mini-agent 必须把灵活反馈落到结构化状态里。

**效果**

用户不必在“批准/终止”之间二选一，可以把任务改成更安全、更符合实际运维流程的路径。

### 8.8 设计点八：安全拒绝要给替代路径

**Codex 的设计方案**

Codex 遇到沙箱、权限或危险命令限制时，会说明限制并寻找可行替代方案。它的好处是任务不中断；局限是替代方案通常围绕文件、命令和代码。

**Claude 的设计方案**

Claude 更擅长把拒绝转成解释和下一步建议，让用户知道还能怎么做。它的好处是用户体验好；局限是如果没有状态机接管，建议可能无法自动继续执行。

**最终方案**

mini-agent 对安全拒绝生成结构化解释：拒绝原因、命中的规则、当前风险、可选替代路径。例如生产库写操作被拒绝时，提供只读诊断、dry-run 报告、生成待审批 SQL、确认环境四种路径。

**权衡原因**

Codex 的权限边界适合硬拒绝，Claude 的解释能力适合软着陆。mini-agent 应该既拒绝危险动作，又给出可继续推进的安全动作。

**效果**

安全护栏不会让任务卡死，用户知道下一步该怎么继续。

### 8.9 设计点九：用户反馈进入状态和记忆

**Codex 的设计方案**

Codex 会遵守用户当前指令和开发者约束，并在执行中保护用户已有工作。它的好处是用户反馈会影响后续行为；局限是跨会话长期偏好和组织规则需要额外记忆系统。

**Claude 的设计方案**

Claude 的记忆和偏好机制适合保存用户长期规则。它的好处是跨轮协作更连续；局限是需要区分普通偏好和安全禁令。

**最终方案**

mini-agent 将用户反馈归一化为 `UserFeedback`，再根据类型更新 `confirmed_context`、`user_constraints`、`ApprovalDecision`、`RuntimePolicy`、`MemoryCandidate`。例如“这个库只读”写成 safety prohibition，“报告里要给 SQL hash”写成 preference。

**权衡原因**

Codex 的指令遵循适合当前任务，Claude 的记忆适合长期协作。mini-agent 需要把用户反馈转成状态和记忆，避免只存在聊天文本里。

**效果**

用户规则会真正影响工具可见性、安全策略、规划和最终报告。

### 8.10 设计点十：结果解释分为证据、结论、建议、下一步

**Codex 的设计方案**

Codex 在运行命令或测试后，会结合结果说明做了什么、验证了什么、是否还有问题。它的好处是结果和行动连接紧密；局限是数据库诊断结果需要更强的领域解释。

**Claude 的设计方案**

Claude 擅长把复杂结果组织成面向用户的解释、建议和下一步。它的好处是易读；局限是如果没有结构化证据引用，容易变成无法审计的总结。

**最终方案**

mini-agent 每个关键步骤输出四段式结果：证据、结论、建议、下一步。证据引用 `DBObservation`、`ResultDigest` 和 `ArtifactRecord`，结论由 Agent 推理生成，建议可触发下一步计划或审批。

**权衡原因**

Codex 的验证闭环保证结果可靠，Claude 的表达方式保证用户能理解。mini-agent 必须让结论既可读又有证据链。

**效果**

用户不用阅读大量原始 SQL 输出，也能知道诊断依据和下一步动作。

### 8.11 设计点十一：长任务支持暂停、恢复和复盘

**Codex 的设计方案**

Codex 的执行过程有状态、工具事件和恢复语义，任务可以在上下文中继续推进。它的好处是长任务不完全依赖单轮消息；局限是数据库副作用动作需要更严格的不可重放策略。

**Claude 的设计方案**

Claude 的对话上下文延续适合让用户跨轮继续任务。它的好处是协作自然；局限是如果没有结构化状态，恢复时可能不知道哪些工具已经执行。

**最终方案**

mini-agent 恢复任务时展示当前步骤、计划状态、已执行工具、已归一化观察、pending approval、replay policy、不可重放写操作和恢复摘要。用户可以继续、取消、重规划或生成报告。

**权衡原因**

Codex 的 checkpoint 思路适合状态恢复，Claude 的对话延续适合用户体验。mini-agent 需要在恢复时明确数据库任务现场。

**效果**

长任务不会因为会话中断而丢失现场，也不会重复执行有副作用的 SQL。

### 8.12 设计点十二：最终报告面向人类决策

**Codex 的设计方案**

Codex 完成任务后通常会总结改动、测试和结果。它的好处是工程闭环清楚；局限是数据库运维报告需要更多审计和风险字段。

**Claude 的设计方案**

Claude 擅长把复杂信息整理成清晰报告。它的好处是表达质量高；局限是必须绑定结构化证据，避免只凭语言总结。

**最终方案**

mini-agent 最终报告包含任务目标、目标环境、计划步骤、工具调用摘要、关键证据、风险判断、执行过的 SQL、未执行建议 SQL、审批记录、验证结果、后续建议和 artifact 引用。

**权衡原因**

Codex 的工程总结适合说明“做了什么”，Claude 的报告表达适合说明“意味着什么”。mini-agent 需要输出能给 DBA、开发和管理者看的报告。

**效果**

任务结果可交付、可分享、可审计，不只是聊天里的临时回答。

### 8.13 设计点十三：普通视图和专家视图分层

**Codex 的设计方案**

Codex 展示大量底层工程细节，例如命令、diff、测试结果。它的好处是专家可审查；局限是对非专家不够友好。

**Claude 的设计方案**

Claude 更偏自然语言解释，会减少底层噪声。它的好处是普通用户容易理解；局限是专家可能觉得信息不够。

**最终方案**

mini-agent 交互输出分普通视图和专家视图。普通视图展示目标、风险、结论、审批按钮和下一步；专家视图展示 SQL、hash、工具参数、SQLSTATE、耗时、行数、审计记录和 artifact 路径。

**权衡原因**

Codex 适合专家透明，Claude 适合普通用户理解。数据库 Agent 同时面对开发、DBA 和业务用户，必须分层展示。

**效果**

用户不会被信息淹没，高级用户又能检查真实执行细节。

### 8.14 设计点十四：协作模块驱动状态机

**Codex 的设计方案**

Codex 的用户输入、审批和工具结果会改变后续执行路径。它的好处是用户不是旁观者，而是控制执行过程的一部分；局限是需要状态机严格区分哪些反馈会触发重试、重规划或终止。

**Claude 的设计方案**

Claude 的用户反馈会自然进入下一轮对话，影响后续推理和工具调用。它的好处是对话连续；局限是如果没有结构化事件，系统很难可靠自动化。

**最终方案**

mini-agent 的协作模块输出结构化事件和状态更新：澄清回答回到任务理解，计划修改回到规划，审批批准进入执行，审批拒绝进入重规划或报告模式，用户新增约束更新安全策略和记忆。

**权衡原因**

Codex 的执行控制适合严谨状态机，Claude 的对话连续性适合自然协作。mini-agent 需要让人类反馈真正驱动 Agent Loop。

**效果**

人机交互不只是展示层，而是数据库任务状态机的一部分。

### 8.15 设计点十五：协作事件全程可审计

**Codex 的设计方案**

Codex 的工具调用、审批和执行事件可以被追踪。它的好处是可复盘；局限是用户协作行为本身也需要作为业务事件保存。

**Claude 的设计方案**

Claude 的 tool_use、tool_result 和用户消息都保留在会话中。它的好处是对话过程可见；局限是自然语言 transcript 不等于结构化审计。

**最终方案**

mini-agent 新增 `CollaborationEvent`，记录任务卡展示、澄清请求、计划展示、审批请求、审批结果、安全拒绝解释、报告展示等用户协作节点，并引用相关状态对象或 artifact。

**权衡原因**

Codex 的事件流适合工具和执行审计，Claude 的会话记录适合人类上下文。mini-agent 需要把关键协作动作结构化保存。

**效果**

后续可以复盘“用户什么时候批准了什么”“Agent 什么时候解释了什么风险”“为什么进入报告模式”。

## 9. 推荐交互状态机

| 状态 | 触发来源 | 展示内容 | 用户可选动作 | 下一步 |
| --- | --- | --- | --- | --- |
| task_card_pending | 任务理解完成 | 任务卡 | 确认 / 修改 / 取消 | 规划或澄清 |
| clarification_pending | 缺少关键槽位 | 澄清问题 | 回答 / 取消 | 更新 intent |
| plan_review_pending | 计划生成 | 计划步骤和风险 | 确认 / 修改 / 降级 | 执行或重规划 |
| running | Agent Loop 执行 | 当前步骤和工具摘要 | 暂停 / 停止 | 继续执行 |
| approval_pending | 安全策略要求审批 | 审批卡 | 批准 / 拒绝 / 修改 / 只报告 | 执行或重规划 |
| safety_blocked | 安全策略拒绝 | 拒绝原因和替代路径 | 选择替代路径 | 重规划 |
| verifying | 工具执行后 | 证据和验证标准 | 接受 / 要求补查 | 报告或继续 |
| paused | 用户暂停 | 当前状态摘要 | 恢复 / 取消 / 报告 | 恢复或结束 |
| completed | 任务完成 | 最终报告 | 保存 / 继续追问 | END |

## 10. 与现有模块的关系

### 10.1 与任务理解模块

任务理解模块输出 `DBTaskIntent`，人机协作模块将它渲染为 `TaskCard`，并把用户修改写回 `confirmed_context`、`user_constraints` 或重新触发 intent analyzer。

### 10.2 与规划模块

规划模块输出 `DBTaskPlan`，人机协作模块生成 `PlanReview`，用户可以确认计划、修改计划、降级为只读或要求只生成报告。

### 10.3 与 Agent Loop

Agent Loop 每个节点输出状态更新，人机协作模块负责展示当前状态、工具调用、观察、验证结果，并在用户反馈后驱动下一步路由。

### 10.4 与安全护栏模块

安全护栏模块输出 `SecurityPolicyDecision`、`SQLSafetyReport`、`pending_approval`。人机协作模块将这些渲染为安全解释和审批卡。

### 10.5 与状态管理模块

状态管理模块保存 `CollaborationEvent`、`UserFeedback`、`PlanReview`、`ApprovalDecision` 等协作状态，保证暂停、恢复和审计可用。

### 10.6 与记忆系统

人机协作模块将用户反馈中的长期规则转成 `MemoryCandidate`，例如只读要求、报告偏好、审批习惯和组织规范。

### 10.7 与工具模块

工具模块提供真实工具调用和结果；人机协作模块负责把工具名、参数和结果翻译成人类可理解的信息，同时保留专家细节。

## 11. 推荐实现步骤

### 11.1 第一阶段：结构化协作状态

1. 在 `agent/state.py` 中新增 `TaskCard`、`PlanReview`、`CollaborationEvent`、`ApprovalCard`、`UserFeedback`。
2. 在状态迁移中补默认字段。
3. 在状态校验中检查 pending approval、plan review 和 current step 一致性。

### 11.2 第二阶段：任务卡和计划确认

1. 在 intent validator 后生成 `TaskCard`。
2. 在 task planner 后生成 `PlanReview`。
3. CLI 展示任务卡和计划确认提示。
4. 支持用户将任务降级为只读诊断或报告模式。

### 11.3 第三阶段：审批卡和反馈处理

1. 将 `pending_approval` 渲染为 `ApprovalCard`。
2. 支持 `approve / reject / edit / dry_run_more / report_only / clarify`。
3. 用户反馈转成 `UserFeedback` 并更新 `ApprovalDecision`、`RuntimePolicy` 或 `replan_trigger`。

### 11.4 第四阶段：协作事件和报告

1. 所有关键协作动作写入 `CollaborationEvent`。
2. 最终报告引用任务卡、计划、工具调用、审批、观察、验证和 artifact。
3. CLI 增加普通视图和专家视图切换。

## 12. 验收标准

1. 用户提交数据库任务后，可以看到任务卡。
2. 关键槽位缺失时，系统只问必要问题。
3. 计划展示包含阶段、风险、工具策略和审批要求。
4. 执行过程显示当前步骤、工具调用和策略结果。
5. 写操作会展示审批卡，并停止当前 turn 等用户确认。
6. 用户拒绝审批后，系统能降级为报告或重规划。
7. 安全拒绝会给出至少一个替代路径。
8. 用户反馈能更新状态、约束或记忆候选。
9. 恢复任务时能展示当前步骤、pending approval 和不可重放动作。
10. 最终报告包含证据、结论、建议、审批和验证结果。

## 13. 最终结论

Codex 给 mini-agent 的核心启发是：人机协作必须绑定真实执行过程，用户要看得见工具、命令、审批、状态和验证结果。Claude 给 mini-agent 的核心启发是：协作要自然、可解释、可修改，用户反馈应该能以对话方式改变任务方向。

最终 mini-agent 的人机交互与协作模块采用折中方案：底层学习 Codex 的状态驱动、工具透明、审批和审计；上层学习 Claude 的自然澄清、计划解释、反馈修改和报告表达。结合 PostgreSQL 管理场景，形成“任务卡、少量澄清、计划确认、过程流式展示、数据库审批卡、反馈入状态、暂停恢复、最终报告”的完整协作协议。

