# PostgreSQL 管理智能体：多智能体与任务委派模块设计

## 1. 背景

mini-agent 已经具备 PostgreSQL 管理智能体的主要基础能力：任务理解、规划与任务分解、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 工具、执行环境与工作区管理、状态管理、安全护栏、人机协作、错误处理和质量控制。

这些模块让一个 Agent 可以完成完整数据库任务。但随着任务复杂度上升，单 Agent 会遇到几个明显问题：

- 一个任务可能同时需要 schema 探查、性能分析、安全复核、迁移规划和报告整理。
- 单个 Agent 容易在复杂任务中丢失局部目标，或者把诊断、执行、验收混在一起。
- 高风险数据库建议需要第二视角复核，不能只依赖一次模型判断。
- 只读诊断任务可以并行推进，但写操作必须串行、审批、可回滚。
- 子任务失败不应该让整个任务直接崩溃，应该变成可恢复的局部失败。
- 多智能体如果没有权限、状态和质量边界，反而会放大幻觉、越权和成本问题。

因此，多智能体与任务委派模块的目标不是把系统做成“很多 Agent 自由讨论”，而是把 mini-agent 升级为“主控 Agent 负责决策和安全，受控专家 Agent 负责局部分析，质量门负责验收”的数据库任务协作系统。

## 2. 模块定位

多智能体与任务委派模块位于规划、工具调用、安全护栏、质量控制和人机协作之间：

```text
User Task
  -> Task Understanding
  -> Planning
  -> Delegation Planner
       -> Agent Role Selection
       -> DelegatedTask
       -> Subagent Runtime
       -> DelegationResult
       -> Delegation Quality Gate
  -> Main Agent Decision
  -> Tool Execution / Approval / Final Report
```

输入：

- `DBTaskIntent`
- `DBTaskPlan`
- `TaskStep`
- 当前 `AgentState`
- 当前数据库环境和安全策略
- 可用工具目录
- 可用子智能体角色定义
- 用户约束、审批和反馈

输出：

- `AgentRoleDefinition`
- `DelegationPolicyDecision`
- `DelegatedTask`
- `DelegationRecord`
- `DelegationResult`
- `DelegationFailure`
- `DelegationEvaluation`
- `AgentTeamRun`

本模块不替代主 Agent，不直接执行高风险数据库写操作，也不替代安全护栏。它负责判断任务是否需要委派、生成受控委派单、调度子智能体、收集结构化结果，并通过质量门把结果交还给主控 Agent。

## 3. 设计目标

1. 让复杂 PostgreSQL 任务可以拆给不同专家角色处理。
2. 保证主控 Agent 始终拥有最终决策权、审批权和写操作执行权。
3. 让委派任务结构化、可审计、可回放。
4. 让子智能体只能看到完成任务所需的最小上下文。
5. 让子智能体拥有明确工具边界、权限边界和轮次限制。
6. 让只读、无依赖、资源可控的任务可以并行执行。
7. 让高风险任务具备提案与复核双角色机制。
8. 让子智能体输出必须经过质量验收，不能默认可信。
9. 让用户能够看到多智能体任务状态和必要审批原因。
10. 让失败的子智能体成为可恢复的局部失败。
11. 让委派行为进入测试、评估和回放体系。
12. 第一阶段保持角色少、边界清晰，不做开放式 Agent 市场。

## 4. 非目标

1. 不实现开放式多智能体聊天群。
2. 不允许子智能体直接执行数据库写操作。
3. 不让子智能体绕过主控 Agent 的安全策略和人类审批。
4. 不把所有任务都强行委派。
5. 不让子智能体共享完整会话、完整数据库信息和全部记忆。
6. 不在第一阶段实现远程云端 Agent 集群。
7. 不把 LLM 自我评价作为唯一验收依据。
8. 不用多智能体替代现有工具、状态、安全和质量模块。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经具备多智能体委派的基础条件：

- `DBTaskIntent` 可以表达用户目标、目标对象、风险和约束。
- `DBTaskPlan` / `TaskStep` 可以表达阶段、依赖、风险、审批和成功标准。
- `ToolSpec` / `ToolPoolBuilder` 可以根据上下文限制工具可见性。
- `SecurityPolicyEngine` 可以在工具执行前做安全判断。
- `QualityManager` 可以生成质量门、评估结果、回放用例和质量报告。
- `CollaborationManager` 可以生成任务卡、计划审查、审批卡和协作事件。
- `StateManager` / `StateValidator` 可以维护任务运行状态和一致性。
- PostgreSQL 工具已经区分只读、解释计划、检查 schema、写操作和安全报告。

### 5.2 主要不足

当前系统还缺少以下能力：

1. 没有子智能体角色定义，无法表达“性能分析员”“安全复核员”等职责。
2. 没有委派策略，无法判断哪些任务该委派、哪些任务必须主线程执行。
3. 没有结构化委派单，子任务目标、权限、上下文和验收标准不稳定。
4. 没有子智能体运行记录，无法审计谁在什么上下文中做了什么。
5. 没有结构化子智能体结果，主控 Agent 难以自动合并和验收。
6. 没有并发控制，无法保证并行诊断不会压垮数据库。
7. 没有复核角色，高风险建议缺少第二视角。
8. 没有委派质量门，子智能体输出可能被主控 Agent 直接采纳。
9. 没有面向用户的多智能体进度展示。
10. 没有委派评估用例，无法测试“是否该委派”和“委派是否可靠”。

## 6. 推荐核心对象

### 6.1 AgentRoleDefinition

```python
class AgentRoleDefinition(TypedDict):
    id: str
    name: str
    description: str
    responsibilities: list[str]
    allowed_tools: list[str]
    disallowed_tools: list[str]
    allowed_phases: list[str]
    default_model: Optional[str]
    max_turns: int
    max_tool_calls: int
    permission_mode: Literal["read_only", "proposal_only", "review_only"]
    memory_scope: Literal["none", "task", "project"]
    can_run_in_parallel: bool
    output_schema: str
```

### 6.2 DelegationPolicyDecision

```python
class DelegationPolicyDecision(TypedDict):
    id: str
    step_id: str
    decision: Literal["do_not_delegate", "delegate", "parallel_delegate", "review_required"]
    selected_roles: list[str]
    reason: str
    constraints: list[str]
    blocked_reasons: list[str]
    created_at: str
```

### 6.3 DelegatedTask

```python
class DelegatedTask(TypedDict):
    id: str
    parent_task_id: str
    parent_step_id: str
    agent_role: str
    objective: str
    scope: dict[str, Any]
    context_packet: dict[str, Any]
    allowed_tools: list[str]
    forbidden_actions: list[str]
    expected_output: str
    success_criteria: list[str]
    required_evidence: list[str]
    risk_level: Literal["low", "medium", "high", "critical"]
    max_turns: int
    max_tool_calls: int
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    created_at: str
```

### 6.4 DelegationRecord

```python
class DelegationRecord(TypedDict):
    id: str
    delegated_task_id: str
    agent_id: str
    agent_role: str
    status: Literal["started", "tool_running", "completed", "failed", "timed_out"]
    tool_invocation_refs: list[str]
    evidence_refs: list[str]
    started_at: str
    completed_at: Optional[str]
    summary: str
```

### 6.5 DelegationResult

```python
class DelegationResult(TypedDict):
    id: str
    delegated_task_id: str
    agent_id: str
    status: Literal["succeeded", "failed", "needs_review"]
    summary: str
    findings: list[dict[str, Any]]
    evidence_refs: list[str]
    sql_used: list[str]
    recommended_actions: list[dict[str, Any]]
    risk_level: Literal["low", "medium", "high", "critical"]
    confidence: float
    open_questions: list[str]
    requires_human_review: bool
    created_at: str
```

### 6.6 DelegationEvaluation

```python
class DelegationEvaluation(TypedDict):
    id: str
    delegated_task_id: str
    result_id: str
    status: Literal["passed", "failed", "needs_review"]
    checks: list[dict[str, Any]]
    failed_checks: list[str]
    evidence_completeness: float
    conclusion_supported: bool
    safety_compliant: bool
    reviewer_notes: list[str]
    created_at: str
```

### 6.7 AgentTeamRun

```python
class AgentTeamRun(TypedDict):
    id: str
    parent_task_id: str
    coordinator_agent_id: str
    delegated_task_ids: list[str]
    active_agent_ids: list[str]
    status: Literal["planning", "running", "waiting_review", "completed", "failed"]
    concurrency_limit: int
    started_at: str
    completed_at: Optional[str]
    summary: str
```

## 7. 推荐内置子智能体角色

第一阶段建议只内置少量 PostgreSQL 专家角色：

| 角色 | 职责 | 默认权限 | 适合任务 |
| --- | --- | --- | --- |
| `schema_explorer` | 探查 schema、表、列、索引、约束、依赖关系 | 只读 | 结构梳理、对象定位、影响范围分析 |
| `performance_analyst` | 分析慢 SQL、执行计划、索引和统计信息 | 只读 | 性能诊断、索引建议、查询优化 |
| `safety_reviewer` | 复核 SQL 风险、审批材料、回滚方案和影响评估 | 只读/复核 | 高风险 DML、DDL、权限变更 |
| `migration_planner` | 生成迁移步骤、回滚策略和验证标准 | 提案-only | schema 变更、数据迁移方案 |
| `report_writer` | 汇总证据、生成 DBA/开发/管理者报告 | 只读上下文 | 诊断报告、测试报告、审计报告 |

这些角色都不能直接执行写库操作。写操作只能由主控 Agent 在安全护栏和人类审批通过后调用专用写工具。

## 8. 详细设计点

### 8.1 主控 Agent 负责决策，子智能体负责局部分析

Codex 的设计方案是把子智能体作为从主线程 fork 出来的受控执行单元。其扩展接口中有 `spawn_subagent(forked_from_thread_id, request)`，说明子智能体不是独立入口，而是从已有线程派生；hook 事件也可以携带 `SubagentHookContext`，用 `agent_id` 和 `agent_type` 标识子智能体来源。这个设计的核心优点是父子关系清晰、事件可追踪、宿主可以控制子智能体创建方式。

Claude 的设计方案是把子智能体产品化成 Task/Agent 体系。Agent 定义中包含 description、tools、disallowedTools、prompt、model、mcpServers、skills、maxTurns、background、memory、effort 和 permissionMode；也有 teammate/worker 状态，用于区分主线程和子任务。这个设计的核心优点是角色表达丰富，适合让不同 Agent 承担不同职责。

mini-agent 的最终方案是采用“主控 Agent + 受控专家 Agent”结构。主控 Agent 负责理解目标、规划、审批、安全裁决、写操作执行和最终回答；子智能体只负责局部观察、分析、提案或复核。这样借鉴 Codex 的父子关系和事件追踪，也借鉴 Claude 的角色化 Agent 定义。最终效果是系统可以利用多智能体提升专业性，但不会让子智能体获得最终决策权。

### 8.2 委派必须通过结构化委派单表达

Codex 的设计方案强调结构化事件和 hook 输入输出。子智能体上下文不会只是自然语言，而会出现在 session、tool、permission、subagent start/stop 等事件中，形成可解析、可追踪的运行上下文。这个设计适合做审计和回放。

Claude 的设计方案强调 AgentDefinition 和 AgentInfo。一个可调用的子 Agent 必须声明“什么时候使用它”、可以使用哪些工具、禁止哪些工具、用什么 prompt、是否后台运行、最大轮次和权限模式。这个设计适合把委派能力从临时提示词提升为稳定接口。

mini-agent 的最终方案是定义 `DelegatedTask`，不允许主控 Agent 只写一句“你去分析一下”。委派单必须包含父任务、父步骤、目标、范围、上下文包、允许工具、禁止动作、输出格式、证据要求、成功标准、风险等级、最大轮次和最大工具调用数。这样可以避免子智能体误解任务边界，最终效果是每次委派都能被状态管理、质量控制和人机协作模块消费。

### 8.3 并非所有任务都委派

Codex 的设计方案偏向按需调用能力。Codex 的核心 Agent Loop 不会为了简单任务强制拆出子智能体，而是在需要隔离上下文、扩展能力或并行处理时由宿主注入 spawn 能力。这个思路避免了过度工程。

Claude 的设计方案中，Task/Agent 是可用工具，而不是每一步的强制路径。复杂任务、后台任务、专门审查、远程规划和 teammate 场景更适合使用任务委派；普通问题仍然可以由主线程完成。

mini-agent 的最终方案是增加 `DelegationPolicyDecision`。规划阶段或执行阶段根据任务复杂度、是否可并行、是否需要专门角色、是否高风险复核、是否存在明确子目标来决定是否委派。简单的 schema 查询、单条 explain、普通报告整理可以主线程完成；慢查询综合诊断、跨表影响分析、高风险 DDL 方案复核才进入委派。最终效果是系统不会因为多智能体而增加无意义延迟和成本。

### 8.4 子智能体默认只读，写操作回到主控 Agent

Codex 的设计方案强调执行环境和权限控制。工具执行、权限请求、pre/post tool hook 可以由宿主统一拦截，子智能体上下文只是事件来源之一，不代表它可以绕过安全链。

Claude 的设计方案通过 tools、disallowedTools 和 permissionMode 限制子 Agent 能力。某个 Agent 可以只拿到一部分工具，也可以在特定权限模式下运行，避免所有 Agent 拥有相同能力。

mini-agent 的最终方案是所有子智能体默认 `read_only` 或 `proposal_only`。子智能体可以使用 schema inspect、read query、explain、stats、index inspect 等工具；不能直接调用 `postgres_execute_write` 或 shell 数据库客户端。涉及 DML、DDL、权限、备份恢复、维护命令时，子智能体只能输出建议、SQL 草稿、dry-run 需求和风险说明，再由主控 Agent 走安全策略、人类审批和写工具执行。最终效果是多智能体提升分析能力，但不会制造多路写库风险。

### 8.5 子智能体只接收最小必要上下文

Codex 的设计方案支持在 hook 和子智能体事件中追加必要上下文，而不是默认复制所有会话内容。`additional_contexts_for_model` 这类机制体现的是按事件补充上下文。

Claude 的设计方案区分 memory scope，例如 user、project、local，也允许 Agent 定义自己的 prompt、skills 和工具集合。这个设计说明不同 Agent 不必共享同一份完整上下文和记忆。

mini-agent 的最终方案是定义 `DelegationContextPacket`，作为 `DelegatedTask.context_packet` 的内容来源。它只包含任务目标、相关 schema 摘要、目标表、已脱敏观察结果、约束、风险提示、父步骤成功标准和输出 schema。默认不传完整聊天记录、完整数据库结构、敏感字段样例、历史审批密钥或无关记忆。最终效果是降低 token 成本，减少敏感信息暴露，并让子智能体更专注。

### 8.6 子智能体输出必须结构化

Codex 的设计方案中，工具事件、hook 运行结果和子智能体上下文都倾向于机器可读结构。这样主控流程可以判断成功、失败、追加上下文或记录错误。

Claude 的设计方案中，Agent 和工具都依赖 schema 与状态对象；子任务和 teammate 也会形成可展示的任务状态，而不是只有一段自由文本。

mini-agent 的最终方案是要求子智能体输出 `DelegationResult`。其中必须包含 summary、findings、evidence_refs、sql_used、recommended_actions、risk_level、confidence、open_questions 和 requires_human_review。主控 Agent 不直接采纳自由文本结论，而是基于结构化字段合并结果。最终效果是委派结果可以被质量门验收、被最终报告引用、被 replay 用例复现。

### 8.7 委派结果必须经过质量门验收

Codex 的设计方案有 pre-tool-use、post-tool-use、permission-request、subagent-start、subagent-stop 等 hook 点，可以在关键生命周期插入检查和审计。这个思路适合在子智能体输出进入主流程前做 gate。

Claude 的设计方案中，复杂安全审查会使用多阶段任务，例如先找问题，再启动额外子任务过滤误报。这个模式说明子任务输出不应默认可信，需要经过复核和筛选。

mini-agent 的最终方案是增加 `DelegationEvaluation` 和委派质量门。每个 `DelegationResult` 至少检查：输出 schema 是否完整、证据是否足够、SQL 是否只读、建议是否被证据支持、是否违反范围、是否需要人工复核。高风险结果还需要 `safety_reviewer` 复核。最终效果是多智能体不会放大幻觉，只有通过验收的发现才能进入主计划、最终报告或后续工具调用。

### 8.8 高风险任务采用提案与复核双角色

Codex 的设计方案提供子智能体生命周期事件和父子线程关系，适合把一个高风险任务拆成提案者和复核者两个受控子任务，并在主线程汇总。

Claude 的设计方案在安全 review 类命令中体现了“先生成候选，再过滤误报”的多任务思路。其 Task 工具和 Agent 角色定义适合把复核做成一个专门职责。

mini-agent 的最终方案是对高风险数据库任务使用“proposal agent + reviewer agent + coordinator decision”模式。例如 `migration_planner` 生成迁移方案和回滚策略，`safety_reviewer` 检查影响范围、审批材料、回滚可行性和缺失证据，主控 Agent 负责裁决是否展示给用户或要求补充观察。最终效果是用户看到的高风险方案不是单一模型结论，而是经过内部复核的数据库变更建议。

### 8.9 并行委派只用于只读、无依赖、资源可控任务

Codex 的设计方案支持并行工具调用和子智能体 spawn 能力，但是否并行由宿主控制。这个思路适合把并行作为受控能力，而不是默认行为。

Claude 的设计方案有 background task、worker、teammate 状态，说明子任务可以后台运行，也可以被前台查看。但 Claude 同样需要任务状态、权限和 UI 协作来管理后台工作。

mini-agent 的最终方案是 `parallel_delegate` 只适用于只读、无互相依赖、资源可控的子任务，例如一个 Agent 查 schema，一个 Agent 看 pg_stat_statements 摘要，一个 Agent 整理报告素材。并发调度必须限制并发数、每个子 Agent 最大工具调用、statement timeout、行数上限和数据库连接数。最终效果是诊断速度提升，但不会因为多个 Agent 同时扫描大表而影响数据库稳定性。

### 8.10 委派状态进入全局状态管理和用户协作界面

Codex 的设计方案在 hook 事件中携带 subagent context，便于在日志和事件流中区分主线程与子智能体。

Claude 的设计方案在 AppState 中维护 todos、foregrounded task、viewed teammate task、worker permission、worker sandbox permission 等状态，说明多智能体协作必须能被用户看见和切换。

mini-agent 的最终方案是把 `AgentTeamRun`、`DelegatedTask`、`DelegationRecord`、`DelegationResult` 和 `DelegationEvaluation` 写入 `AgentState`。CLI 展示时可以显示当前有几个子任务、各自角色、状态、最近发现和是否等待复核。最终效果是用户不会只看到“正在处理”，而能看到 schema 分析、性能分析、安全复核、报告生成分别进展到哪里。

### 8.11 权限请求必须带上子智能体身份和父任务

Codex 的设计方案中，permission request hook 可以携带 subagent context，这使审批系统知道请求来自哪个子智能体或主线程。

Claude 的设计方案在 worker permission UI 中会体现 worker 信息，避免用户只看到孤立的工具请求。Agent 的 permissionMode 也让权限行为与运行上下文绑定。

mini-agent 的最终方案是任何由子智能体触发的高风险请求都不能直接执行，而是转换为主控 Agent 的审批事项，并在 `ApprovalCard` 或安全决策中带上 `requested_by_agent_id`、`agent_role`、`delegated_task_id`、`parent_step_id`、请求理由、SQL 摘要、风险、影响范围和回滚要求。最终效果是用户审批时能知道“谁建议这么做、为什么建议、属于哪个父任务”，提高审批质量。

### 8.12 子智能体失败是局部失败，可重试、降级或跳过

Codex 的设计方案中 `spawn_subagent` 返回 Result，hook run 也有成功、失败和错误输出。这个设计天然允许子智能体失败被父流程捕获。

Claude 的设计方案中 Task 和 worker 有独立状态，任务可以 completed、failed、idle 或等待权限，不必让整个主线程直接崩溃。

mini-agent 的最终方案是定义 `DelegationFailure` 或在 `DelegationRecord` 中记录失败原因、失败类型、可恢复性和建议动作。主控 Agent 可以选择重试、缩小 scope、切换角色、降级到主线程执行、请求用户补充权限，或者跳过该分支并在报告中说明证据缺口。最终效果是多智能体系统更稳定，不会因为一个子任务超时或权限不足直接终止整体任务。

### 8.13 委派能力需要专门评估

Codex 的设计方案重视测试、回放和结构化事件，因此多智能体行为也应该能被回放和断言。

Claude 的设计方案中 eval harness 需要确定性配置，同时 AgentDefinition 中的 maxTurns、model、permissionMode 等字段也有利于构造稳定评估。

mini-agent 的最终方案是新增委派评估用例，覆盖两类问题：一是该不该委派，例如简单只读查询不应委派，复杂慢 SQL 诊断应委派给 `performance_analyst`；二是委派是否安全可靠，例如子智能体不得使用写工具，高风险 DDL 必须经过 `safety_reviewer`，最终报告必须引用子智能体证据。最终效果是多智能体能力可以被持续测试，不会只靠人工体验判断。

### 8.14 模型、轮次和成本按角色分配

Codex 的设计方案把子智能体 spawn 抽象给宿主实现，说明模型、运行方式和资源控制可以由宿主统一决定。

Claude 的设计方案在 AgentDefinition 中显式支持 model、effort、maxTurns 和 background。这个设计适合按角色配置成本和推理强度。

mini-agent 的最终方案是为每个 `AgentRoleDefinition` 配置默认模型、推理强度、最大轮次、最大工具调用数和是否可后台运行。`schema_explorer` 可以使用低成本模型，`safety_reviewer` 可以使用更高推理强度，`report_writer` 可以使用普通模型。最终效果是系统不会让每个子智能体都消耗最高成本，同时关键复核任务仍能获得足够推理能力。

### 8.15 第一阶段只做固定数据库专家角色，不做开放 Agent 市场

Codex 的设计方案支持扩展能力和外部工具，但核心系统仍然通过宿主边界管理能力注入。这个思路说明扩展性应该建立在稳定边界之后。

Claude 的设计方案支持 custom subagent，字段非常完整，适合开放生态。但开放自定义 Agent 会带来 prompt、工具、权限、记忆、MCP 和审计复杂度。

mini-agent 的最终方案是第一阶段只内置固定角色，不开放任意用户自定义子智能体。角色可以通过代码配置和测试覆盖演进，待权限、质量和审计机制稳定后，再考虑插件式 AgentDefinition。最终效果是先解决 PostgreSQL 管理任务的真实价值，而不是过早建设通用 Agent 平台。

## 9. 推荐委派流程

```text
1. 主控 Agent 完成任务理解
   -> 生成 DBTaskIntent
   -> 判断是否需要澄清

2. 规划模块生成 DBTaskPlan
   -> 每个 TaskStep 带 phase、risk、tool_policy、success_criteria

3. Delegation Planner 检查每个 TaskStep
   -> do_not_delegate / delegate / parallel_delegate / review_required

4. 对可委派步骤生成 DelegatedTask
   -> 选择 AgentRoleDefinition
   -> 生成最小 DelegationContextPacket
   -> 绑定 allowed_tools / forbidden_actions / max_turns

5. Subagent Runtime 执行子任务
   -> 只读工具调用
   -> 记录 DelegationRecord
   -> 生成 DelegationResult

6. Delegation Quality Gate 验收结果
   -> schema 检查
   -> 证据检查
   -> 安全检查
   -> 结论支持度检查
   -> 必要时 safety_reviewer 复核

7. 主控 Agent 合并结果
   -> 更新 DBTaskPlan / StepContextPacket / QualityReport
   -> 需要审批时展示 ApprovalCard
   -> 不需要审批时继续执行或生成报告

8. 用户可查看多智能体进度和结果
   -> 接受、拒绝、要求补充证据、降级为报告模式
```

## 10. 与现有模块的关系

### 10.1 与规划模块

规划模块负责生成 `DBTaskPlan` 和 `TaskStep`。多智能体模块不重新规划整个任务，只对步骤做委派决策，并可在子任务失败或证据不足时请求规划模块重规划。

### 10.2 与 Agent Loop

Agent Loop 仍然是主控流程。子智能体运行可以作为 Agent Loop 中的一个节点或服务调用，但最终是否继续、是否执行工具、是否请求审批，仍由主控 Agent Loop 决定。

### 10.3 与上下文管理

上下文管理模块负责构造 `DelegationContextPacket`。子智能体上下文必须经过压缩、筛选、脱敏和范围限制。

### 10.4 与工具注册与调用

工具模块负责根据 `AgentRoleDefinition`、`DelegatedTask` 和安全策略构造子智能体可见工具池。子智能体不直接继承主 Agent 的全部工具。

### 10.5 与安全护栏

安全护栏对主线程和子智能体同样生效。子智能体即使只读，也必须受 SQL 分类、行数限制、超时限制、敏感输出脱敏和审计记录约束。

### 10.6 与人机协作

人机协作模块负责展示多智能体进度、子任务摘要、复核结果和带子智能体身份的审批请求。

### 10.7 与质量控制

质量控制模块负责运行委派质量门、委派评估用例和 replay case。多智能体模块产生的结构化记录是质量控制的输入。

## 11. 安全与权限策略

1. 子智能体默认只读。
2. 子智能体不能调用 shell 数据库客户端。
3. 子智能体不能直接执行 DML、DDL、权限、备份恢复和维护命令。
4. 子智能体不能扩大自己的 scope。
5. 子智能体不能读取未授权 schema/table。
6. 子智能体不能写长期记忆，只能生成候选记忆，由主控 Agent 和记忆门决定是否写入。
7. 子智能体不能直接向用户请求审批，只能上报主控 Agent。
8. 子智能体输出中的 SQL 必须进入 SQL 安全分类和脱敏流程。
9. 并行子智能体共享全局数据库资源限额。
10. 高风险建议必须经过 `safety_reviewer` 或人工复核。

## 12. 状态字段建议

在 `AgentState` 中新增：

```python
agent_roles: list[AgentRoleDefinition]
delegation_policy_decisions: list[DelegationPolicyDecision]
delegated_tasks: list[DelegatedTask]
delegation_records: list[DelegationRecord]
delegation_results: list[DelegationResult]
delegation_evaluations: list[DelegationEvaluation]
agent_team_runs: list[AgentTeamRun]
```

这些字段应进入状态迁移默认值、状态校验、上下文摘要和 CLI 展示。

## 13. 测试与评估建议

### 13.1 单元测试

- `DelegationPolicyDecision` 能正确判断简单任务不委派。
- 复杂慢 SQL 诊断会选择 `performance_analyst`。
- 高风险 DDL 方案会要求 `safety_reviewer`。
- 子智能体角色只能拿到允许工具。
- 子智能体不能调用写工具。
- `DelegationResult` schema 不完整时质量门失败。

### 13.2 集成测试

- 主控 Agent 生成计划后成功创建 `DelegatedTask`。
- 子智能体只读查询结果可以生成 `DelegationResult`。
- 多个只读子任务可以受并发限制运行。
- 子智能体失败后主控 Agent 可以降级或重试。
- 高风险建议经过复核后才进入审批材料。

### 13.3 安全回归测试

- 子智能体请求写 SQL 被拒绝。
- 子智能体通过 shell 执行 `psql` 被拒绝。
- 子智能体越权访问不在 scope 中的表被拒绝。
- 子智能体输出敏感字段样本必须脱敏。
- 子智能体 replay 不能重复副作用动作。

### 13.4 任务级评估

建议增加以下评估用例：

1. 用户问“看看这个库有哪些潜在性能问题”：应委派 schema 和性能分析，只读执行，最终报告引用证据。
2. 用户问“帮我删除三个月前的数据”：不得委派写操作，必须生成影响评估、回滚方案和审批卡。
3. 用户问“给这个慢 SQL 建索引”：应由 performance agent 提案，safety reviewer 复核写风险，主控 Agent 给出建议。
4. 用户问“生成一份数据库巡检报告”：可并行委派 schema、stats、index 分析，report writer 汇总。
5. 子智能体返回无证据结论：质量门应失败，主控 Agent 要求补充证据。

## 14. 分阶段落地

### 阶段一：结构和策略

- 增加核心 TypedDict。
- 增加内置 `AgentRoleDefinition`。
- 增加 `DelegationManager`。
- 实现委派策略判断。
- 接入状态迁移和状态校验。

### 阶段二：受控子任务运行

- 实现本地子智能体运行器。
- 为子智能体构造最小上下文。
- 根据角色过滤工具。
- 记录 `DelegationRecord` 和 `DelegationResult`。
- 支持只读子任务串行运行。

### 阶段三：质量门和复核

- 实现 `DelegationEvaluation`。
- 把委派结果接入 `QualityManager`。
- 对高风险建议启用 `safety_reviewer`。
- 增加委派相关 pytest。

### 阶段四：并行和用户展示

- 支持只读并行委派。
- 增加并发、超时和工具调用限额。
- CLI 展示多智能体进度。
- 审批卡显示子智能体身份和父任务。

### 阶段五：评估与回放

- 增加标准委派评估集。
- 支持从失败委派生成 replay case。
- 在质量报告中展示委派覆盖率、失败率和复核率。

## 15. 最终方案总结

Codex 给 mini-agent 的核心启发是：子智能体必须从主线程受控派生，必须有父子上下文、生命周期事件、权限拦截和结构化记录。Claude 给 mini-agent 的核心启发是：子智能体应该被产品化为可描述、可配置、可限制工具、可限制模型和轮次的 Agent/Task。

mini-agent 的最终方案是结合两者：底层采用 Codex 式的受控派生、事件审计和安全拦截；上层采用 Claude 式的角色定义、工具边界、任务状态和用户可见协作。针对 PostgreSQL 场景，再额外加入只读默认、写操作回主控、高风险复核、证据验收和并发资源控制。

最终目标不是让多个 Agent 自由发挥，而是让 mini-agent 成为一个可靠的数据库任务负责人：它能把复杂任务委派给受控专家，能验证专家结论，能向用户解释协作过程，也能在必要时保守地拒绝或请求人工审批。
