# PostgreSQL 管理智能体：产物生成与交付模块设计

## 1. 背景

mini-agent 已经具备 PostgreSQL 管理智能体的大部分基础模块：任务理解、规划、Agent Loop、上下文管理、记忆系统、工具注册、PostgreSQL 工具、执行环境与工作区、状态管理、安全护栏、人机协作、错误处理、质量控制、多智能体和模型路由。

但当前系统的“产物生成与交付”仍然偏弱。系统已经有 `TaskWorkspace`、`ArtifactRecord`、`QualityReport`、`final_report` 类型和报告目录，但这些能力还没有形成一条完整交付链路：

```text
工具结果 / SQL 草稿 / EXPLAIN / 审批记录 / 执行记录 / 验证证据
  -> 分散写入状态或 artifact
  -> 缺少统一交付契约
  -> 缺少面向用户的交付包
  -> 缺少交付前质量门
  -> 缺少失败/中断也可交付的报告
```

对 PostgreSQL 管理智能体来说，这个模块非常关键。数据库任务的结束不能只是模型回答一句“已完成”，而应该产出可检查、可复用、可审计的材料。例如：

- 慢 SQL 诊断应该交付 EXPLAIN 摘要、瓶颈判断、证据引用和优化建议。
- DDL 变更应该交付 SQL 草稿、影响分析、锁风险、回滚方案、审批材料和验证 SQL。
- 数据修复应该交付 dry-run 结果、审批记录、执行记录、影响行数和验证结果。
- 安全拦截或失败任务也应该交付阻塞原因、已收集证据、未完成事项和下一步选择。

因此，本模块的目标是把 mini-agent 从“会生成回复”升级为“会生成数据库任务交付物”，让最终输出既适合用户阅读，也适合 DBA、审计和后续任务恢复。

## 2. 模块定位

产物生成与交付模块位于 Agent Loop 的末端，但依赖整个系统的结构化状态。

推荐链路：

```text
User Request
  -> Task Understanding / DeliveryContract
  -> DBTaskPlan / TaskStep
  -> Tool Execution / DBObservation / SQLSafetyReport
  -> ApprovalDecision / VerificationResult
  -> ArtifactRecord / TaskWorkspace
  -> ArtifactManifest
  -> DeliveryPackage
  -> DeliveryQualityGate
  -> FinalReport / User-facing Response
```

本模块的输入：

- `current_intent`
- `db_task_plan`
- `task_stack`
- `db_observations`
- `tool_execution_results`
- `tool_invocation_records`
- `sql_safety_reports`
- `approval_decisions`
- `verification_results`
- `artifact_records`
- `quality_gates`
- `quality_reports`
- `model_routes`
- `model_invocation_records`
- `error_reports`
- `task_workspace`

本模块的输出：

- `DeliveryContract`
- `ArtifactManifest`
- `DeliveryPackage`
- `DeliveryItem`
- `ReportSection`
- `SQLDeliveryItem`
- `EvidenceReference`
- `DeliveryQualityGate`
- `ArtifactRecord(kind="final_report")`
- 用户可读最终答复

本模块不替代工具执行，也不替代安全护栏。它负责把任务过程中已经产生的事实材料组织成可交付结果，并在交付前检查是否满足用户目标和数据库安全要求。

## 3. 设计目标

1. 每个任务都能声明最终应该交付什么，而不是默认只输出自然语言回答。
2. 让 SQL、报告、审批、执行记录、验证证据都成为结构化产物。
3. 让最终报告基于结构化状态和 artifact，而不是基于聊天历史自由生成。
4. 让每个结论都能引用证据，避免“无来源判断”。
5. 让高风险 PostgreSQL 交付物必须包含审批、回滚和验证信息。
6. 让用户可读版和审计版分离。
7. 让大结果和敏感信息通过 artifact 引用和脱敏摘要交付。
8. 让失败、中断、拒绝审批、安全拦截也能生成可用交付物。
9. 让交付前质量门阻止不完整或不安全的报告。
10. 让 CLI/API 能清楚展示交付物入口、状态、文件路径和下一步动作。

## 4. 非目标

1. 不把 mini-agent 变成完整工单系统。
2. 不实现复杂 PDF/Word 排版引擎，第一阶段以 Markdown、JSON、SQL 文件为主。
3. 不把所有原始数据库结果永久保存。
4. 不允许交付模块绕过 SQL 安全、审批和权限控制。
5. 不允许模型为了生成报告而编造不存在的证据。
6. 不在第一阶段实现远程对象存储或企业知识库归档。

## 5. 当前 mini-agent 的基础与不足

### 5.1 已有基础

当前 mini-agent 已经具备以下基础：

- `TaskWorkspace` 能为任务建立 `.mini_agent/tasks/{task_id}` 工作区。
- `ArtifactRecord` 能记录产物类型、路径、摘要、敏感级别和生命周期。
- `ArtifactStore` 能创建轻量 artifact 元数据。
- PostgreSQL 工具已经能把部分工具结果转换成 `DBObservation`。
- 工具执行节点已经能把部分结果写成 artifact 记录。
- `QualityManager.task_completion_gate()` 已经检查最终报告是否存在证据。
- `ModelTask.report_generation` 已在模型路由中预留。
- `CollaborationManager` 已经有最终报告展示事件。
- `QualityReport` 已经能作为质量交付的一部分。

### 5.2 主要不足

1. 没有 `DeliveryContract`，系统不知道这次任务应该交付哪些东西。
2. `ArtifactRecord` 是单个产物记录，缺少 `ArtifactManifest` 把产物串成任务级证据链。
3. 没有 `DeliveryPackage`，用户无法一次性看到本次任务的交付入口。
4. 最终报告节点还不够独立，报告生成逻辑没有完全从 Agent Loop 中抽象出来。
5. SQL 草稿、审批、执行记录、验证结果之间缺少显式绑定。
6. 没有报告模板体系，不同 PostgreSQL 任务的交付格式不稳定。
7. 没有交付前质量门，可能出现缺证据、缺回滚、缺验证的报告。
8. 用户可读报告和审计报告没有分层。
9. 失败、中断、安全阻塞时的交付物不够系统。
10. CLI/API 还不能以“交付包”的方式展示结果。

## 6. 推荐核心对象

### 6.1 DeliveryContract

```python
class DeliveryContract(TypedDict):
    id: str
    intent_id: str
    plan_id: Optional[str]
    audience: Literal["developer", "dba", "operator", "auditor", "general"]
    delivery_mode: Literal["chat_summary", "artifact_package", "approval_package", "audit_package"]
    required_items: list[str]
    optional_items: list[str]
    required_evidence_types: list[str]
    requires_sql_package: bool
    requires_approval_evidence: bool
    requires_rollback_plan: bool
    requires_verification: bool
    output_formats: list[Literal["markdown", "json", "sql", "text"]]
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    status: Literal["draft", "ready", "blocked", "delivered"]
    created_at: str
    updated_at: str
```

### 6.2 EvidenceReference

```python
class EvidenceReference(TypedDict):
    id: str
    source_type: Literal[
        "db_observation",
        "tool_result",
        "artifact",
        "approval",
        "verification",
        "quality_gate",
        "model_record",
        "error_report",
    ]
    source_id: str
    summary: str
    supports_claim: str
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
```

### 6.3 SQLDeliveryItem

```python
class SQLDeliveryItem(TypedDict):
    id: str
    purpose: Literal["diagnostic", "change", "rollback", "verification", "dry_run"]
    sql_preview: str
    sql_hash: str
    classification: str
    risk_level: str
    target_environment: str
    approval_id: Optional[str]
    safety_report_id: Optional[str]
    execution_record_id: Optional[str]
    verification_refs: list[str]
    status: Literal["draft", "approved", "executed", "verified", "blocked"]
```

### 6.4 ReportSection

```python
class ReportSection(TypedDict):
    id: str
    title: str
    purpose: Literal[
        "summary",
        "evidence",
        "diagnosis",
        "recommendation",
        "risk",
        "approval",
        "execution",
        "verification",
        "rollback",
        "next_steps",
    ]
    content: str
    evidence_refs: list[str]
    status: Literal["complete", "missing_evidence", "not_applicable"]
```

### 6.5 ArtifactManifest

```python
class ArtifactManifest(TypedDict):
    id: str
    task_id: str
    artifact_ids: list[str]
    evidence_refs: list[EvidenceReference]
    sql_items: list[SQLDeliveryItem]
    report_paths: list[str]
    missing_items: list[str]
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    created_at: str
```

### 6.6 DeliveryPackage

```python
class DeliveryPackage(TypedDict):
    id: str
    task_id: str
    contract_id: str
    title: str
    status: Literal["draft", "ready", "blocked", "delivered", "failed"]
    summary: str
    user_report_path: Optional[str]
    audit_report_path: Optional[str]
    manifest_id: str
    artifact_ids: list[str]
    quality_gate_ids: list[str]
    next_actions: list[str]
    created_at: str
    delivered_at: Optional[str]
```

### 6.7 DeliveryQualityGate

第一阶段可以复用 `QualityGate`，新增 `gate_type="delivery_quality"`。

```python
QualityGate(
    gate_type="delivery_quality",
    required_checks=[
        "contract_satisfied",
        "required_evidence_present",
        "sql_items_have_safety_metadata",
        "write_items_have_approval",
        "rollback_present_when_required",
        "verification_present_when_required",
        "sensitive_data_redacted",
        "report_paths_recorded",
    ],
)
```

## 7. 详细设计点

### 7.1 建立 DeliveryContract 交付契约

**Codex 的设计方案**

Codex 的任务完成判断强调“目标是否真正达成”。它不会只因为模型说完成了就结束，而是要求当前文件、命令输出、测试结果、工具结果等证据能证明用户要求已经满足。Codex 的优点是交付边界清晰，最终回答会围绕做了什么、验证了什么、还有什么风险来收口；局限是它主要面向代码任务，没有天然表达 PostgreSQL 交付物类型，例如审批包、回滚包、验证包。

**Claude 的设计方案**

Claude 的交互系统更重视会话总结、输出风格和任务通知。它可以根据用户场景调整回答形式，也能通过 summary、task、transcript 等机制把过程整理给用户。优点是表达灵活、对用户友好；局限是如果没有额外约束，报告结构可能依赖提示词风格，缺少数据库任务所需的硬性字段。

**最终方案**

mini-agent 新增 `DeliveryContract`，由任务理解阶段或规划阶段生成。它不枚举所有用户意图，而是描述“这次任务必须交付什么”。例如慢 SQL 任务要求诊断报告和证据引用；DDL 任务要求 SQL 包、审批包、回滚包和验证报告；只读巡检任务要求健康报告和风险建议。最终效果是交付目标提前结构化，后续 Agent Loop 和 final report 都能围绕契约补齐材料。

### 7.2 建立 PostgreSQL 交付物类型体系

**Codex 的设计方案**

Codex 常把文件 diff、patch、测试结果、命令输出作为工程任务的核心产物。它的好处是产物可验证，用户可以看到实际改动；局限是这些产物类型偏代码工程，不能直接覆盖 SQL 审批、执行记录、数据库验证等场景。

**Claude 的设计方案**

Claude 更偏向工具结果和自然语言解释的组织，能把复杂过程总结为用户可读内容。它的好处是用户容易理解；局限是产物类型如果只靠语言表达，后续审计和恢复会比较弱。

**最终方案**

mini-agent 定义 PostgreSQL 专用交付类型：`diagnostic_report`、`sql_change_package`、`approval_package`、`execution_record`、`rollback_package`、`verification_report`、`final_report`、`blocked_report`。这些类型映射到已有 `ArtifactRecord.kind`，必要时扩展 kind 枚举。最终效果是不同数据库任务有稳定交付物，不会把所有结果都混成一段最终回复。

### 7.3 用 ArtifactManifest 串起证据链

**Codex 的设计方案**

Codex 的 session trace、工具事件、patch 和终端输出形成可追溯链路。它的好处是能复盘“为什么得出这个结果”；局限是这些 trace 面向通用执行过程，不一定直接表达“某个报告结论由哪个 DBObservation 支撑”。

**Claude 的设计方案**

Claude 的 transcript 和 summary 可以把会话过程交给用户查看。它的好处是过程完整；局限是 transcript 信息量大，不适合作为最终交付索引。

**最终方案**

mini-agent 新增 `ArtifactManifest`，把 `artifact_records`、`db_observations`、`approval_decisions`、`verification_results`、`quality_gates` 统一组织成任务级清单。最终报告里的每个关键结论必须引用 `EvidenceReference`。最终效果是报告不只是好读，还能追溯来源。

### 7.4 最终报告必须从结构化状态生成

**Codex 的设计方案**

Codex 的最终回答通常基于实际工作区状态、命令结果和测试结果，而不是只基于模型记忆。它的好处是抗幻觉，能避免把没做的事情说成做了；局限是 Codex 不需要面对数据库结果脱敏、审批、回滚等复杂状态。

**Claude 的设计方案**

Claude 的 stop hook、summary 和 transcript 机制适合在任务结束时整理过程。它的好处是最终表达自然；局限是如果没有结构化状态约束，模型可能遗漏某些关键数据库证据。

**最终方案**

mini-agent 新增独立 `final_report` 节点。该节点只读取结构化状态和 artifact 摘要，不直接依赖完整聊天历史。模型生成报告时必须使用 `DeliveryContract + ArtifactManifest + EvidenceReference`。最终效果是即使上下文被压缩，最终报告仍然完整可靠。

### 7.5 建立 PostgreSQL 报告模板

**Codex 的设计方案**

Codex 的最终交付通常有稳定模式：变更摘要、验证情况、未完成事项、下一步。这种固定收口让工程任务结果清晰；局限是模板偏代码实现，不足以覆盖数据库诊断和审批流程。

**Claude 的设计方案**

Claude 的 output style 能根据用户需求调整语气和结构。它的好处是表达适配性强；局限是结构稳定性依赖提示词。

**最终方案**

mini-agent 为 PostgreSQL 任务定义模板：

- 性能诊断模板：现象、证据、执行计划摘要、瓶颈、建议、风险、下一步。
- Schema 变更模板：目标、当前状态、变更 SQL、影响范围、锁风险、回滚、验证、审批。
- 数据修复模板：目标行范围、dry-run、审批、执行记录、验证结果、回滚或补偿。
- 权限变更模板：主体、权限差异、风险、审批、验证。
- 健康巡检模板：连接、慢查询、锁、索引、膨胀、风险优先级。

最终效果是报告更像 DBA 交付文档，而不是通用聊天回答。

### 7.6 SQL 交付物必须带安全元数据

**Codex 的设计方案**

Codex 在文件改动中强调 diff 和 patch，用户能看到具体变更内容。好处是变更边界清晰；局限是 SQL 执行除了文本差异，还需要环境、审批、风险和执行状态。

**Claude 的设计方案**

Claude 的工具权限上下文会关注工具是否允许执行。好处是交互式安全体验好；局限是 SQL 草稿、审批、执行、验证之间需要数据库 Agent 自己建模。

**最终方案**

mini-agent 新增 `SQLDeliveryItem`。凡是 SQL 草稿、变更 SQL、回滚 SQL、验证 SQL，都必须记录 `sql_hash`、classification、risk、target_environment、safety_report_id、approval_id、execution_record_id 和 status。最终效果是用户能清楚区分“草稿 SQL”“已审批 SQL”“已执行 SQL”“回滚 SQL”，避免数据库任务中最危险的状态混淆。

### 7.7 支持交付物版本和 diff

**Codex 的设计方案**

Codex 的 patch/diff 是非常核心的交付方式。用户可以看到每次修改的差异。好处是适合审查；局限是数据库交付物还需要展示 SQL diff、计划变化和报告版本。

**Claude 的设计方案**

Claude 的文件编辑工具能更新文件并在会话中说明变化。好处是对用户友好；局限是没有天然的数据库变更版本链。

**最终方案**

mini-agent 对 SQL 草稿、报告和交付包增加版本字段。SQL 修改生成 SQL diff；EXPLAIN 前后对比生成计划摘要 diff；报告重生成时记录 report diff。最终效果是用户审批前可以看到 Agent 到底改了什么。

### 7.8 用户可读版与审计版分离

**Codex 的设计方案**

Codex 的最终回答通常简洁，只告诉用户关键变更、测试结果和文件位置；详细证据留在工具输出或文件里。好处是最终回答不臃肿；局限是数据库审计信息需要更明确的归档结构。

**Claude 的设计方案**

Claude 支持 summary 和 transcript，前者适合快速阅读，后者适合完整回看。好处是信息层次清晰；局限是 transcript 太原始，不等同于审计报告。

**最终方案**

mini-agent 生成两层交付：`user_report` 给用户看，语言简洁；`audit_report` 给 DBA/审计看，包含 SQL hash、审批、工具调用、模型调用、质量门和证据清单。最终效果是普通用户不被日志淹没，审计人员也能复盘完整过程。

### 7.9 交付前必须运行 DeliveryQualityGate

**Codex 的设计方案**

Codex 在结束前倾向于验证当前状态，例如跑测试、检查命令结果、确认文件修改。好处是最终交付更可信；局限是它的质量检查主要面向代码任务。

**Claude 的设计方案**

Claude 的 hook 和权限机制适合在生命周期节点插入检查。好处是可以在任务结束、工具完成等阶段做额外动作；局限是检查规则需要业务系统定义。

**最终方案**

mini-agent 新增 `delivery_quality` gate，检查：

- 交付契约是否满足。
- 必要证据是否存在。
- 写操作是否有审批。
- SQL 是否有安全报告和 hash。
- 高风险任务是否有回滚方案。
- 执行后是否有验证结果。
- 报告是否引用证据。
- 敏感数据是否脱敏。
- 大结果是否只引用 artifact。

最终效果是报告不完整时不会被标记为成功交付。

### 7.10 DeliveryPackage 作为最终交付入口

**Codex 的设计方案**

Codex 的工作结果通常围绕文件、diff、测试和最终摘要展开。它的好处是用户知道去哪看结果；局限是多文件、多证据、多审批的数据库任务需要统一入口。

**Claude 的设计方案**

Claude 的任务通知和 summary 能把多步骤结果压缩给用户。好处是交互清楚；局限是缺少 artifact 级索引。

**最终方案**

mini-agent 新增 `DeliveryPackage`，作为一个任务的交付首页。它包含 user report、audit report、manifest、SQL 包、审批包、验证包、质量门结果和下一步动作。最终效果是复杂数据库任务也能一眼看清交付状态。

### 7.11 失败、中断、安全阻塞也要交付

**Codex 的设计方案**

Codex 区分 complete 和 blocked，不会把未完成任务说成完成。好处是状态诚实；局限是 blocked 输出通常是文本说明，没有数据库任务专用结构。

**Claude 的设计方案**

Claude 的任务和 summary 机制适合在任务中断时整理上下文。好处是用户可以接着处理；局限是需要业务层定义失败报告内容。

**最终方案**

mini-agent 新增 `blocked_report` 和 `error_delivery_report`。安全策略拦截、审批拒绝、工具失败、模型失败、验证失败都能生成交付物，说明已完成事项、已有证据、失败原因、未完成事项和可选恢复动作。最终效果是失败任务也可交接，不会只留下一句错误信息。

### 7.12 报告生成走模型路由，但必须受证据约束

**Codex 的设计方案**

Codex 的最终回答强调基于实际证据，不鼓励凭空总结。好处是可信；局限是报告表达不一定像业务文档。

**Claude 的设计方案**

Claude 的语言组织能力强，适合生成高质量用户报告。好处是可读性强；局限是如果缺少证据约束，容易生成看起来合理但不可审计的内容。

**最终方案**

mini-agent 使用 `ModelTask.report_generation` 生成报告。普通报告可用 cheap/balanced 模型，高风险报告可使用 review 模型。但提示词输入只能来自 `DeliveryContract`、`ArtifactManifest` 和结构化状态摘要，并要求每个关键结论绑定 evidence ref。最终效果是兼顾成本、可读性和可审计性。

### 7.13 敏感数据和大结果只通过引用交付

**Codex 的设计方案**

Codex 的工作区边界和最终回答风格会避免无意义地倾倒大量文件内容。好处是输出简洁；局限是数据库结果的敏感字段需要更严格策略。

**Claude 的设计方案**

Claude 的工具 schema 支持敏感参数不进入历史记录，且 transcript/summary 能区分完整过程和摘要。好处是能减少敏感信息扩散；局限是 PostgreSQL 字段脱敏和大结果生命周期仍要业务实现。

**最终方案**

mini-agent 交付时遵循输出安全策略：敏感字段脱敏，大结果写 artifact，报告只写摘要和 artifact 引用；secret 级别信息默认不进入最终报告。最终效果是报告可用，但不会把生产数据、连接信息或隐私字段泄露到聊天和长期记忆中。

### 7.14 交付结果驱动下一步人机协作

**Codex 的设计方案**

Codex 在需要权限或用户决定时会请求确认。好处是关键动作不越权；局限是数据库交付物还需要表达“审批、只读验证、重新生成、导出审计包”等动作。

**Claude 的设计方案**

Claude 的交互式任务通知和协作模式适合给用户选择下一步。好处是体验自然；局限是数据库动作必须受安全策略约束。

**最终方案**

mini-agent 在 `DeliveryPackage.next_actions` 中提供下一步动作，例如 `approve_execution`、`request_more_evidence`、`regenerate_sql`、`run_verification`、`export_audit_package`、`report_only`。所有动作再进入人机协作和安全护栏。最终效果是交付物不是终点，而是安全推进数据库流程的入口。

### 7.15 CLI/API 展示交付摘要

**Codex 的设计方案**

Codex 最终回答会告诉用户做了什么、改了哪些文件、测试是否通过。好处是收口清晰；局限是没有 PostgreSQL 交付包视图。

**Claude 的设计方案**

Claude 的 CLI 会展示工具进度、任务摘要、通知和 transcript。好处是交互反馈丰富；局限是交付包结构需要业务系统补齐。

**最终方案**

mini-agent CLI/API 展示最近 `DeliveryPackage`：状态、报告路径、SQL 包状态、审批状态、验证状态、质量门状态、下一步动作。最终回复只放简洁摘要和文件入口。最终效果是用户不需要翻长对话，也能找到交付物。

### 7.16 交付状态参与恢复和记忆

**Codex 的设计方案**

Codex 的 session 和 trace 机制可以恢复上下文，避免只靠模型记忆。好处是长任务更可控；局限是数据库交付状态需要专用字段。

**Claude 的设计方案**

Claude 通过任务文件、transcript 和 summary 支持跨回合延续。好处是适合长任务；局限是如果没有结构化交付状态，恢复后仍需重新推断。

**最终方案**

mini-agent 将 `delivery_contracts`、`artifact_manifests`、`delivery_packages` 写入 `AgentState`，并在长期记忆中只保存脱敏摘要和 artifact 引用。恢复任务时优先读取交付状态，判断哪些材料已完成、哪些还缺。最终效果是长数据库任务中断后能继续交付，而不是从聊天历史重新猜。

### 7.17 交付模块纳入测试和评估

**Codex 的设计方案**

Codex 强调用测试和命令结果验证完成质量。好处是交付可靠；局限是测试目标偏代码。

**Claude 的设计方案**

Claude 的任务总结能帮助人工判断质量。好处是可读；局限是自动化评估不足。

**最终方案**

mini-agent 增加交付相关评估用例：

- 慢 SQL 诊断必须包含 EXPLAIN 证据。
- DDL 交付必须包含 rollback 和 approval。
- 执行后交付必须包含 verification。
- 报告不得包含 secret 字段。
- final report 中每个关键结论必须有 evidence ref。
- blocked task 必须生成 blocked report。

最终效果是交付质量可以被 CI 和质量模块守住。

## 8. 推荐状态字段

建议在 `AgentState` 中增加：

```python
delivery_contracts: NotRequired[Annotated[list[DeliveryContract], operator.add]]
active_delivery_contract: NotRequired[Optional[DeliveryContract]]
artifact_manifests: NotRequired[Annotated[list[ArtifactManifest], operator.add]]
delivery_packages: NotRequired[Annotated[list[DeliveryPackage], operator.add]]
report_sections: NotRequired[Annotated[list[ReportSection], operator.add]]
sql_delivery_items: NotRequired[Annotated[list[SQLDeliveryItem], operator.add]]
evidence_references: NotRequired[Annotated[list[EvidenceReference], operator.add]]
```

也可以第一阶段只新增 `delivery_packages` 和 `artifact_manifests`，并复用已有 `artifact_records`、`quality_gates`、`quality_reports`。

## 9. 推荐节点

### 9.1 delivery_contract_builder

在任务理解后运行，根据 `current_intent` 和 `selected_workflow` 生成交付契约。

### 9.2 artifact_manifest_builder

在工具执行、审批、验证后运行，收集结构化证据并生成 artifact manifest。

### 9.3 final_report_generator

在任务完成、阻塞或失败时运行，根据交付契约和 manifest 生成用户报告和审计报告。

### 9.4 delivery_quality_gate

在报告交付前运行，检查报告完整性、安全性和证据引用。

### 9.5 delivery_presenter

负责把 `DeliveryPackage` 转成 CLI/API 用户可读输出。

## 10. 推荐流程

```text
intent_analyzer
  -> delivery_contract_builder
  -> task_planner
  -> agent_loop / tools / approvals / verification
  -> artifact_manifest_builder
  -> final_report_generator
  -> delivery_quality_gate
  -> delivery_presenter
```

如果任务失败：

```text
error_handler
  -> artifact_manifest_builder
  -> blocked_report / error_delivery_report
  -> delivery_quality_gate
  -> delivery_presenter
```

## 11. 与现有模块的关系

### 11.1 与任务理解

任务理解负责识别目标、风险、输出要求；产物模块把这些要求转成 `DeliveryContract`。

### 11.2 与规划模块

规划模块决定 observe、diagnose、approve、execute、verify、report 阶段；产物模块检查这些阶段是否产生必要交付材料。

### 11.3 与上下文管理

上下文管理为报告生成提供压缩后的工作集和证据摘要；产物模块避免直接读取庞大聊天历史。

### 11.4 与工具系统

工具系统产生 `ToolExecutionResult`、`DBObservation` 和 artifact；产物模块负责把这些材料组织成交付包。

### 11.5 与安全护栏

安全护栏决定 SQL 是否能执行、结果是否脱敏；产物模块负责在交付前确认这些安全结论已记录。

### 11.6 与人机协作

人机协作负责审批、拒绝、编辑、补充信息；产物模块把这些决策写入审批包和最终报告。

### 11.7 与质量控制

质量控制负责交付前检查；产物模块产生 `delivery_quality` gate 和质量摘要。

### 11.8 与模型路由

报告生成通过 `report_generation` 模型任务完成；高风险交付可升级为 review 模型。

## 12. 交付模板示例

### 12.1 慢 SQL 诊断报告

```markdown
# 慢 SQL 诊断报告

## 结论
- ...

## 证据
- EXPLAIN: evidence:obs-xxx
- 表结构: evidence:artifact-xxx

## 瓶颈判断
- ...

## 建议
- ...

## 风险
- ...

## 下一步
- ...
```

### 12.2 DDL 变更交付包

```text
DeliveryPackage
  user_report.md
  audit_report.md
  sql/change.sql
  sql/rollback.sql
  sql/verify.sql
  approvals/approval.json
  reports/lock_risk.md
  reports/verification.md
  manifest.json
```

### 12.3 阻塞报告

```markdown
# 任务阻塞报告

## 阻塞原因
- 缺少生产库写操作审批。

## 已完成
- 已完成 schema 观察。
- 已完成 SQL 分类。

## 缺失材料
- 用户审批。
- 回滚确认。

## 可选下一步
- 只生成报告。
- 补充审批。
- 改为 dry-run。
```

## 13. 测试建议

### 13.1 单元测试

- `DeliveryContract` 根据不同 intent 生成正确 required items。
- `ArtifactManifest` 能收集 observation、approval、verification。
- `SQLDeliveryItem` 能绑定 sql_hash、approval_id 和 safety_report_id。
- `DeliveryQualityGate` 能阻止缺证据报告。
- `DeliveryQualityGate` 能阻止缺回滚的高风险变更包。

### 13.2 集成测试

- 慢 SQL 诊断完整链路生成 final report。
- DDL 变更生成 SQL 包、审批包、回滚包、验证包。
- 用户拒绝审批后生成 blocked report。
- 执行失败后生成 error delivery report。

### 13.3 安全测试

- secret 字段不会进入 user report。
- 大查询结果不会直接进入最终回答。
- 生产库写操作没有 approval 时不能生成 ready 状态交付包。

### 13.4 回归测试

- 每个最终报告关键结论至少有一个 evidence ref。
- 每个 `DeliveryPackage` 必须有 manifest。
- 每个 persistent artifact 必须在 task workspace 中。

## 14. 分阶段落地

### 阶段一：交付契约和交付包

新增 `DeliveryContract`、`ArtifactManifest`、`DeliveryPackage` 状态对象，先不写复杂文件，只生成结构化状态和 Markdown final report。

### 阶段二：报告节点和模板

新增 `final_report_generator`，实现慢 SQL、schema change、data change、health check、blocked report 模板。

### 阶段三：SQL 包和审计包

新增 `SQLDeliveryItem`，绑定 SQL safety、approval、execution、verification。

### 阶段四：质量门

新增 `delivery_quality` gate，并接入 `QualityManager.quality_report()`。

### 阶段五：CLI/API 展示和导出

CLI 展示 delivery package 摘要；API 返回交付包索引；支持导出 audit package。

## 15. 最终方案总结

mini-agent 的产物生成与交付模块应该采用“契约驱动、证据支撑、文件落地、质量门拦截、双层报告”的方案。Codex 的核心借鉴点是：完成必须有当前状态证据，最终交付要说明实际变更和验证结果；Claude 的核心借鉴点是：输出需要适配用户场景，长任务需要 summary、transcript 和任务通知式交付。mini-agent 的最终取舍是：不把最终交付完全交给模型自由发挥，也不把所有过程日志倾倒给用户，而是用结构化状态生成可读报告和审计包。

最终效果是，mini-agent 管理 PostgreSQL 数据库时，不只是“回答问题”，而是能交付 DBA 真正能使用、审批、执行、验证和归档的任务成果。
