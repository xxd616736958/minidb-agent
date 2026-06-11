# PostgreSQL 管理智能体：任务理解与意图建模模块设计

## 1. 背景

mini-agent 当前的入口流程更接近通用编程 Agent：

```text
用户输入
  -> task_planner 判断是否需要拆任务
  -> memory_compactor
  -> llm_reason
  -> human_approval
  -> execute_tools
```

这个流程适合一般编程任务，但如果要把 mini-agent 优化为 PostgreSQL 数据库管理智能体，会遇到一个核心问题：数据库任务的风险差异非常大，用户一句自然语言可能对应只读查询、性能诊断、索引建议、DDL 变更、DML 数据修复、权限管理、备份恢复、报告生成等多种行为。如果不在执行前显式理解任务，Agent 很容易在信息不完整、环境不明确、风险未分级的情况下进入规划或工具调用。

因此需要增加一个独立的“任务理解与意图建模模块”。它不是为了把用户所有需求写死成枚举表，而是为了在进入 planner 之前，把用户的自然语言请求转成一个可检查、可追问、可路由、可审计的任务理解对象。

## 2. 模块定位

该模块负责回答四个问题：

1. 用户大概想完成什么类型的事情？
2. 这个任务是否和 PostgreSQL 数据库有关？
3. 当前信息是否足够进入规划和执行？
4. 这个任务的风险等级、下一步工作流和用户确认要求是什么？

推荐的新流程：

```text
用户输入
  -> intent_analyzer
  -> intent_validator
  -> clarification_gate
  -> workflow_planner
  -> memory_compactor
  -> llm_reason
  -> approval / tools / final
```

其中：

- `intent_analyzer` 负责用大模型生成结构化任务理解。
- `intent_validator` 负责用规则和数据库安全策略校验理解结果。
- `clarification_gate` 负责在缺少关键信息时请求用户澄清。
- `workflow_planner` 负责根据意图和风险选择后续工作流模板，再交给原有 planner 或替代原有 planner。

## 3. 设计目标

1. 不把系统做成僵硬的关键词分类器。
2. 不要求一句话只能命中一个意图。
3. 不枚举所有数据库操作。
4. 能处理模糊、多目标、跨领域任务。
5. 能在执行前显式暴露风险、缺失信息和下一步动作。
6. 能和现有 task planner、approval、tool executor、memory 继续协作。
7. 能为 PostgreSQL 高风险操作提供安全前置判断。

## 4. 非目标

1. 该模块不直接执行 SQL。
2. 该模块不替代 human approval。
3. 该模块不替代 planner。
4. 该模块不维护一个覆盖所有用户需求的巨大 intent 枚举表。
5. 该模块不保证大模型理解一定正确，而是让理解结果可检查、可追问、可修正。

## 5. 核心概念

### 5.1 意图不是具体动作枚举

这里的“意图”不是指：

```text
create_index
drop_table
write_test_report
analyze_slow_query
export_csv
```

这种设计会很快膨胀，每新增一种需求都要加一种意图，系统会变得不灵活。

本设计中的意图是粗粒度的“分诊方向”：

```text
read_only_analysis
performance_diagnosis
schema_change
data_change
permission_admin
backup_restore
documentation
general_question
unknown_or_mixed
```

具体要做什么，放在 `goal`、`targets`、`output_contract`、`workflow_hints` 等字段中表达。

### 5.2 结构化对象不是替代大模型，而是约束大模型输出

任务理解仍然主要依赖大模型完成。结构化对象的作用是让大模型的理解结果变成系统可以校验和使用的数据，而不是停留在一段自然语言解释里。

关键词匹配只作为辅助信号，用于风险兜底，例如识别 `DROP`、`TRUNCATE`、`DELETE`、`ALTER`、`GRANT` 这类高风险 SQL 关键词。

### 5.3 模糊任务允许存在

用户输入经常不是单一明确意图，例如：

```text
这个接口最近很慢，帮我看看是不是数据库问题，顺便给我一份报告。
```

这句话至少包含：

- 性能诊断
- 数据库证据收集
- 报告生成

系统不应该强行压成一个 intent，而应该允许：

```text
primary_intent: performance_diagnosis
candidate_intents:
  - performance_diagnosis
  - documentation
requires_clarification: false
```

如果缺少数据库连接、时间范围、接口名称和慢 SQL 样本，则应进入澄清。

## 6. 推荐数据结构

### 6.1 DBTaskIntent

```python
class DBTaskIntent(TypedDict):
    id: str
    domain: Literal["postgresql", "documentation", "code", "general", "unknown"]
    primary_intent: str
    candidate_intents: list[str]
    confidence: float

    goal: str
    user_language_summary: str

    operation_nature: Literal[
        "read_only",
        "diagnostic",
        "write_data",
        "schema_change",
        "permission_change",
        "backup_restore",
        "documentation",
        "unknown",
    ]

    target_environment: Literal["production", "staging", "dev", "local", "unknown"]
    target_database: Optional[str]
    target_objects: list[dict]

    input_artifacts: list[dict]
    output_contract: dict

    missing_slots: list[str]
    assumptions: list[str]
    constraints: list[str]

    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    requires_clarification: bool
    requires_approval: bool
    requires_rollback_plan: bool

    evidence_needed: list[str]
    suggested_workflow: str
    next_action: Literal[
        "ask_clarification",
        "plan",
        "read_only_observe",
        "request_approval",
        "decline",
    ]
```

### 6.2 为什么不把字段设计得更细

字段过细会导致每新增一种任务都要改 schema。例如“测试大纲”和“测试报告”不应分别新增 intent，而应该表示为：

```text
domain: documentation
primary_intent: documentation
output_contract.type: test_outline | test_report
evidence_needed: 是否需要查询数据库、是否需要真实测试结果
```

如果报告需要数据库证据，则 `domain` 可以是 `postgresql`，`candidate_intents` 同时包含 `documentation`。

## 7. 设计点与参考权衡

### 7.1 设计点一：粗粒度意图族，而不是穷举操作

**Codex 的做法**

Codex 没有维护一个巨大的“用户意图枚举表”。它更依赖系统提示、工具协议、计划工具、审批策略和执行环境，让模型在运行时理解用户目标。Codex 结构化的是 Agent 行为边界，例如计划、工具、审批、用户输入，而不是把所有用户请求预先分类。

**Claude 的做法**

Claude 也没有把所有用户意图规定死。它更强调任务化协作：todo、task、permission、worker、coordinator、远程会话等。用户目标由模型理解，系统把可协作、可审批、可追踪的行为结构化。

**最终方案**

mini-agent 采用少量稳定的意图族，而不是细粒度操作枚举。意图族只用于路由、风险判断和工作流选择；具体行为由 `goal`、`targets`、`output_contract` 和 planner 继续展开。

**权衡原因**

PostgreSQL 任务比通用编程任务风险更集中，完全隐式理解不够安全；但如果枚举所有操作，又会损失智能体灵活性。因此选择“粗粒度分诊 + 开放式目标描述”的中间方案。

**效果**

系统能识别“这是高风险数据修改”或“这是只读诊断”，但不会因为用户想写测试报告、容量报告、索引建议报告就不断新增 intent。

### 7.2 设计点二：LLM 生成结构化意图，规则负责校验

**Codex 的做法**

Codex 大量使用结构化协议承载模型行为，例如计划更新、工具调用、审批请求、用户输入请求。模型仍然负责理解任务，但输出被约束成系统可处理的事件和工具参数。

**Claude 的做法**

Claude 同样依赖模型进行开放理解，但在工具调用、todo、任务、权限请求等环节使用结构化对象，让 UI、权限系统和任务系统能消费模型结果。

**最终方案**

mini-agent 使用大模型生成 `DBTaskIntent` 草稿，再用确定性校验器修正或拦截：

```text
用户输入
  -> LLM 生成 DBTaskIntent
  -> validator 检查字段完整性、风险等级、SQL 关键词、环境信息
  -> 通过则进入 planner
  -> 不通过则进入 clarification
```

**权衡原因**

纯关键词匹配不理解上下文；纯 LLM 又不够稳定。LLM 负责语义理解，规则负责安全兜底，这是数据库场景更稳妥的组合。

**效果**

用户说“清掉老数据”，模型能理解为数据修改，规则能发现缺少表名、清理条件、备份策略和环境信息，从而阻止直接执行。

### 7.3 设计点三：支持多候选意图和模糊意图

**Codex 的做法**

Codex 在不确定时倾向通过计划、澄清或用户输入请求推进，而不是要求一开始就把任务完全分类正确。

**Claude 的做法**

Claude 通过 todo/task 可以把复合任务拆成多个工作项。例如一个请求既包含分析又包含实现，系统可以拆成多个可追踪步骤。

**最终方案**

`DBTaskIntent` 同时保留 `primary_intent` 和 `candidate_intents`。当置信度不足或候选方向冲突时，不强行选择，而是进入澄清或拆成多个工作流。

**权衡原因**

数据库任务经常是混合的。比如“优化慢查询并出报告”既是性能诊断也是文档生成。强行单分类会丢信息。

**效果**

系统既能保留模糊性，又能继续推进：先做只读诊断，最后生成报告；如果用户要求执行优化，再进入审批。

### 7.4 设计点四：缺失信息显式化，并触发澄清

**Codex 的做法**

Codex 有结构化用户输入请求能力，可以在任务执行中向用户提出具体问题，并等待用户回答后继续。

**Claude 的做法**

Claude 在交互式权限、elicitation、REPL 和远程会话中，把需要用户决策的点显式暴露出来，而不是只让模型自然语言追问。

**最终方案**

mini-agent 在 intent 阶段维护 `missing_slots`。缺少关键槽位时，不进入 planner，而是生成 `ClarificationRequest`：

```text
缺少 target_environment -> 问这是生产库、测试库还是本地库
缺少 target_objects -> 问涉及哪些表、索引、SQL 或接口
缺少 safety_boundary -> 问是否只读、是否允许生成变更 SQL
缺少 output_contract -> 问需要报告、SQL、诊断结论还是执行操作
```

**权衡原因**

数据库管理中，很多错误不是 SQL 写错，而是上下文不明确。先澄清比事后回滚更便宜。

**效果**

模糊任务不会直接执行。用户输入“帮我删掉无效数据”时，系统会先问“哪个表、无效规则、时间范围、是否备份、是否生产库”。

### 7.5 设计点五：风险等级在任务理解阶段产生

**Codex 的做法**

Codex 会根据执行环境、审批策略、工具调用风险决定是否需要用户授权。风险控制不是工具执行之后才出现，而是在执行前就影响流程。

**Claude 的做法**

Claude 使用权限上下文和工具使用确认机制，在工具调用前决定允许、拒绝、询问用户或交给其他控制逻辑。

**最终方案**

mini-agent 在 `DBTaskIntent` 中生成 `risk_level` 和 `requires_approval`：

```text
low: SELECT、EXPLAIN、查看元数据
medium: 生成索引建议、生成迁移 SQL 但不执行
high: CREATE INDEX、ALTER TABLE、UPDATE、DELETE、GRANT
critical: DROP、TRUNCATE、批量无条件修改、生产库不可逆操作
```

风险等级由 LLM 初判，validator 根据 SQL 动词、环境、是否生产、是否有 WHERE、是否有回滚方案进行兜底升级。

**权衡原因**

如果等到工具调用阶段才发现风险，planner 可能已经朝危险方向展开。提前建模风险，可以影响整个后续流程。

**效果**

高风险任务会自动要求澄清、审批和回滚方案；低风险只读任务可以快速执行。

### 7.6 设计点六：证据优先，先观察后建议再执行

**Codex 的做法**

Codex 在代码任务中通常先读取上下文、理解现状，再修改文件或执行命令。这种“先观察再行动”的模式适合迁移到数据库场景。

**Claude 的做法**

Claude 通过 todo/task 能把“收集证据、分析、执行、验证”拆成可追踪步骤，减少模型直接跳到结论。

**最终方案**

PostgreSQL 相关任务默认生成 `evidence_needed`：

```text
性能诊断:
  - SQL 文本
  - EXPLAIN / EXPLAIN ANALYZE
  - 表结构
  - 索引列表
  - 行数和统计信息
  - 锁等待或慢查询记录

数据修改:
  - 影响行数预估
  - 备份或快照信息
  - 回滚 SQL
  - WHERE 条件
```

**权衡原因**

数据库建议如果没有证据，容易变成经验猜测。证据优先会让 Agent 慢一点，但可靠性明显更高。

**效果**

用户说“优化这个 SQL”，Agent 会先收集执行计划和索引信息，而不是直接建议“加索引”。

### 7.7 设计点七：工作流模板替代细粒度 intent 爆炸

**Codex 的做法**

Codex 使用计划工具和执行协议，让模型按当前任务生成计划，不需要为每一种任务写死一个流程。

**Claude 的做法**

Claude 倾向把任务转成 todo/task，并结合工具和权限系统推进。它的工作流是产品化的，但不是每个用户请求都对应一个独立 intent。

**最终方案**

mini-agent 维护少量 PostgreSQL 工作流模板：

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

模板只规定阶段，不规定具体 SQL：

```text
performance_diagnosis_workflow:
  1. 确认问题范围
  2. 收集只读证据
  3. 分析瓶颈
  4. 生成优化建议
  5. 如需变更，进入审批和回滚设计
  6. 验证效果
```

**权衡原因**

工作流模板比 intent 枚举更稳定。新增“测试报告”“容量报告”“索引评估报告”通常只需要调整 output_contract 或 planner，不需要新增工作流。

**效果**

系统行为稳定，但仍保留模型规划的自由度。

### 7.8 设计点八：意图结果要能影响 planner，而不是只做标签

**Codex 的做法**

Codex 的计划工具不是装饰，它会影响用户对进度的理解，也会约束 Agent 后续行动。

**Claude 的做法**

Claude 的 todo/task 会进入任务状态，成为后续执行和 UI 展示的一部分。

**最终方案**

`DBTaskIntent` 必须成为 planner 的输入。planner 不再只看原始用户消息，而是看：

```text
原始用户请求
DBTaskIntent
missing_slots
risk_level
evidence_needed
suggested_workflow
user constraints
```

**权衡原因**

如果 intent 只作为日志字段保存，它对系统没有实际价值。它必须参与路由、规划、审批和工具选择。

**效果**

同一句“优化数据库”，在生产库和本地库、只读权限和允许变更权限下，会生成不同计划。

### 7.9 设计点九：保留非数据库任务的退路

**Codex 的做法**

Codex 是通用 Agent，不能因为任务不属于某个领域就无法处理。它通过通用工具、计划和自然语言能力处理开放任务。

**Claude 的做法**

Claude 也不会把所有请求限制在某个固定领域。即使有任务系统和权限系统，模型仍可以处理写文档、解释、总结、代码修改等任务。

**最终方案**

mini-agent 虽然优化为 PostgreSQL 管理智能体，但不应拒绝所有非数据库请求。对于“写测试大纲”“写测试报告”：

```text
如果不需要数据库证据:
  domain = documentation
  primary_intent = documentation
  next_action = plan

如果需要数据库证据:
  domain = postgresql
  candidate_intents = [documentation, read_only_analysis]
  evidence_needed = [query_result, schema_summary, metric_snapshot]
```

**权衡原因**

数据库 Agent 仍然会遇到报告、文档、解释、复盘等任务。完全拒绝会降低可用性；完全不区分又会降低安全性。

**效果**

系统既能处理文档类任务，又能在文档需要真实数据库证据时进入只读观察流程。

### 7.10 设计点十：任务理解结果要持久化进会话状态

**Codex 的做法**

Codex 使用 turn/session 状态跟踪待处理输入、审批、计划和事件，让多轮协作不会完全依赖聊天文本。

**Claude 的做法**

Claude 使用应用状态保存任务、todo、权限上下文、远程会话、通知等，使用户可以在复杂任务中持续协作。

**最终方案**

mini-agent 在 `AgentState` 增加：

```python
current_intent: Optional[DBTaskIntent]
intent_history: list[DBTaskIntent]
pending_clarification: Optional[dict]
confirmed_context: dict
```

**权衡原因**

数据库任务经常多轮推进。用户第二轮说“那就按第二个方案来”，系统需要知道“第二个方案”来自上一轮哪个意图、哪个表、哪个风险等级。

**效果**

多轮任务不会丢失数据库上下文，也方便审计用户确认过什么。

## 8. 节点设计

### 8.1 intent_analyzer

职责：

- 读取最新用户消息。
- 结合 working memory、当前数据库上下文、上一轮 intent。
- 调用无工具 LLM，输出 `DBTaskIntent` JSON。
- 不执行工具。

输出：

```python
{
    "current_intent": intent,
    "intent_history": [intent],
}
```

### 8.2 intent_validator

职责：

- 校验 JSON schema。
- 修正明显错误风险等级。
- 根据 SQL 关键词、环境、权限、目标对象补充风险。
- 计算 `missing_slots`。
- 决定 `next_action`。

典型规则：

```text
生产库 + DDL -> 至少 high
DROP/TRUNCATE -> critical
DELETE/UPDATE 无 WHERE -> critical
data_change 缺少 rollback -> requires_rollback_plan = true
target_environment unknown 且 risk >= high -> requires_clarification = true
```

### 8.3 clarification_gate

职责：

- 如果 `requires_clarification = true`，生成面向用户的澄清问题。
- 问题数量控制在 1-3 个。
- 用户回答后更新 `confirmed_context` 和 `current_intent`。

### 8.4 workflow_planner

职责：

- 根据 `suggested_workflow` 选择模板。
- 把模板、intent、风险和证据需求传给 planner。
- 生成可执行任务 DAG。

## 9. 与现有模块的关系

### 9.1 与 task_planner 的关系

当前 task_planner 直接分析用户原始输入。重构后它应该分析：

```text
user_message + current_intent + selected_workflow
```

如果任务很简单，planner 仍可返回空计划。

### 9.2 与 human_approval 的关系

intent 模块只判断是否需要审批，不执行审批。human_approval 仍然负责最终用户确认。

### 9.3 与 tool_executor 的关系

tool_executor 不负责理解用户意图，只执行已经被 planner 和 approval 允许的工具调用。

### 9.4 与 memory 的关系

memory 应保存用户确认过的稳定事实，例如：

- 当前默认数据库环境。
- 用户指定的只读限制。
- 已确认的目标表。
- 用户偏好的报告格式。

memory 不应保存一次性敏感信息，例如临时密码、一次性 token。

## 10. Prompt 设计原则

intent_analyzer 的系统提示应强调：

1. 不要执行 SQL。
2. 不要编造数据库名、表名、列名。
3. 不确定时写入 `missing_slots`。
4. 允许多个候选意图。
5. 意图族只用于路由，不要为了具体动作新增标签。
6. 高风险任务必须显式标注审批和回滚需求。
7. 输出必须是 JSON，不要输出解释文本。

## 11. 示例

### 11.1 慢 SQL 分析

用户输入：

```text
这个接口最近很慢，帮我看看是不是数据库问题。
```

意图结果：

```json
{
  "domain": "postgresql",
  "primary_intent": "performance_diagnosis",
  "candidate_intents": ["performance_diagnosis", "read_only_analysis"],
  "operation_nature": "diagnostic",
  "risk_level": "low",
  "missing_slots": ["interface_name_or_sql", "time_range", "target_environment"],
  "requires_clarification": true,
  "requires_approval": false,
  "evidence_needed": ["slow_query_sample", "execution_plan", "schema_summary", "index_summary"],
  "suggested_workflow": "performance_diagnosis_workflow",
  "next_action": "ask_clarification"
}
```

### 11.2 数据清理

用户输入：

```text
帮我清理掉 orders 表里的老数据。
```

意图结果：

```json
{
  "domain": "postgresql",
  "primary_intent": "data_change",
  "candidate_intents": ["data_change"],
  "operation_nature": "write_data",
  "risk_level": "high",
  "target_objects": [{"type": "table", "name": "orders"}],
  "missing_slots": ["old_data_definition", "target_environment", "backup_strategy", "rollback_plan"],
  "requires_clarification": true,
  "requires_approval": true,
  "requires_rollback_plan": true,
  "suggested_workflow": "data_change_workflow",
  "next_action": "ask_clarification"
}
```

### 11.3 测试报告

用户输入：

```text
帮我写一份数据库迁移测试报告。
```

意图结果：

```json
{
  "domain": "documentation",
  "primary_intent": "documentation",
  "candidate_intents": ["documentation", "read_only_analysis"],
  "operation_nature": "documentation",
  "risk_level": "low",
  "missing_slots": ["migration_scope", "test_result_source"],
  "requires_clarification": true,
  "requires_approval": false,
  "output_contract": {
    "type": "test_report",
    "format": "markdown"
  },
  "suggested_workflow": "documentation_workflow",
  "next_action": "ask_clarification"
}
```

## 12. 实施计划

### 阶段一：只做理解，不改变执行

1. 增加 `DBTaskIntent` schema。
2. 增加 `intent_analyzer` 节点。
3. 把 intent 结果写入 state。
4. CLI 打印“我理解你的任务是...”。
5. 不改变原有 task_planner 和工具执行。

验收标准：

- 用户输入数据库任务后，能看到结构化理解摘要。
- 模糊任务能显示缺失信息。
- 非数据库任务不会报错。

### 阶段二：接入澄清

1. 增加 `intent_validator`。
2. 增加 `clarification_gate`。
3. 对高风险且缺信息的任务阻止进入 planner。
4. CLI 支持回答澄清问题后 resume。

验收标准：

- “删掉老数据”不会直接进入执行。
- “只读分析这个 SQL”可以直接进入 planner。

### 阶段三：接入工作流模板

1. 增加 workflow registry。
2. planner 消费 `current_intent` 和 workflow。
3. 为慢 SQL、数据修改、DDL、报告生成提供模板。

验收标准：

- 慢 SQL 任务先收集证据。
- DDL/DML 任务默认生成回滚和审批步骤。

### 阶段四：接入审批和数据库工具

1. PostgreSQL 工具根据 `risk_level` 和 `operation_nature` 限制行为。
2. high/critical 操作必须经过 approval。
3. 执行前输出影响范围和回滚方案。

验收标准：

- `DROP`、`TRUNCATE`、无 WHERE 的 `DELETE/UPDATE` 无法绕过审批。
- 只读任务不会频繁打扰用户。

## 13. 最终取舍总结

本模块没有照搬 Codex，也没有照搬 Claude。

Codex 值得借鉴的是：

- 结构化协议。
- 计划和用户输入显式化。
- 执行前风险边界。
- 不把所有用户意图写死。

Claude 值得借鉴的是：

- 任务/todo 产品化。
- 权限上下文。
- 多轮协作状态。
- 把复杂任务拆成可追踪工作项。

mini-agent 的最终方案是：

```text
LLM 开放理解用户目标
  + 少量稳定意图族做分诊
  + 结构化 DBTaskIntent 暴露理解结果
  + 规则校验器做安全兜底
  + 缺失信息进入澄清
  + 风险等级影响 planner、approval 和工具
```

这样既不会把系统做死，也不会让数据库 Agent 完全凭自然语言自由发挥。

