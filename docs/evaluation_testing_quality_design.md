# PostgreSQL 管理智能体：评估、测试与质量控制模块设计

## 1. 背景

mini-agent 已经逐步具备 PostgreSQL 管理智能体的核心能力：任务理解、规划与任务分解、Agent Loop、上下文管理、记忆系统、工具注册与调用、PostgreSQL 工具、执行环境与工作区管理、状态管理、安全护栏与权限控制、人机交互与协作、错误处理与自我修复。

这些模块让 Agent “能做事、能防错、能恢复”，但要真正用于数据库管理，还必须回答一个更关键的问题：怎么证明它做得对、做得安全、做得稳定？

PostgreSQL 管理智能体的质量问题和普通应用不同：

- 有些错误不是代码异常，而是错误理解用户任务。
- 有些错误不是工具失败，而是工具选择不合规。
- 有些错误不是 SQL 执行失败，而是缺少证据、审批、回滚或验证。
- 有些错误只会在真实 PostgreSQL 中出现，例如锁等待、权限不足、SQLSTATE、事务回滚。
- 有些输出看起来合理，但没有证据链，无法用于 DBA 决策。
- 有些改动单元测试能过，但破坏了安全护栏或审批流程。

因此，评估、测试与质量控制模块不是简单的 pytest 集合，而是贯穿开发、CI、运行时、任务回放、质量报告和人工复核的质量控制体系。

## 2. 模块定位

评估、测试与质量控制模块位于研发流程和 Agent 运行流程之间：

```text
User Task / Agent State / Tool Result / Database Result / Code Change
  -> Quality Criteria
  -> Test Suite
  -> Evaluation Dataset
  -> Safety Regression
  -> Replay
  -> QualityGate
  -> EvaluationResult
  -> QualityReport
```

输入：

- 用户任务样例
- `DBTaskIntent`
- `DBTaskPlan`
- `TaskStep`
- 工具调用和工具结果
- `DBObservation`
- `ApprovalDecision`
- `SQLSafetyReport`
- `ErrorRecord`
- `RecoveryDecision`
- 最终报告
- 代码变更
- CI 运行结果

输出：

- `QualityGate`
- `EvaluationCase`
- `EvaluationResult`
- `ReplayCase`
- `QualityReport`
- 测试通过 / 失败
- 阻断 / 允许继续
- 人工复核建议

本模块不替代具体业务模块，也不替代安全护栏。它负责定义质量标准、组织测试与评估、发现回归、生成质量报告，并在高风险场景阻断继续执行或要求人工复核。

## 3. 设计目标

1. 为每类数据库任务定义可验证的完成标准。
2. 建立单元、集成、端到端、安全回归和任务评估的分层测试体系。
3. 使用可重置 PostgreSQL 测试环境验证真实数据库行为。
4. 建设标准任务集，评估 Agent 的任务级能力。
5. 对工具输入输出做契约测试。
6. 把安全质量门禁做成硬阻断，而不是事后提醒。
7. 对 Agent 输出进行结构化评分。
8. 支持历史任务回放，复现线上失败和用户反馈。
9. 专门测试失败处理和自我修复路径。
10. 将质量控制接入 CI。
11. 对高风险变更加人工评审质量门。
12. 生成可交付、可审计的质量报告。

## 4. 非目标

1. 不追求一次性构建完整大规模 benchmark 平台。
2. 不用 mock 替代所有 PostgreSQL 行为。
3. 不让 LLM 自己判断所有测试是否通过。
4. 不把评估分数作为绕过安全护栏的依据。
5. 不把质量报告替代真实测试。
6. 不要求每个普通文案改动都跑完整端到端数据库测试。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前系统已经具备以下质量基础：

- 已有较完整的 pytest 测试集。
- PostgreSQL 工具有结构化结果。
- `StateValidator` 可以检查状态一致性。
- `SecurityPolicyEngine` 可以检查工具、SQL、输出和 replay 安全。
- `ErrorRecord` 和 `RecoveryDecision` 可以表达失败与恢复。
- `ToolInvocationRecord`、`DBObservation`、`VerificationResult` 提供审计链。
- 文档已经覆盖多个模块设计。
- CI 可以运行单元测试和集成测试。

### 5.2 主要不足

1. 还没有统一的任务质量门对象。
2. 测试更多覆盖模块行为，还缺少标准 Agent 任务集。
3. 真实 PostgreSQL 场景测试还可以更系统，例如权限、锁、约束、慢查询、事务回滚。
4. 工具契约测试还没有统一声明输入、输出、错误和敏感数据规则。
5. 缺少结构化 `EvaluationResult` 来表达任务级评分。
6. 还没有任务 replay case，把历史失败沉淀成回归用例。
7. 高风险代码变更还没有单独质量门。
8. 测试结果还没有汇总成面向人类的 `QualityReport`。

## 6. 推荐核心对象

### 6.1 QualityGate

```python
class QualityGate(TypedDict):
    id: str
    gate_type: Literal[
        "task_completion",
        "tool_contract",
        "safety_regression",
        "state_integrity",
        "error_recovery",
        "report_quality",
        "ci",
        "human_review",
    ]
    target_ref: str
    required_checks: list[str]
    passed_checks: list[str]
    failed_checks: list[str]
    status: Literal["pending", "passed", "failed", "waived"]
    blocking: bool
    created_at: str
```

### 6.2 EvaluationCase

```python
class EvaluationCase(TypedDict):
    id: str
    category: Literal[
        "intent",
        "planning",
        "tool_use",
        "safety",
        "postgresql_task",
        "error_recovery",
        "reporting",
    ]
    user_input: str
    initial_state: dict[str, Any]
    expected_state_assertions: list[dict[str, Any]]
    expected_output_assertions: list[dict[str, Any]]
    forbidden_actions: list[str]
    allowed_tools: list[str]
    required_evidence: list[str]
    tags: list[str]
```

### 6.3 EvaluationResult

```python
class EvaluationResult(TypedDict):
    id: str
    case_id: str
    status: Literal["passed", "failed", "needs_review"]
    scores: dict[str, float]
    failed_assertions: list[str]
    evidence_refs: list[str]
    safety_blocked: bool
    requires_human_review: bool
    summary: str
    created_at: str
```

### 6.4 ReplayCase

```python
class ReplayCase(TypedDict):
    id: str
    source: Literal["manual", "failed_task", "user_feedback", "production_incident"]
    input_messages: list[dict[str, Any]]
    state_snapshot_ref: Optional[str]
    tool_invocation_refs: list[str]
    expected_recovery: Optional[str]
    expected_final_status: str
    sensitivity: Literal["public", "internal", "sensitive"]
    created_at: str
```

### 6.5 QualityReport

```python
class QualityReport(TypedDict):
    id: str
    target_ref: str
    scope: Literal["task", "module", "release", "ci_run", "replay_suite"]
    status: Literal["passed", "failed", "needs_review"]
    test_summary: dict[str, Any]
    evaluation_summary: dict[str, Any]
    safety_summary: dict[str, Any]
    uncovered_risks: list[str]
    human_review_required: bool
    recommendations: list[str]
    created_at: str
```

## 7. 推荐测试分层

| 层级 | 测试对象 | 主要验证内容 |
| --- | --- | --- |
| 单元测试 | intent、planner、policy、classifier、validator | 单个函数和状态对象是否正确 |
| 集成测试 | Agent Loop、工具注册、状态恢复、审批流 | 模块间状态流是否正确 |
| PostgreSQL 工具测试 | 真实测试库和工具 | SQLSTATE、事务、锁、权限、结果结构 |
| 端到端测试 | 用户任务到最终报告 | Agent 是否完成完整任务闭环 |
| 安全回归测试 | 危险 SQL、生产写、审批复用 | 安全策略是否硬阻断 |
| 任务评估 | 标准 EvaluationCase | 任务理解、计划、工具、报告质量 |
| Replay 测试 | 历史失败和用户反馈 | 线上问题是否复现并修复 |
| CI 质量门 | 提交和发布 | 是否允许合并或发布 |

## 8. 详细设计点

### 8.1 设计点一：为每类数据库任务定义完成标准

**Codex 的设计方案**

Codex 在工程任务中通常会用“代码是否修改完成、测试是否运行、输出是否验证”来判断任务闭环。它的好处是结果不是只靠模型口头说明，而是绑定了实际命令、测试和文件变化；局限是 Codex 的完成标准主要面向代码和文件，对数据库任务中的 SQL hash、审批、回滚、证据链没有天然字段。

**Claude 的设计方案**

Claude 擅长把复杂任务的完成标准转成自然语言，例如说明报告应该包含哪些部分、诊断应该解释哪些原因、用户还需要确认什么。它的好处是用户容易理解；局限是如果只停留在自然语言标准，系统无法自动阻断不合格输出。

**最终方案**

mini-agent 新增 `QualityGate`，根据任务类型生成完成标准。只读诊断必须有观察证据、结论、建议；写库任务必须有 SQL hash、审批记录、回滚说明、执行结果和验证结果；报告任务必须有证据引用、风险说明和下一步建议。

**权衡原因**

Codex 的验证闭环适合机器检查，Claude 的自然语言表达适合用户理解。mini-agent 需要把完成标准做成结构化质量门，同时能向用户解释。

**效果**

Agent 不能只说“完成了”，必须满足任务对应的证据、审批、安全和报告标准。

### 8.2 设计点二：建立分层测试体系

**Codex 的设计方案**

Codex 的工程工作流非常重视运行测试、看测试结果、根据失败修复。它的好处是每次修改都有自动验证；局限是通用测试通常不足以覆盖 Agent 的多轮状态流、工具策略和数据库风险。

**Claude 的设计方案**

Claude 擅长把复杂流程拆成清晰检查点，例如把一个数据库任务拆成理解、规划、执行、验证、报告几个质量维度。它的好处是测试设计更完整；局限是需要落成真实测试代码，否则只是测试建议。

**最终方案**

mini-agent 采用分层测试：单元测试验证单模块，集成测试验证状态流，PostgreSQL 工具测试验证真实数据库行为，端到端测试验证任务闭环，安全回归测试验证危险路径，任务评估验证 Agent 能力。

**权衡原因**

Codex 提供工程化自动测试方法，Claude 提供测试维度拆解。最终方案让测试既能跑，又覆盖 Agent 的关键能力。

**效果**

每次重构不仅知道代码是否能运行，也知道数据库任务理解、规划、安全、执行和报告是否仍然可靠。

### 8.3 设计点三：使用可重置 PostgreSQL 测试环境

**Codex 的设计方案**

Codex 倾向于在真实环境中运行命令、测试和验证，而不是只靠静态推理。它的好处是真实问题暴露得早；局限是需要处理环境安装、数据初始化和测试隔离。

**Claude 的设计方案**

Claude 可以帮助设计测试场景，例如慢查询、缺索引、权限不足、锁等待、约束冲突。它的好处是场景覆盖广；局限是场景必须落地到真实数据库才能验证。

**最终方案**

mini-agent 准备 disposable PostgreSQL 测试库，包含标准 schema、测试数据、权限角色、锁等待脚本、约束冲突样例、慢查询样例和可回滚写操作。每次测试前初始化，测试后清理或重建。

**权衡原因**

Codex 的真实执行验证适合发现实际问题，Claude 的场景设计适合覆盖 DBA 常见任务。最终方案用真实 PostgreSQL 验证工具和 Agent 行为。

**效果**

系统能测出 mock 无法发现的问题，例如 SQLSTATE、锁等待、事务回滚、权限不足和 EXPLAIN 输出差异。

### 8.4 设计点四：建设标准任务评估集

**Codex 的设计方案**

Codex 的工程质量依赖测试用例和回归验证，能判断新改动是否破坏既有行为。它的好处是稳定；局限是普通单元测试无法覆盖“用户一句话到数据库报告”的任务级表现。

**Claude 的设计方案**

Claude 的能力评估常用自然语言任务和 rubric，例如看回答是否完整、是否遵守约束、是否解释清楚。它的好处是适合评估开放式输出；局限是需要结构化断言，否则评估容易主观。

**最终方案**

mini-agent 建设 `EvaluationCase` 集合，覆盖慢 SQL 分析、索引建议、迁移测试报告、只生成 SQL 不执行、生产写操作拒绝、审批 SQL hash 不一致阻断、锁等待诊断等任务。每个 case 包含用户输入、初始状态、期望状态断言、期望输出断言、禁止动作、允许工具和必要证据。

**权衡原因**

Codex 的回归测试保证稳定，Claude 的 rubric 适合开放任务。最终方案把自然语言任务变成可运行的评估用例。

**效果**

每次调整提示词、规划器、工具策略或模型时，可以比较 Agent 在标准任务集上的质量变化。

### 8.5 设计点五：所有工具调用都做契约测试

**Codex 的设计方案**

Codex 的工具调用强调真实参数、真实输出和可追踪执行结果。它的好处是工具边界清楚；局限是工具契约需要在项目中显式声明和测试。

**Claude 的设计方案**

Claude 的 tool_use / tool_result 形式强调工具输入输出是对话协议的一部分。它的好处是容易把工具调用解释给用户；局限是工具返回结构如果不稳定，会影响后续推理。

**最终方案**

mini-agent 为每个工具建立契约测试，检查 args schema、result schema、错误 schema、敏感字段脱敏、result_type、sqlstate、duration_ms、artifact 记录、replay policy 和 observation 转换。

**权衡原因**

Codex 的真实工具执行适合验证契约，Claude 的工具协议思想提醒我们工具输出要稳定可读。最终方案把工具当成稳定 API 管理。

**效果**

Agent Loop、上下文管理、错误处理和报告模块不会因为某个工具输出格式变化而失效。

### 8.6 设计点六：安全质量门禁必须硬阻断

**Codex 的设计方案**

Codex 对权限、沙箱和危险命令有硬边界，不能靠模型解释绕过。它的好处是安全性强；局限是数据库安全还需要 SQL 分类、环境、审批和 SQL hash 绑定。

**Claude 的设计方案**

Claude 的权限提示和风险解释适合让用户理解为什么被阻断。它的好处是交互清晰；局限是解释不能代替强制拦截。

**最终方案**

mini-agent 把安全质量门禁做成阻断检查：未知环境和生产环境禁止写，写 SQL 必须审批，审批必须绑定 step、environment、SQL hash，DROP/TRUNCATE 必须 critical，只读任务不得出现写工具，审批不匹配必须失败。

**权衡原因**

Codex 的硬边界负责安全，Claude 的解释负责用户理解。最终方案不允许质量门只做警告。

**效果**

危险动作即使由模型生成，也会在测试、CI 和运行时被拦住。

### 8.7 设计点七：对 Agent 输出做结构化质量评分

**Codex 的设计方案**

Codex 通常通过测试结果、命令输出和 diff 合理性判断工程质量。它的好处是客观；局限是自然语言报告和数据库诊断质量不能完全靠单元测试判断。

**Claude 的设计方案**

Claude 适合使用 rubric 评价自然语言输出，例如完整性、证据性、清晰度、风险说明。它的好处是能评价开放式结果；局限是需要控制评分标准，避免主观漂移。

**最终方案**

mini-agent 新增 `EvaluationResult`，从任务理解、计划完整性、工具合规性、证据充分性、安全通过、错误处理、报告可读性、人工复核需求等维度评分。

**权衡原因**

Codex 的客观测试适合判断执行正确性，Claude 的 rubric 适合判断解释质量。最终方案把二者合并成结构化评分。

**效果**

系统可以量化不同版本、不同模型、不同提示词在数据库任务上的表现。

### 8.8 设计点八：引入任务回放机制

**Codex 的设计方案**

Codex 的 checkpoint、工具调用记录和执行状态适合复现问题现场。它的好处是能定位回归；局限是需要把隐私和敏感数据处理好。

**Claude 的设计方案**

Claude 的对话上下文和用户反馈适合沉淀成复盘样例。它的好处是能保留用户真实表达；局限是自然语言 transcript 需要结构化后才能稳定评估。

**最终方案**

mini-agent 新增 `ReplayCase`，把历史失败任务、用户反馈、状态快照、工具调用引用和期望恢复结果保存成可回放用例。敏感数据必须脱敏或标记 sensitivity。

**权衡原因**

Codex 的可复现执行现场用于技术回归，Claude 的对话样例用于真实用户任务覆盖。最终方案把线上问题转成长期测试资产。

**效果**

线上出现一次失败后，可以沉淀成永久回归用例，避免同类问题再次发生。

### 8.9 设计点九：测试失败路径和自我修复

**Codex 的设计方案**

Codex 的工程验证会关注命令失败、测试失败和修复后的再次验证。它的好处是失败处理可验证；局限是数据库失败类型更复杂，需要专门分类。

**Claude 的设计方案**

Claude 能把失败解释成用户可理解的原因和下一步。它的好处是失败体验好；局限是解释不能证明恢复路径正确。

**最终方案**

mini-agent 专门测试连接失败、认证失败、权限不足、SQL 语法错、对象不存在、锁等待、语句超时、审批拒绝、审批不匹配、状态损坏、工具 schema 错误和 LLM 输出不合法，并检查 `ErrorRecord`、`RecoveryDecision`、`ErrorReport` 是否正确。

**权衡原因**

Codex 的失败验证保证恢复路径可执行，Claude 的解释能力保证用户能理解。最终方案把失败作为一等质量场景。

**效果**

Agent 不只在 demo 成功路径表现好，遇到真实数据库异常也能稳定处理。

### 8.10 设计点十：质量控制接入 CI

**Codex 的设计方案**

Codex 的工程习惯是修改后运行验证，并在最终说明里报告测试命令和结果。它的好处是质量反馈及时；局限是人工运行容易遗漏。

**Claude 的设计方案**

Claude 能把 CI 结果、失败项和建议整理成清晰报告。它的好处是帮助人理解失败；局限是必须由 CI 执行真实测试。

**最终方案**

mini-agent 在 CI 中运行单元测试、集成测试、安全回归、工具契约测试、schema 检查、文档检查和核心 eval case。高成本 PostgreSQL E2E 测试可按标签分为 quick、db、e2e、release。

**权衡原因**

Codex 的自动化测试负责执行，Claude 的报告能力负责解释。最终方案把质量控制从人工习惯变成强制流程。

**效果**

每次提交都能知道是否破坏核心能力，发布前能知道高风险场景是否仍然安全。

### 8.11 设计点十一：高风险变更加人工评审质量门

**Codex 的设计方案**

Codex 对高风险命令、权限变更和用户工作区保护更谨慎。它的好处是能防止破坏性操作；局限是代码层面的高风险变更需要项目自己识别。

**Claude 的设计方案**

Claude 擅长解释风险和列出人工 review 要点。它的好处是协作效率高；局限是不能替代真实 reviewer 的判断。

**最终方案**

mini-agent 对安全策略、审批逻辑、写库工具、状态恢复、错误处理、记忆写入等模块的改动标记为 high-risk change，要求更完整测试和人工 review。质量报告必须明确高风险文件、相关测试和未覆盖风险。

**权衡原因**

Codex 的谨慎执行保护系统，Claude 的风险解释帮助 reviewer 聚焦。最终方案让质量门和代码风险等级绑定。

**效果**

关键安全模块不会被低成本随意修改，减少“测试通过但安全语义被破坏”的风险。

### 8.12 设计点十二：生成 QualityReport

**Codex 的设计方案**

Codex 的最终总结通常会说明做了什么、运行了什么测试、结果如何。它的好处是工程闭环清楚；局限是数据库 Agent 质量报告需要包含安全、环境、审批、证据和未覆盖风险。

**Claude 的设计方案**

Claude 擅长把复杂验证结果整理成可读报告，说明风险、建议和下一步。它的好处是易读；局限是报告必须绑定真实测试结果。

**最终方案**

mini-agent 生成 `QualityReport`，包含测试范围、通过率、失败项、EvaluationResult 汇总、安全检查、数据库环境、是否涉及真实写操作、未覆盖风险、是否需要人工复核和建议。

**权衡原因**

Codex 的测试结果提供事实，Claude 的表达能力提供可读解释。最终方案让质量状态可交付、可审计。

**效果**

用户和开发者能知道“测了什么、没测什么、风险在哪里、是否可以继续”。

## 9. 推荐质量控制流程

```text
1. 代码或提示词变更
   -> 识别影响模块和风险等级
   -> 选择测试标签 quick/db/e2e/security/release

2. 运行测试和评估
   -> 单元测试
   -> 集成测试
   -> PostgreSQL 工具测试
   -> 安全回归
   -> 任务 eval
   -> replay

3. 生成质量对象
   -> QualityGate
   -> EvaluationResult
   -> QualityReport

4. 判断是否允许继续
   -> passed: 允许提交或继续执行
   -> failed: 阻断
   -> needs_review: 请求人工复核
```

## 10. 与现有模块的关系

### 10.1 与任务理解模块

评估任务理解是否正确识别 domain、primary_intent、candidate_intents、risk_level、missing_slots 和 next_action。

### 10.2 与规划模块

评估计划是否包含 observe、diagnose、approve、execute、verify、report 等必要阶段，写操作是否包含审批和回滚。

### 10.3 与 Agent Loop

评估是否按当前步骤推进，工具调用是否被 policy gate 控制，执行后是否归一化观察并验证。

### 10.4 与工具模块

通过工具契约测试保证工具输入输出稳定，错误结构可消费，敏感信息被脱敏。

### 10.5 与安全护栏

安全回归测试验证危险 SQL、生产写、审批复用、SQL hash mismatch 和 replay 风险。

### 10.6 与状态管理

质量控制消费 `StateIntegrityReport`，把状态一致性作为质量门。

### 10.7 与错误处理

失败路径测试验证 `ErrorRecord`、`RecoveryDecision`、`RecoveryAttempt`、`ErrorReport` 是否符合预期。

### 10.8 与人机协作

质量报告和人工复核请求通过协作模块展示给用户或 reviewer。

## 11. 推荐实现步骤

### 11.1 第一阶段：质量对象和测试标签

1. 在 `agent/state.py` 新增 `QualityGate`、`EvaluationCase`、`EvaluationResult`、`ReplayCase`、`QualityReport`。
2. 在状态迁移中补默认字段。
3. 为 pytest 增加标签：quick、db、e2e、security、replay、release。
4. 增加 `quality/` 包，提供质量对象构造器。

### 11.2 第二阶段：工具契约和安全回归

1. 为 PostgreSQL 工具建立契约测试。
2. 为危险 SQL、生产环境写、审批复用、SQL hash mismatch 建立安全回归测试。
3. 把 `StateValidator` 和 `SecurityPolicyEngine` 的关键规则纳入质量门。

### 11.3 第三阶段：EvaluationCase 任务集

1. 建立 `eval_cases/` 目录。
2. 增加慢 SQL、索引建议、只读报告、写 SQL 草案、审批阻断、错误恢复等标准任务。
3. 实现 eval runner，把 Agent 输出转成 `EvaluationResult`。

### 11.4 第四阶段：Replay 和质量报告

1. 从失败任务生成 `ReplayCase`。
2. 支持 replay 历史状态和工具调用。
3. 生成 `QualityReport`。
4. CLI 展示质量报告摘要。

### 11.5 第五阶段：CI 和人工复核门

1. 配置 CI 分层运行 quick/db/e2e/security/release。
2. 根据变更文件识别 high-risk change。
3. 高风险变更要求安全回归和人工 review。

## 12. 验收标准

1. 每类数据库任务都有对应 `QualityGate`。
2. PostgreSQL 工具有契约测试，覆盖成功、错误、脱敏、artifact、replay policy。
3. 安全回归能阻断生产写、未知环境写、危险 SQL、审批不匹配。
4. 至少有一组标准 `EvaluationCase` 覆盖任务理解、规划、工具、安全、报告和错误恢复。
5. Agent 输出能生成 `EvaluationResult`。
6. 历史失败能生成 `ReplayCase` 并重新运行。
7. CI 能运行 quick 测试和安全回归。
8. 高风险变更能触发人工复核质量门。
9. 质量结果能汇总成 `QualityReport`。
10. 全量测试通过，且质量报告说明测试范围和未覆盖风险。

## 13. 最终结论

Codex 给 mini-agent 的核心启发是：质量必须绑定真实执行、真实测试、真实工具结果和可复现状态，不能只靠模型自评。Claude 给 mini-agent 的核心启发是：质量标准、评估结果和风险说明要能被人理解，尤其要把复杂数据库任务拆成可读的检查维度。

最终 mini-agent 的评估、测试与质量控制模块采用折中方案：底层学习 Codex 的自动化测试、回归验证、执行证据和硬质量门；上层学习 Claude 的任务 rubric、自然语言报告和人工复核协作。结合 PostgreSQL 管理场景，形成“完成标准、分层测试、真实数据库验证、任务评估集、工具契约、安全回归、结构化评分、任务回放、失败路径测试、CI 质量门、人工复核、质量报告”的完整质量体系。
