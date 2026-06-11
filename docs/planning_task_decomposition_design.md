# PostgreSQL 管理智能体：规划与任务分解模块设计

## 1. 背景

mini-agent 当前的规划节点是通用型 `task_planner`：它读取用户原始请求，判断是否需要拆成 3 个以上逻辑步骤，如果需要，就让 LLM 输出一个通用 DAG：

```text
用户输入
  -> task_planner
  -> task_stack
  -> llm_reason
  -> execute_tools
```

这个设计对普通编程任务可用，但对 PostgreSQL 管理智能体不够。数据库任务不是简单的“拆成几步”，而是必须遵循安全流程：先观察、再分析、再生成方案、再审批、再执行、再验证。比如“清理老数据”和“写测试报告”都可能是多步任务，但前者是高风险数据修改，必须有影响行数预估、备份、回滚和审批；后者可能只是文档生成，也可能需要只读查询数据库证据。

因此，规划与任务分解模块需要从“通用任务 DAG 生成器”升级为“PostgreSQL 安全工作流规划器”。

## 2. 模块定位

该模块位于任务理解之后、工具调用之前：

```text
用户输入
  -> intent_analyzer
  -> intent_validator
  -> clarification_gate
  -> workflow_planner
  -> db_task_planner
  -> llm_reason
  -> approval / tools / final
```

输入：

- 用户原始请求。
- `DBTaskIntent`。
- `selected_workflow`。
- 用户约束。
- 当前会话上下文。

输出：

- `DBTaskPlan`。
- `DBTaskStep` 列表。
- 每个步骤的风险等级、证据需求、审批需求、回滚需求、完成标准。

该模块不执行 SQL，也不直接审批。它只负责把任务拆成安全、可追踪、可验证的步骤。

## 3. 设计目标

1. 让数据库任务默认遵循“先观察、后建议、再执行”的流程。
2. 让高风险步骤自动带审批、回滚和影响评估。
3. 让计划不仅是给模型看的，也能给用户看。
4. 让每一步都可追踪状态、结果和失败原因。
5. 支持模糊任务、多目标任务和用户中途变更目标后的重新规划。
6. 支持模板化安全流程，同时保留 LLM 动态补全能力。
7. 让后续工具调用、审批和 CLI 展示都能消费计划结构。

## 4. 非目标

1. 该模块不负责连接 PostgreSQL。
2. 该模块不负责执行 SQL。
3. 该模块不替代权限控制和 human approval。
4. 该模块不把所有数据库任务写死成固定步骤。
5. 该模块不追求一次规划永远正确，而是支持执行过程中更新计划。

## 5. 核心设计原则

### 5.1 计划不是自然语言，而是状态对象

当前 mini-agent 已有 `TaskStep`，但字段较少：

```text
id
description
status
dependencies
result
error
```

PostgreSQL 管理场景需要更丰富的步骤语义：

```text
phase
operation_type
risk_level
requires_approval
requires_rollback_plan
evidence_required
success_criteria
tool_policy
```

这样计划才能驱动审批、工具选择、风险展示和验证。

### 5.2 模板保证安全，LLM 补全细节

完全让 LLM 自由规划不安全，容易漏掉“影响行数预估”“回滚 SQL”“审批”等关键步骤。

完全写死模板又不灵活，无法适配用户具体任务。

推荐方案是：

```text
DBTaskIntent
  -> 选择安全工作流模板
  -> LLM 基于模板补全具体步骤
  -> deterministic validator 校验计划
  -> 输出 DBTaskPlan
```

### 5.3 高风险任务必须拆出安全步骤

对于 DDL、DML、权限、备份恢复任务，planner 不能直接生成“执行变更”一步，而必须拆成：

```text
确认范围
  -> 只读观察
  -> 影响评估
  -> 生成变更 SQL
  -> 生成回滚方案
  -> 用户审批
  -> 执行
  -> 验证
  -> 总结
```

### 5.4 计划需要支持重规划

数据库任务经常会因为以下情况改变：

- 用户补充“这是生产库，只允许只读”。
- 发现没有权限。
- 查询结果显示目标表不存在。
- 影响行数超过阈值。
- 执行计划与预期不同。
- 用户中途要求只生成报告，不执行变更。

所以 planner 不能只在第一轮运行一次。它应该支持在用户补充信息、工具失败、风险升级或目标变化后重新规划。

## 6. 推荐数据结构

### 6.1 DBTaskStep

```python
class DBTaskStep(TypedDict):
    id: str
    description: str
    status: Literal["pending", "running", "blocked", "completed", "failed", "skipped"]
    dependencies: list[str]

    phase: Literal[
        "clarify",
        "observe",
        "diagnose",
        "propose",
        "approve",
        "execute",
        "verify",
        "report",
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
    requires_approval: bool
    requires_rollback_plan: bool
    evidence_required: list[str]
    success_criteria: list[str]

    expected_tools: list[str]
    tool_policy: Literal["no_tools", "read_only_tools", "write_tools_after_approval"]

    result: Optional[str]
    error: Optional[str]
```

### 6.2 DBTaskPlan

```python
class DBTaskPlan(TypedDict):
    id: str
    intent_id: str
    workflow: str
    summary: str
    status: Literal["draft", "awaiting_approval", "running", "completed", "failed", "cancelled"]
    steps: list[DBTaskStep]
    assumptions: list[str]
    constraints: list[str]
    global_risk_level: Literal["low", "medium", "high", "critical"]
    requires_user_confirmation: bool
    created_at: str
    updated_at: str
```

## 7. 设计点与参考权衡

### 7.1 设计点一：计划是用户可见的协作界面

**Codex 的做法**

Codex 通过计划更新机制把任务拆解和进度显式展示给用户。计划不是内部草稿，而是人机协作的一部分。用户可以看到 Agent 准备做什么、当前做到哪一步、后面还剩什么。

**Claude 的做法**

Claude 通过 todo/task 机制把复杂任务变成可追踪的工作项。它更强调产品化的任务列表、状态展示和多任务协作。

**最终方案**

mini-agent 的计划应同时满足两类需求：

- 像 Codex 一样，用计划向用户透明说明执行路径。
- 像 Claude 一样，用任务项维护状态、结果、错误和后续动作。

计划不能只是一段 Markdown 文本，而必须是结构化 `DBTaskPlan`。

**权衡原因**

数据库任务风险高，用户必须看懂 Agent 下一步准备做什么；同时任务执行可能很长，系统必须维护状态。

**效果**

用户能看到：

```text
1. 只读收集 orders 表索引和统计信息
2. 获取慢 SQL 的 EXPLAIN 计划
3. 生成索引优化建议
4. 等待用户确认是否生成 CREATE INDEX
```

而不是只看到“我将分析并优化数据库”。

### 7.2 设计点二：使用 PostgreSQL 工作流模板，而不是完全自由规划

**Codex 的做法**

Codex 不为每种任务写死流程，而是让模型结合上下文规划。但 Codex 的系统提示会强约束“先理解、再修改、再验证”等行为。

**Claude 的做法**

Claude 的 todo/task 更偏通用任务管理，模型可以按任务需要生成步骤，但权限和工具使用会约束执行边界。

**最终方案**

mini-agent 使用少量数据库工作流模板：

```text
read_only_analysis_workflow
performance_diagnosis_workflow
schema_change_workflow
data_change_workflow
permission_admin_workflow
backup_restore_workflow
documentation_workflow
unknown_or_mixed_workflow
```

模板只规定阶段，不写死具体 SQL。LLM 根据用户任务补全表名、指标、SQL、报告结构等细节。

**权衡原因**

PostgreSQL 场景比通用编程更需要安全顺序。模板负责守住安全流程，LLM 负责灵活适配。

**效果**

慢 SQL 分析永远先收集证据，数据修改永远先预估影响并生成回滚，而不是让 LLM 每次重新发明流程。

### 7.3 设计点三：先观察后行动

**Codex 的做法**

Codex 在代码任务中通常先读取文件、理解项目结构和现有实现，再编辑代码。这个模式避免盲目修改。

**Claude 的做法**

Claude 的任务拆解也会把探索、修改、验证拆成不同工作项。它适合长任务中持续追踪“已经观察了什么、还要验证什么”。

**最终方案**

所有 PostgreSQL 计划默认先生成只读观察步骤：

```text
observe:
  - 查看 schema
  - 查看索引
  - 查看统计信息
  - 查看锁等待
  - 获取 EXPLAIN
  - 预估影响行数
```

只有当观察结果支持后续操作，并且风险条件满足时，才进入 propose/approve/execute。

**权衡原因**

数据库建议如果没有证据，很容易变成猜测；数据库变更如果没有影响评估，风险不可控。

**效果**

用户说“给这个慢 SQL 建个索引”，计划不会直接执行 `CREATE INDEX`，而是先确认执行计划、现有索引、数据量和过滤条件。

### 7.4 设计点四：高风险步骤自动插入审批和回滚

**Codex 的做法**

Codex 的执行会受到 sandbox 和 approval policy 约束。涉及风险操作时，系统不会把所有命令都当成普通步骤处理。

**Claude 的做法**

Claude 有权限上下文和工具使用确认机制。工具调用前可以进入用户确认、拒绝或其他控制流程。

**最终方案**

planner 根据 `DBTaskIntent.risk_level` 和 `operation_nature` 自动插入：

```text
impact_estimation
rollback_plan
approval_request
post_execution_verification
```

例如 `data_change` 计划必须包含：

```text
1. SELECT 预估影响行数
2. 生成 UPDATE/DELETE SQL
3. 生成回滚 SQL 或备份策略
4. 请求用户审批
5. 执行已审批 SQL
6. 验证影响行数和数据状态
```

**权衡原因**

如果依赖模型自觉添加审批和回滚，风险太高。审批/回滚必须成为 planner validator 的硬性规则。

**效果**

即使用户说“直接删掉这些数据”，计划也不会绕过影响评估和审批。

### 7.5 设计点五：每个步骤都有完成标准

**Codex 的做法**

Codex 会在任务结束前强调验证，例如运行测试、检查结果、总结变更。它关心任务是否真的完成。

**Claude 的做法**

Claude 的 todo/task 状态可以标记完成、未完成、进行中，使任务进度可追踪。

**最终方案**

每个 `DBTaskStep` 必须带 `success_criteria`，例如：

```text
observe step:
  - 已获取目标表 schema
  - 已获取索引列表
  - 已获取 EXPLAIN 计划

data_change step:
  - 已确认 WHERE 条件
  - 已预估影响行数
  - 已生成回滚 SQL

verify step:
  - 已确认变更后查询结果符合预期
  - 已记录实际影响行数
```

**权衡原因**

没有完成标准，Agent 容易把“做了一点分析”当成“完成任务”。

**效果**

计划执行可以被系统检查，CLI 也能显示每一步为什么完成或为什么失败。

### 7.6 设计点六：计划必须影响工具策略

**Codex 的做法**

Codex 的计划和执行环境不是完全割裂的。权限模式、sandbox 和审批策略会影响实际可做的动作。

**Claude 的做法**

Claude 的 tool use 会受 permission context 影响。不同任务状态和工具风险会决定是否询问用户。

**最终方案**

`DBTaskStep.tool_policy` 直接约束后续工具调用：

```text
no_tools:
  只能让 LLM 输出解释或文档

read_only_tools:
  只能使用 SELECT、EXPLAIN、元数据查询类工具

write_tools_after_approval:
  只有 approval 通过后才能执行 DDL/DML/权限变更
```

**权衡原因**

如果计划只是展示，不影响工具执行，那安全价值很弱。计划必须成为工具层的输入。

**效果**

当当前步骤是 observe，PostgreSQL 工具即使被模型要求执行 DELETE，也应该拒绝。

### 7.7 设计点七：计划支持动态重规划

**Codex 的做法**

Codex 会在任务推进中更新计划，完成一步标记一步，遇到新信息时调整后续步骤。

**Claude 的做法**

Claude 的 todo/task 支持持续更新，复杂任务可以根据执行结果调整任务列表。

**最终方案**

mini-agent 增加 `replan_trigger`：

```text
user_changed_goal
missing_permission
tool_error
risk_escalated
unexpected_result
impact_too_large
clarification_answered
```

当触发条件出现时，planner 不应沿用旧计划，而应基于当前状态生成修订版计划。

**权衡原因**

数据库任务的不确定性高，执行计划必须能吸收新信息。

**效果**

如果预估影响行数从 500 变成 50 万，系统会停止原执行计划，重新规划为“报告风险并请求用户确认”，而不是继续执行。

### 7.8 设计点八：计划区分阶段角色

**Codex 的做法**

Codex 虽然不一定显式定义角色，但其流程通常体现出探索、修改、验证、总结等阶段。

**Claude 的做法**

Claude 有 coordinator、worker、task 等概念，复杂任务可以被分派成不同职责的工作项。

**最终方案**

mini-agent 不必马上实现多 Agent，但规划阶段应显式区分任务角色：

```text
observer: 收集只读证据
diagnoser: 分析瓶颈或问题
proposer: 生成方案
executor: 执行已审批操作
verifier: 验证结果
reporter: 输出报告
```

这些角色可以映射到 `phase` 字段。

**权衡原因**

角色化阶段让计划更稳定，也为未来拆成多 Agent 或后台任务留下接口。

**效果**

性能诊断不会混成一句“分析并优化”，而是明确分成观察、诊断、建议、验证和报告。

### 7.9 设计点九：计划要保留用户约束

**Codex 的做法**

Codex 会遵循用户显式要求，例如是否运行测试、是否改文件、是否只分析不修改。

**Claude 的做法**

Claude 的任务系统和权限系统会把用户决策融入后续执行。

**最终方案**

`DBTaskPlan.constraints` 必须包含用户约束：

```text
只读
只生成 SQL，不执行
只针对 staging
不要锁表
只输出报告
执行前必须确认
影响行数超过阈值则停止
```

planner validator 必须检查计划是否违反约束。

**权衡原因**

数据库任务中，用户约束往往就是安全边界。如果计划不保留约束，后续 LLM 容易忘记。

**效果**

用户说“只读分析”，计划里不会出现 execute phase；用户说“只生成迁移 SQL”，计划不会包含真实执行步骤。

### 7.10 设计点十：计划输出同时服务 CLI 和执行器

**Codex 的做法**

Codex 的计划更新面向用户，但也反映内部任务状态。

**Claude 的做法**

Claude 的 todo/task 既服务 UI，也服务 Agent 对当前工作的组织。

**最终方案**

mini-agent 的 `DBTaskPlan` 应同时服务：

- CLI 展示。
- LLM 当前任务上下文。
- PostgreSQL 工具策略。
- human approval。
- 执行后验证。
- 会话恢复和审计。

**权衡原因**

如果 CLI 一份计划、执行器一份计划、LLM 一份计划，三者很容易不一致。

**效果**

用户看到的计划就是系统真实执行依据，降低黑盒感和安全风险。

## 8. 工作流模板设计

### 8.1 read_only_analysis_workflow

```text
1. clarify: 确认分析目标和输出格式
2. observe: 只读收集 schema、索引、统计信息或查询结果
3. diagnose: 分析发现
4. report: 输出结论和建议
```

### 8.2 performance_diagnosis_workflow

```text
1. clarify: 确认慢 SQL、接口、时间范围和环境
2. observe: 收集 EXPLAIN、索引、行数、统计信息、锁等待
3. diagnose: 判断瓶颈
4. propose: 生成优化方案
5. approve: 如需变更，等待用户确认
6. execute: 执行已批准优化
7. verify: 对比优化前后结果
8. report: 输出诊断报告
```

### 8.3 schema_change_workflow

```text
1. clarify: 确认环境、目标对象、变更目标
2. observe: 查看现有 schema、依赖、索引、数据量
3. propose: 生成迁移 SQL、锁风险、兼容性说明
4. propose: 生成回滚 SQL
5. approve: 用户审批
6. execute: 执行已审批 DDL
7. verify: 验证 schema 和应用影响
8. report: 总结变更
```

### 8.4 data_change_workflow

```text
1. clarify: 确认表、过滤条件、环境、备份要求
2. observe: SELECT 预估影响行数
3. propose: 生成 DML 和回滚方案
4. approve: 用户审批
5. execute: 执行已审批 DML
6. verify: 校验影响行数和数据状态
7. report: 输出执行记录
```

### 8.5 documentation_workflow

```text
1. clarify: 确认文档类型、读者、格式、证据来源
2. observe: 如需要，收集只读证据
3. report: 生成文档
4. verify: 检查文档是否覆盖用户要求
```

## 9. 计划校验器

LLM 生成计划后，必须经过 deterministic validator。

校验规则包括：

```text
high/critical step 必须 requires_approval = true
data_change 必须包含 observe 影响评估
data_change 必须包含 rollback plan
schema_change 必须包含 rollback plan 或说明不可回滚风险
execute step 不能出现在 approve step 之前
read_only constraint 下不能出现 write tool_policy
production 环境下 high/critical 必须有 approval
所有 step 必须有 success_criteria
所有 dependencies 必须引用存在的 step id
DAG 不允许循环依赖
```

校验失败时：

```text
可修复问题 -> 自动修复
不可修复问题 -> 返回 planner 重新生成
安全问题 -> 阻止进入执行
```

## 10. 与现有模块的关系

### 10.1 与任务理解模块

规划模块不重新猜用户意图，而是消费 `DBTaskIntent`。

如果 `DBTaskIntent.requires_clarification = true`，planner 不应运行。

### 10.2 与工具调用模块

当前步骤的 `tool_policy` 应传给 PostgreSQL 工具层。工具层根据步骤策略拒绝越权 SQL。

### 10.3 与审批模块

planner 负责插入 approval step，审批模块负责真正收集用户确认。

### 10.4 与状态管理模块

`DBTaskPlan` 和 `DBTaskStep` 应持久化进 LangGraph state，支持 resume、history 和审计。

### 10.5 与人机协作模块

CLI 应展示结构化计划，并允许用户确认、跳过、编辑、重新规划。

## 11. 实施计划

### 阶段一：扩展计划数据结构

1. 增加 `DBTaskStep` 和 `DBTaskPlan`。
2. 保留现有 `TaskStep` 兼容字段。
3. planner 输出结构化计划。
4. CLI 展示 phase、risk、status。

验收标准：

- 计划里能看到每一步的阶段、风险、完成标准。
- 现有普通任务仍能运行。

### 阶段二：接入工作流模板

1. 增加 workflow registry。
2. 根据 `DBTaskIntent.suggested_workflow` 选择模板。
3. LLM 只补全模板中的具体任务。
4. planner prompt 注入 intent、workflow、constraints。

验收标准：

- 慢 SQL 任务包含 observe/diagnose/propose。
- DML 任务包含 observe/rollback/approval/verify。

### 阶段三：增加计划校验器

1. 校验审批、回滚、依赖和只读约束。
2. 自动修复缺失的安全步骤。
3. 对不可修复计划阻止执行。

验收标准：

- 高风险计划无法缺少 approval。
- 只读任务不会出现 execute write step。

### 阶段四：让计划影响执行

1. llm_reason 注入当前 `DBTaskStep`。
2. PostgreSQL 工具读取当前步骤 `tool_policy`。
3. execute_tools 完成后更新步骤状态。
4. 验证步骤检查 `success_criteria`。

验收标准：

- observe 阶段无法执行写 SQL。
- execute 阶段必须审批通过。

### 阶段五：支持重规划

1. 增加 `replan_trigger`。
2. 用户补充信息、工具失败、风险升级时重新规划。
3. 保留旧计划和新计划的审计记录。

验收标准：

- 用户说“只读，不要执行”后，计划移除写操作。
- 影响行数过大时，计划停止执行并请求用户确认。

## 12. 最终取舍总结

Codex 值得借鉴的是：

- 计划透明。
- 执行前理解上下文。
- 计划随任务推进更新。
- 权限和执行环境会影响行动。

Claude 值得借鉴的是：

- todo/task 的产品化任务列表。
- 任务状态持续维护。
- 复杂任务可以被拆成可协作工作项。
- 权限上下文影响工具使用。

mini-agent 的最终方案是：

```text
DBTaskIntent
  -> 选择 PostgreSQL 工作流模板
  -> LLM 补全具体任务步骤
  -> validator 校验安全规则
  -> 生成 DBTaskPlan
  -> CLI 展示
  -> llm_reason 和工具执行消费当前步骤
  -> 根据结果更新或重规划
```

这个方案避免了两种极端：

- 不完全依赖 LLM 自由规划，避免漏掉数据库安全步骤。
- 不把所有任务写死，避免新增需求时不断添加硬编码流程。

最终目标是让 mini-agent 的规划模块从“通用任务拆解器”升级为“PostgreSQL 安全工作流规划器”。

