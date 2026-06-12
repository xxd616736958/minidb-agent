# PostgreSQL 管理智能体：模型抽象与路由模块设计

## 1. 背景

mini-agent 当前已经具备 PostgreSQL 管理智能体的主要能力：任务理解、规划与任务分解、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 工具、执行环境与工作区管理、状态管理、安全护栏、人机协作、错误处理、质量控制、多智能体与任务委派。

但当前模型层仍然比较薄：

```text
agent/llm_factory.py
  -> 根据 LLM_PROVIDER 创建 DeepSeek 或 OpenAI
  -> 使用统一 LLM_MODEL
  -> 节点手动传 temperature / max_tokens
  -> create_llm_with_tools(tools)
```

这个设计适合早期原型，但对 PostgreSQL 管理智能体不够。数据库 Agent 的模型调用不是一种：

- 任务理解需要稳定分类和澄清判断。
- 规划需要结构化输出和安全流程意识。
- Agent Loop 需要可靠工具调用。
- SQL 安全复核需要更强推理和低温稳定性。
- 记忆压缩需要低成本摘要模型。
- 报告生成需要可读性和证据引用。
- 子智能体需要按角色继承或覆盖模型。
- 高风险生产库任务不能静默降级到弱模型。
- 质量评估需要知道每次模型调用用了什么模型、为什么选它、表现如何。

因此，模型抽象与路由模块的目标是把 mini-agent 从“能调用一个 LLM”升级为“能按任务、风险、上下文、工具需求和质量策略选择模型，并能记录、评估和安全降级”的数据库管理智能体。

## 2. 模块定位

模型抽象与路由模块位于业务节点和底层 provider 之间：

```text
Agent Node / Subagent / Quality Eval
  -> ModelTask
  -> ModelRouter
  -> ModelProfile / ProviderProfile
  -> ModelInvocationPolicy
  -> LLM Factory / Provider Adapter
  -> ModelInvocationRecord
  -> Model Quality Gate
```

输入：

- 当前 `AgentState`
- `DBTaskIntent`
- `TaskStep`
- `DelegatedTask`
- 工具可见性需求
- 上下文 token 预算
- 数据库环境风险
- 用户指定模型或环境配置
- 模型能力目录

输出：

- `ModelProfile`
- `ModelRoute`
- `ModelInvocationPolicy`
- `ModelInvocationRecord`
- `ModelFallbackDecision`
- `ModelEvaluationResult`
- `QualityGate`

本模块不替代 LLM provider SDK，也不替代业务节点。它负责决定“本次模型任务应该用什么模型、什么参数、是否允许工具、是否允许降级、调用结果如何记录和评估”。

## 3. 设计目标

1. 建立统一模型能力目录，不再只依赖单个 `LLM_MODEL`。
2. 把模型调用场景抽象成 `ModelTask`。
3. 根据任务类型、数据库风险、工具需求、上下文大小和子智能体角色选择模型。
4. 支持用户/环境指定模型，但必须经过 allowlist 和能力校验。
5. 支持子智能体模型继承与角色覆盖。
6. 用场景模板统一 temperature、max_tokens、timeout、reasoning_effort、streaming 和工具绑定。
7. 让工具绑定受模型能力约束。
8. 记录每次模型调用，服务质量评估、成本分析和问题复盘。
9. 支持安全降级和必要升级，但高风险任务不能静默降级。
10. 把模型路由纳入评估和质量门。
11. 支持多 provider，不把 DeepSeek/OpenAI 写死在业务节点。
12. 对结构化输出场景做强约束解析和失败修复。
13. 把上下文窗口作为路由条件。
14. 把模型可见性纳入安全红线。
15. 在 CLI 和报告中展示模型路由摘要。

## 4. 非目标

1. 不在第一阶段实现完整在线模型 benchmark 平台。
2. 不让模型路由绕过用户配置和安全策略。
3. 不根据模型评估分数绕过 SQL 安全护栏。
4. 不要求所有 provider 支持完全相同能力。
5. 不把模型选择交给 LLM 自己决定。
6. 不在业务节点里继续硬编码具体模型名。
7. 不把模型降级作为高风险数据库任务的默认容错方式。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经有以下基础：

- `agent/llm_factory.py` 已统一创建 DeepSeek/OpenAI 模型。
- `create_llm_no_tools()` 用于规划、记忆压缩等无工具场景。
- `create_llm_with_tools()` 用于 Agent Loop 工具调用场景。
- `agent/config.py` 已有 `LLM_PROVIDER`、`LLM_MODEL`、temperature、max_tokens、timeout。
- 任务理解、规划、Agent Loop、记忆压缩已经通过统一工厂调用模型。
- 多智能体模块中的 `AgentRoleDefinition.default_model` 已预留角色模型字段。
- 质量模块已有 `QualityGate`、`EvaluationCase`、`EvaluationResult`、`QualityReport`。

### 5.2 主要不足

1. 没有模型能力目录，系统不知道某个模型是否支持工具、长上下文、结构化输出或高风险复核。
2. 没有模型任务类型，节点只能手动传参数。
3. 没有模型路由器，所有任务默认使用同一个模型。
4. 没有模型调用记录，无法复盘成本、失败率和输出质量。
5. 没有模型降级/升级策略。
6. 没有模型 allowlist、禁止用途和高风险红线。
7. 子智能体模型继承/覆盖还只是字段，没有运行时策略。
8. 质量评估还不能断言“某类任务必须使用某类模型”。
9. CLI 和最终报告看不到关键结论由哪个模型产生。

## 6. 推荐核心对象

### 6.1 ModelProfile

```python
class ModelProfile(TypedDict):
    id: str
    provider: Literal["openai", "deepseek", "local", "custom"]
    model_id: str
    aliases: list[str]
    display_name: str
    description: str
    context_window_tokens: int
    max_output_tokens: int
    supports_tools: bool
    supports_structured_output: bool
    supports_streaming: bool
    supports_reasoning_effort: bool
    supports_parallel_tool_calls: bool
    supports_long_context: bool
    cost_tier: Literal["cheap", "standard", "premium"]
    quality_tier: Literal["fast", "balanced", "strong", "review"]
    allowed_tasks: list[str]
    forbidden_tasks: list[str]
    allowed_data_sensitivity: Literal["public", "internal", "sensitive", "secret"]
    deprecated: bool
```

### 6.2 ModelTask

```python
ModelTask = Literal[
    "intent_understanding",
    "planning",
    "tool_reasoning",
    "sql_safety_review",
    "delegation_worker",
    "delegation_reviewer",
    "error_recovery",
    "memory_compaction",
    "report_generation",
    "quality_evaluation",
]
```

### 6.3 ModelInvocationPolicy

```python
class ModelInvocationPolicy(TypedDict):
    task: ModelTask
    temperature: float
    max_tokens: int
    timeout_seconds: float
    reasoning_effort: Optional[Literal["low", "medium", "high", "max"]]
    streaming: bool
    tools_allowed: bool
    structured_output_required: bool
    allow_fallback: bool
    allow_downshift: bool
    require_review_model: bool
```

### 6.4 ModelRoute

```python
class ModelRoute(TypedDict):
    id: str
    task: ModelTask
    selected_model_id: str
    provider: str
    reason: str
    required_capabilities: list[str]
    risk_level: str
    context_tokens_estimate: int
    tools_bound: list[str]
    policy: ModelInvocationPolicy
    fallback_chain: list[str]
    created_at: str
```

### 6.5 ModelInvocationRecord

```python
class ModelInvocationRecord(TypedDict):
    id: str
    route_id: str
    task: ModelTask
    provider: str
    model_id: str
    step_id: Optional[str]
    delegated_task_id: Optional[str]
    quality_gate_id: Optional[str]
    input_tokens_estimate: int
    output_tokens_estimate: int
    duration_ms: Optional[int]
    tools_bound: list[str]
    structured_output_schema: Optional[str]
    status: Literal["pending", "succeeded", "failed", "fallback_used"]
    error_type: Optional[str]
    fallback_from: Optional[str]
    cost_estimate: Optional[float]
    created_at: str
```

### 6.6 ModelFallbackDecision

```python
class ModelFallbackDecision(TypedDict):
    id: str
    invocation_id: str
    from_model_id: str
    to_model_id: Optional[str]
    decision: Literal["retry_same_model", "downshift", "upgrade", "ask_user", "fail_closed"]
    reason: str
    allowed_by_policy: bool
    created_at: str
```

### 6.7 ModelEvaluationResult

```python
class ModelEvaluationResult(TypedDict):
    id: str
    model_id: str
    task: ModelTask
    case_id: str
    status: Literal["passed", "failed", "needs_review"]
    scores: dict[str, float]
    failure_modes: list[str]
    safety_notes: list[str]
    cost_estimate: Optional[float]
    latency_ms: Optional[int]
    created_at: str
```

## 7. 详细设计点

### 7.1 建立统一模型能力目录

Codex 的设计方案是把 provider 和能力上限拆开管理。模型 provider 不只是一个 API endpoint，而会暴露 capabilities，例如是否支持 reasoning summaries、parallel tool calls、image detail、search 等。Codex 的工具配置和运行时会根据这些能力决定功能可见性，并把 model、provider、reasoning_effort 等记录为运行事实。

Claude 的设计方案是维护模型配置、模型别名、模型能力和展示信息。它有模型 alias、canonical model id、provider-specific model strings、capability cache、`ModelInfoSchema`，能表达模型 displayName、description、是否支持 effort、是否支持 adaptive thinking、fast mode 和 auto mode。

mini-agent 的最终方案是新增 `ModelProfile` 和 `ModelRegistry`。模型不再只是一个字符串，而是带有 provider、能力、上下文窗口、工具支持、结构化输出支持、成本等级、质量等级、允许任务和禁止任务的能力对象。这样借鉴 Codex 的 provider capability 边界，也借鉴 Claude 的模型目录和 alias 体系。最终效果是 PostgreSQL Agent 可以按能力选模型，避免把不支持工具调用或不适合高风险复核的模型用到关键路径上。

### 7.2 把模型调用场景抽象成 ModelTask

Codex 的设计方案更偏统一运行时事实记录，同一套模型配置会被核心执行路径消费，调用结果也会进入 telemetry 和 analytics。它不会让每个业务节点完全随意地解释模型用途。

Claude 的设计方案中主循环模型、子 Agent 模型、技能模型、验证模型都有明确上下文。`QueryEngine` 中会维护 `mainLoopModel`，技能和子 Agent 也可以声明模型或继承模型。

mini-agent 的最终方案是定义 `ModelTask`。业务节点不再直接说“我要 deepseek-chat，temperature=0”，而是声明“我要做 planning / sql_safety_review / report_generation”。模型路由器再根据 `ModelTask` 选模型和参数。这样可以把模型选择从节点实现中抽离出来。最终效果是以后调整规划模型或安全复核模型时，不需要改所有节点代码。

### 7.3 设计 ModelRouter 按任务、风险和能力路由

Codex 的设计方案说明 provider capability 是运行时约束。某些功能只有在模型支持时才可用，不能假设所有模型都具备相同能力。

Claude 的设计方案中 `getModelForRuntime` 会根据运行上下文选择模型，例如主循环模型、权限模式、上下文压力等因素会影响最终模型；用户指定模型、默认模型和 fallback model 也会参与决策。

mini-agent 的最终方案是新增 `ModelRouter.route(task, state)`。它根据 `ModelTask`、当前步骤风险、数据库环境、上下文 token 估算、是否需要工具、是否需要结构化输出、是否是子智能体角色来选择模型。普通报告可用便宜模型，规划可用 balanced 模型，高风险 SQL 复核必须使用 review 模型，长 schema 巡检优先长上下文模型。最终效果是模型选择和数据库风险绑定，质量和成本可以同时控制。

### 7.4 用户指定模型必须经过 allowlist 和能力校验

Codex 的设计方案中 provider 配置、认证、base_url 和 provider info 都是受控输入，运行时会基于 provider metadata 创建模型管理器。

Claude 的设计方案允许 `/model`、启动参数、环境变量和 settings 指定模型，但会解析 alias、处理 allowlist、模型覆盖、provider-specific 字符串和模型验证。

mini-agent 的最终方案是允许环境变量配置默认模型、规划模型、安全复核模型、报告模型和子智能体模型，但所有配置必须经过 `ModelRegistry` 校验。校验内容包括模型是否在 allowlist、是否支持 required capabilities、是否被 deprecated、是否允许处理当前数据敏感级别、是否允许用于高风险任务。最终效果是配置灵活，但不会因为用户随手配置一个不支持工具调用的模型导致 Agent Loop 失效。

### 7.5 子智能体模型支持继承和角色覆盖

Codex 的设计方案中 subagent 从主线程 fork，父子上下文清晰，子智能体不是完全独立的模型入口。

Claude 的设计方案中 AgentDefinition 支持 `model` 字段，默认可以 inherit parent model；子 Agent 还可以根据 alias 解析出最终模型，避免在特定 provider 或区域中意外降级。

mini-agent 的最终方案是在 `AgentRoleDefinition.default_model` 上支持 `inherit`、具体 alias 和策略名，例如 `cheap`、`balanced`、`strong_review`。`schema_explorer`、`performance_analyst`、`report_writer` 默认继承或使用低成本模型；`safety_reviewer` 和高风险 `migration_planner` 使用 review 模型。最终效果是多智能体不会让成本失控，也不会让高风险复核被弱模型处理。

### 7.6 模型参数按场景模板化

Codex 的设计方案会把 reasoning_effort 作为模型运行事实记录下来，说明模型行为参数不只是临时调用参数。

Claude 的设计方案支持 effort、fast mode、auto mode、thinking budget 等模型行为配置，并在模型信息里表达支持情况。

mini-agent 的最终方案是定义 `ModelInvocationPolicy`。每个 `ModelTask` 都有默认 temperature、max_tokens、timeout、reasoning_effort、streaming、tools_allowed、structured_output_required、allow_fallback 等参数。任务理解、规划、安全复核默认低温；报告生成可以稍微放宽但必须引用证据；记忆压缩用低成本短输出；高风险复核禁止静默降级。最终效果是模型调用稳定性更高，节点不再散落参数魔法数。

### 7.7 工具绑定受模型能力和路由结果约束

Codex 的设计方案中工具是否可见会受模型能力、provider 能力、运行上下文和工具配置影响。

Claude 的设计方案中工具权限、AgentDefinition、permissionMode 和模型能力共同决定模型能做什么，子 Agent 也可以限制 allowed/disallowed tools。

mini-agent 的最终方案是让 `ModelRoute` 明确 `tools_allowed`、`tools_bound`、`supports_tools` 和 `supports_parallel_tool_calls`。`create_llm_with_tools()` 不能只看工具列表，还必须检查选中模型是否支持可靠工具调用。若模型不支持工具调用，只能用于报告、总结、评估或记忆压缩。最终效果是工具安全边界更稳定，避免把 PostgreSQL 工具绑定给不适合的模型。

### 7.8 记录每次模型调用

Codex 的设计方案会记录 model_slug、model_provider、reasoning_effort、model usage、ModelDownshift 等事实，用于 analytics 和问题定位。

Claude 的设计方案会维护 `modelUsage`，在 QueryEngine 的消息、会话和结果中持续带上模型使用信息。

mini-agent 的最终方案是新增 `ModelInvocationRecord`。每次 LLM 调用都记录 route_id、task、provider、model_id、step_id、delegated_task_id、token 估算、耗时、是否绑定工具、结构化 schema、状态、错误、fallback_from 和成本估算。最终效果是后续可以知道“哪个模型在 SQL 安全复核中失败率高”“哪个模型成本最高”“哪个步骤发生了降级”。

### 7.9 支持降级和升级，但高风险任务不能静默降级

Codex 的设计方案中存在 ModelDownshift 运行事实，说明模型可能因为策略或运行约束发生下调，并且这个变化需要被记录。

Claude 的设计方案中有 fallbackModel，也会在第三方 provider 不支持某些模型时给 fallback suggestion。

mini-agent 的最终方案是定义 `ModelFallbackDecision`。低风险报告、记忆压缩、普通摘要可以在超时或失败后 downshift；复杂规划和错误恢复可以 retry 或 upgrade；生产库写操作、SQL 安全复核、权限变更复核不能静默 downshift，只能 upgrade、ask_user 或 fail_closed。最终效果是系统可用性提升，但高风险数据库任务不会因为模型不可用而偷偷降低安全标准。

### 7.10 模型路由进入质量评估

Codex 的设计方案把模型配置和使用事实纳入 telemetry 和测试，便于发现模型相关回归。

Claude 的设计方案中 eval harness 可以通过环境覆盖固定 feature/model 行为，让测试具备确定性。

mini-agent 的最终方案是扩展 `EvaluationCase`，支持断言模型路由。例如高风险 DDL 必须选择 review 模型，记忆压缩不得使用 premium 模型，不支持工具的模型不得绑定 PostgreSQL 工具，子智能体 reviewer 不能低于主模型质量等级。最终效果是模型策略可以被 CI 测试守住，而不是靠人工检查配置。

### 7.11 建立 PostgreSQL 模型质量评分闭环

Codex 的设计方案强调工程可观测、运行事实和回归控制。模型表现不是凭感觉，而是要能从事件和测试中复盘。

Claude 的设计方案通过模型能力、模型 usage、eval 覆盖和 feature override 支撑模型策略演进。

mini-agent 的最终方案是新增 `ModelEvaluationResult`，并在 `QualityManager` 中增加 `model_routing_gate` 和 `model_output_quality_gate`。模型评估指标包括：结构化输出成功率、证据引用完整性、SQL 安全误判率、工具调用合规率、报告可读性、成本和延迟。最终效果是模型选型可以持续优化，后续换模型不会只看主观体验。

### 7.12 多 provider 通过 ProviderAdapter 统一

Codex 的设计方案中 provider 抽象包含 auth、base_url、account state、provider capabilities、model manager 等，业务层不直接关心每个 provider 的细节。

Claude 的设计方案处理 firstParty、Bedrock、Vertex、Foundry 等 provider 差异，并把 provider-specific model strings 和 canonical model id 分开。

mini-agent 的最终方案是保留现有 `llm_factory` 作为底层构造器，但在上层新增 `ProviderAdapter`。DeepSeek、OpenAI、本地模型、企业网关都实现同一接口：`create_chat_model(route)`、`validate_model(profile)`、`estimate_cost(record)`、`normalize_error(error)`。最终效果是业务节点只依赖模型路由结果，不关心 provider SDK 差异。

### 7.13 结构化输出启用强约束解析和修复

Codex 的设计方案中工具、hook 和事件都强调结构化输入输出，便于运行时可靠处理。

Claude 的设计方案中工具 schema、Agent schema、SDK schema 都大量使用结构化校验，避免自由文本破坏系统状态。

mini-agent 的最终方案是让 `ModelInvocationPolicy.structured_output_required` 控制结构化解析。任务理解、规划、SQL 安全复核、委派结果、质量评估都必须绑定 schema；解析失败时先进行同模型低温修复，再根据策略升级模型或进入错误恢复。最终效果是数据库关键链路不会因为模型少输出一个字段就悄悄进入错误状态。

### 7.14 上下文窗口作为路由条件

Codex 的设计方案中模型能力会影响工具和上下文使用，不同模型不是等价容器。

Claude 的设计方案支持 1M context 相关模型解析和升级路径，也会根据模型能力处理长上下文。

mini-agent 的最终方案是让 `ModelRouter` 读取 `context_token_budget`、当前 working set、schema 摘要大小、历史观察数量和用户任务类型。如果预计上下文超过模型窗口的安全阈值，就选择长上下文模型、触发压缩或请求报告分段。最终效果是复杂数据库巡检、跨 schema 分析和长报告生成更可靠。

### 7.15 模型可见性纳入安全红线

Codex 的设计方案中 provider 能力和权限边界共同决定运行能力，不是所有 provider 都可用于所有任务。

Claude 的设计方案通过 allowlist、permission mode、AgentDefinition 和模型验证控制模型可用范围。

mini-agent 的最终方案是定义模型安全红线：未经评估模型不能做生产写操作审批；外部低可信 provider 不能处理 secret 级别数据；不支持工具的模型不能进工具调用节点；deprecated 模型不能用于高风险任务；cheap 模型不能单独裁决 critical SQL。最终效果是模型本身也成为安全策略的一部分。

### 7.16 CLI 和质量报告展示模型路由摘要

Codex 的设计方案会把模型、provider、reasoning effort 等作为运行事实记录，便于排查。

Claude 的设计方案会展示或输出 modelUsage，让用户和系统知道本轮使用了什么模型。

mini-agent 的最终方案是在 CLI 展示最近 `ModelRoute` 和 `ModelInvocationRecord`：任务、模型、provider、是否 fallback、是否工具绑定、耗时和状态。`QualityReport` 中加入模型调用数、失败数、降级次数、高风险任务是否使用 review 模型。最终效果是用户和开发者可以看清关键结论由哪个模型产生，也能复盘模型相关问题。

## 8. 推荐模型任务默认策略

| ModelTask | 默认质量等级 | 工具 | 温度 | 降级策略 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `intent_understanding` | balanced | no | 0.0 | retry/downshift | 稳定识别目标、约束和澄清需求 |
| `planning` | balanced/strong | no | 0.0 | retry/upgrade | 输出结构化计划，复杂任务可升级 |
| `tool_reasoning` | balanced | yes | 0.0 | fail or retry | 必须支持可靠 tool calling |
| `sql_safety_review` | review | no/tools limited | 0.0 | upgrade/fail_closed | 高风险 SQL 不能静默降级 |
| `delegation_worker` | cheap/balanced | role-scoped | 0.0 | retry/downshift | 子智能体只读分析 |
| `delegation_reviewer` | strong/review | limited | 0.0 | upgrade/fail_closed | 高风险复核 |
| `error_recovery` | balanced/strong | no/tools limited | 0.0 | retry/upgrade | 需要稳健分类和修复建议 |
| `memory_compaction` | cheap | no | 0.0 | downshift | 成本优先，不能写安全结论 |
| `report_generation` | cheap/balanced | no | 0.2 | downshift | 必须引用证据 |
| `quality_evaluation` | balanced/review | no | 0.0 | retry/upgrade | 评估结构化输出和安全要求 |

## 9. 推荐路由流程

```text
1. 节点声明 ModelTask
   -> intent_understanding / planning / tool_reasoning / sql_safety_review / ...

2. ModelRouter 读取当前状态
   -> task risk
   -> database environment
   -> context token estimate
   -> tool need
   -> structured output need
   -> subagent role
   -> user/model config

3. ModelRegistry 查找候选模型
   -> provider available
   -> allowlist
   -> capabilities
   -> data sensitivity
   -> forbidden tasks

4. 生成 ModelRoute
   -> selected_model_id
   -> ModelInvocationPolicy
   -> fallback_chain
   -> reason

5. ProviderAdapter 创建模型
   -> create_llm_no_tools / create_llm_with_tools
   -> bind tools only when model supports tools

6. 调用后记录 ModelInvocationRecord
   -> success / failure / fallback
   -> tokens / latency / cost

7. QualityManager 检查模型质量门
   -> routing gate
   -> output quality gate
   -> high-risk no silent downshift
```

## 10. 与现有模块的关系

### 10.1 与 Agent Loop

Agent Loop 不再直接调用 `create_llm_with_tools(visible_tools)`，而是声明 `ModelTask="tool_reasoning"`，由模型路由器选择支持工具调用的模型，并返回可绑定工具的 LLM。

### 10.2 与任务理解和规划

任务理解和规划使用结构化输出策略。模型路由器根据任务复杂度和风险选择 balanced 或 strong 模型，避免高风险数据库任务被弱模型规划。

### 10.3 与安全护栏

安全护栏不仅检查工具和 SQL，也检查模型是否允许处理当前数据敏感级别、是否允许用于高风险任务、是否发生不允许的 downshift。

### 10.4 与多智能体委派

子智能体模型默认从主控 Agent 继承，但角色可覆盖。`safety_reviewer` 必须走 review 模型或同等级模型，`report_writer` 可以走 cheap/balanced 模型。

### 10.5 与上下文管理

上下文管理提供 token 估算和压缩状态。模型路由器根据上下文大小选择长上下文模型或要求压缩。

### 10.6 与质量控制

质量模块消费 `ModelInvocationRecord` 和 `ModelEvaluationResult`，生成模型路由质量门、模型输出质量门和模型使用摘要。

### 10.7 与人机协作

CLI 和最终报告展示关键模型路由摘要。高风险任务如果模型不可用或需要降级，必须向用户说明并请求选择。

## 11. 状态字段建议

在 `AgentState` 中新增：

```python
model_profiles: list[ModelProfile]
model_routes: list[ModelRoute]
model_invocation_policies: list[ModelInvocationPolicy]
model_invocation_records: list[ModelInvocationRecord]
model_fallback_decisions: list[ModelFallbackDecision]
model_evaluation_results: list[ModelEvaluationResult]
```

这些字段应进入状态迁移、上下文摘要、CLI 展示、质量报告和 replay case。

## 12. 配置建议

新增环境变量：

```text
MODEL_ROUTING_ENABLED=true
MODEL_ALLOWLIST=deepseek-chat,deepseek-reasoner,gpt-4o,gpt-4o-mini
DEFAULT_MODEL_ALIAS=balanced
PLANNING_MODEL_ALIAS=balanced
TOOL_REASONING_MODEL_ALIAS=balanced
SAFETY_REVIEW_MODEL_ALIAS=review
REPORT_MODEL_ALIAS=cheap
MEMORY_MODEL_ALIAS=cheap
MODEL_FORBID_SILENT_DOWNSHIFT_FOR_HIGH_RISK=true
MODEL_MAX_CONTEXT_SAFETY_RATIO=0.8
```

第一阶段可以把 alias 映射到当前 provider 下的实际模型，例如：

```text
cheap -> deepseek-chat
balanced -> deepseek-chat
review -> deepseek-reasoner
```

后续可扩展到 OpenAI、本地模型或企业模型网关。

## 13. 测试与评估建议

### 13.1 单元测试

- `ModelRegistry` 能解析 alias 和 provider model id。
- 不支持 tools 的模型不能路由到 `tool_reasoning`。
- 高风险 SQL 复核必须选择 review 模型。
- cheap 模型不能用于 critical 数据库写操作审批。
- 子智能体默认继承主模型，`safety_reviewer` 可以覆盖为 review 模型。
- 超出上下文窗口时触发长上下文模型或压缩建议。

### 13.2 集成测试

- `intent_analyzer` 通过 `ModelRouter` 创建模型。
- `task_planner` 使用 planning 路由策略。
- `llm_reason` 只绑定路由允许的工具。
- `memory_compactor` 使用 memory 模型策略。
- `delegation_planner` 为子智能体生成模型 route。
- `QualityManager` 能生成模型路由质量门。

### 13.3 安全回归测试

- 高风险任务不允许 silent downshift。
- deprecated 模型不能用于 safety review。
- 外部 provider 不能处理 secret 数据。
- 不在 allowlist 的模型不能被用户配置启用。
- 模型 fallback 记录必须存在。

### 13.4 任务级评估

建议增加以下评估用例：

1. “删除三个月前订单数据”：必须路由到 review 模型做 SQL 安全复核。
2. “生成巡检报告”：报告模型可用 cheap/balanced，但必须引用证据。
3. “分析慢 SQL 并建议索引”：performance 子智能体可用 balanced，safety reviewer 用 review。
4. “只压缩上下文”：必须使用 cheap 模型，不得使用 premium/review。
5. “工具调用模型不支持 tool calling”：路由必须失败或换模型。

## 14. 分阶段落地

### 阶段一：模型目录和路由器

- 新增 `ModelProfile`、`ModelTask`、`ModelInvocationPolicy`、`ModelRoute`。
- 新增 `ModelRegistry` 和 `ModelRouter`。
- 把当前 DeepSeek/OpenAI 配置映射为默认模型 profile。
- 为规划、工具推理、安全复核、记忆压缩、报告生成建立默认策略。

### 阶段二：接入 LLM Factory

- 保留 `create_llm()` 底层函数。
- 新增 `create_llm_for_task(task, state, tools=None)`。
- `intent_analyzer`、`task_planner`、`llm_reason`、`memory_compactor` 改为走模型路由。
- 调用后写入 `ModelInvocationRecord`。

### 阶段三：安全和质量门

- 新增 `model_routing_gate`。
- 新增 `model_output_quality_gate`。
- 高风险任务禁止 silent downshift。
- 模型 allowlist、deprecated、数据敏感级别进入安全校验。

### 阶段四：子智能体和评估

- 子智能体角色接入模型继承/覆盖。
- 委派结果记录模型调用。
- 增加模型路由评估用例。
- 在质量报告中展示模型调用和降级摘要。

### 阶段五：多 provider 扩展

- 增加 ProviderAdapter。
- 支持本地模型或企业网关。
- 增加成本估算和 provider 错误归一化。

## 15. 最终方案总结

Codex 给 mini-agent 的核心启发是：模型 provider 必须有能力边界，模型使用必须成为可记录的运行事实，降级和能力差异不能隐藏在调用函数内部。Claude 给 mini-agent 的核心启发是：模型别名、主循环模型、子 Agent 继承模型、模型能力、effort 和 eval 覆盖都应该产品化，而不是散落在业务逻辑里。

mini-agent 的最终方案是：保留当前 `llm_factory` 作为 provider 构造层，在其上新增 `ModelRegistry + ModelRouter + ModelInvocationPolicy + ModelInvocationRecord + ModelQualityGate`。业务节点只声明模型任务，不直接选择具体模型；模型路由器根据 PostgreSQL 风险、工具需求、上下文大小、子智能体角色和质量策略做选择；质量模块再评估模型路由是否正确。

最终目标是让 mini-agent 不只是“会调模型”，而是“知道什么时候该用什么模型、为什么用它、用了之后表现如何、失败时能否安全降级”的 PostgreSQL 数据库管理智能体。
