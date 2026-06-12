# PostgreSQL 管理智能体：推理循环 / Agent Loop 模块设计

## 1. 背景

mini-agent 当前的核心循环接近通用工具型 Agent：

```text
llm_reason
  -> human_approval
  -> execute_tools
  -> memory_compactor
  -> llm_reason
  -> END
```

这个循环适合一般编程任务，但不足以支撑 PostgreSQL 管理智能体。数据库任务不只是“模型想调用工具就调用工具”，而是必须围绕当前计划步骤推进：当前步骤允许什么工具、需要什么证据、风险多高、是否需要审批、执行后如何验证、什么时候重规划。

前面两个模块已经提供了：

- `DBTaskIntent`：结构化任务理解。
- `DBTaskPlan` / `TaskStep`：结构化数据库安全计划。

因此 Agent Loop 的下一步改造目标是：让执行循环从“LLM 自由驱动”升级为“计划步骤驱动的 PostgreSQL 安全执行循环”。

## 2. 模块定位

推理循环位于规划之后、工具执行和最终总结之间：

```text
DBTaskIntent
  -> DBTaskPlan
  -> Agent Loop
      -> select_step
      -> reason_on_step
      -> tool_policy_gate
      -> approval_gate
      -> execute_tool
      -> normalize_observation
      -> verify_step
      -> update_plan
      -> replan_or_next
      -> final_report
```

输入：

- `DBTaskPlan`
- 当前 `TaskStep`
- 用户消息和历史上下文
- 工具调用结果
- 审批结果
- 用户中途反馈

输出：

- 更新后的 `DBTaskPlan`
- 结构化观察结果 `DBObservation`
- 审批记录 `ApprovalDecision`
- 验证结果 `VerificationResult`
- 是否继续、等待用户、重规划或结束

## 3. 设计目标

1. 每轮推理都围绕当前计划步骤，而不是让 LLM 自由游走。
2. 工具调用必须受当前步骤的 `tool_policy` 约束。
3. 高风险数据库操作必须进入显式审批分支。
4. 工具结果要结构化为数据库观察对象。
5. 执行后必须验证，不允许执行完直接总结。
6. 异常、风险升级、用户改口时支持重规划。
7. loop 的终止条件基于计划状态，而不是“本轮没有工具调用”。
8. 每轮关键状态可持久化、可恢复、可审计。

## 4. 非目标

1. 本模块不负责生成初始意图。
2. 本模块不负责生成初始计划。
3. 本模块不实现具体 PostgreSQL 驱动。
4. 本模块不替代审批模块，而是决定何时进入审批。
5. 本模块不把所有错误都自动恢复；不可恢复错误应明确阻塞。

## 5. 推荐状态结构

### 5.1 DBObservation

```python
class DBObservation(TypedDict):
    id: str
    step_id: str
    type: Literal[
        "query_result",
        "explain_plan",
        "schema_summary",
        "index_summary",
        "row_count_estimate",
        "lock_wait",
        "affected_rows",
        "sql_error",
        "tool_error",
    ]
    source_tool: str
    summary: str
    payload: dict
    created_at: str
```

### 5.2 ApprovalDecision

```python
class ApprovalDecision(TypedDict):
    id: str
    step_id: str
    status: Literal["pending", "approved", "rejected", "edited", "expired"]
    risk_level: Literal["low", "medium", "high", "critical"]
    target_environment: str
    sql_preview: Optional[str]
    impact_summary: Optional[str]
    rollback_summary: Optional[str]
    user_message: Optional[str]
    created_at: str
    resolved_at: Optional[str]
```

### 5.3 VerificationResult

```python
class VerificationResult(TypedDict):
    id: str
    step_id: str
    status: Literal["passed", "failed", "blocked", "skipped"]
    criteria_checked: list[str]
    evidence_ids: list[str]
    summary: str
    created_at: str
```

### 5.4 DBLoopState

可以直接并入 `AgentState`：

```python
current_step_id: Optional[str]
db_observations: list[DBObservation]
approval_decisions: list[ApprovalDecision]
verification_results: list[VerificationResult]
pending_approval: Optional[ApprovalDecision]
loop_status: Literal[
    "running",
    "waiting_for_user",
    "waiting_for_approval",
    "replanning",
    "completed",
    "blocked",
]
replan_trigger: Optional[str]
```

## 6. 设计点与参考权衡

### 6.1 设计点一：计划步骤驱动，而不是 LLM 自由驱动

**Codex 的做法**

Codex 通过计划更新机制和 turn 状态，让 Agent 在长任务中围绕当前目标推进。计划不是模型内部隐藏推理，而是协作过程的一部分。

**Claude 的做法**

Claude 通过 todo/task 维护任务状态，复杂任务不会只靠一段自然语言上下文推进，而是由任务项持续承载进度。

**最终方案**

mini-agent 的 loop 每轮先读取 `DBTaskPlan.steps`，选择一个可执行的 `TaskStep`，把该步骤的 `phase/risk/tool_policy/success_criteria` 注入 `llm_reason`。LLM 只能围绕当前步骤推理。

**权衡原因**

Codex 的计划透明性适合人机协作，Claude 的 task 状态适合长任务执行。数据库 Agent 同时需要这两点。

**效果**

Agent 不会在“只读观察”步骤里突然执行数据修改，因为当前步骤明确限制了允许动作。

### 6.2 设计点二：增加 step_scheduler

**Codex 的做法**

Codex 会维护计划项状态，例如 pending、in_progress、completed，使用户和系统知道当前推进到哪里。

**Claude 的做法**

Claude 的 todo/task 更强调任务列表管理，可以根据任务状态选择下一项工作。

**最终方案**

增加 `step_scheduler` 节点：

```text
输入 DBTaskPlan.steps
  -> 找到 status=pending 且 dependencies 已完成的步骤
  -> 标记为 running
  -> 写入 current_step_id
```

如果没有可执行步骤：

- 全部完成 -> final_report
- 有 pending approval -> waiting_for_approval
- 有 blocked/failed -> error_or_replan

**权衡原因**

当前 `current_task_index + 1` 只能支持简单线性计划。数据库计划有依赖、审批和阻塞，需要独立 scheduler。

**效果**

`execute-approved-change` 不会在 `request-approval` 完成前运行。

### 6.3 设计点三：工具调用前增加 tool_policy_gate

**Codex 的做法**

Codex 的工具执行受到 sandbox、approval policy 和执行环境约束。工具调用不是模型说了就能执行。

**Claude 的做法**

Claude 使用 permission context 和工具权限判断，在工具执行前决定允许、拒绝、询问用户或走其他控制逻辑。

**最终方案**

在 `llm_reason` 和 `execute_tools` 之间增加 `tool_policy_gate`：

```text
no_tools:
  拒绝所有工具调用，要求 LLM 用已有信息完成当前步骤

read_only_tools:
  只允许 SELECT、EXPLAIN、元数据查询类 PostgreSQL 工具

write_tools_after_approval:
  必须检查 approval_decision=approved
```

**权衡原因**

数据库工具风险高，不能只依赖工具内部保护。loop 层应该先做一次策略分流。

**效果**

即使 LLM 误生成 `DELETE`，当前步骤是 `read_only_tools` 时也会被拦截。

### 6.4 设计点四：默认先观察，再行动

**Codex 的做法**

Codex 在代码任务中倾向先读取上下文、理解项目，再编辑文件。这个模式避免盲目修改。

**Claude 的做法**

Claude 的 task/todo 可以把探索、实现、验证拆成独立任务项。

**最终方案**

Agent Loop 遇到 PostgreSQL 计划时，优先执行 `observe` 阶段，只读收集：

- schema
- index
- statistics
- EXPLAIN
- lock wait
- row count estimate

没有观察证据时，不能进入高风险 execute。

**权衡原因**

数据库决策必须基于证据。Codex 的“先读再改”和 Claude 的阶段化任务都适合迁移到数据库场景。

**效果**

“优化慢 SQL”会先拿执行计划和索引信息，再给优化建议。

### 6.5 设计点五：工具结果结构化为 DBObservation

**Codex 的做法**

Codex 使用事件化协议传递工具调用、审批、用户输入和执行结果，使系统可以消费这些状态。

**Claude 的做法**

Claude 的工具执行和 UI 状态也不是纯文本流，权限、任务、工具结果都会进入应用状态。

**最终方案**

增加 `normalize_observation` 节点，把 PostgreSQL 工具输出转成 `DBObservation`：

```text
EXPLAIN 输出 -> explain_plan
SELECT COUNT -> row_count_estimate
pg_locks 查询 -> lock_wait
DDL/DML 结果 -> affected_rows
SQL 异常 -> sql_error
```

**权衡原因**

原始工具输出适合给人看，但不适合自动验证和重规划。结构化观察让 loop 能做规则判断。

**效果**

如果 `affected_rows` 超过阈值，可以自动触发 `risk_escalated`，而不是让模型从文本里猜。

### 6.6 设计点六：高风险步骤进入显式审批分支

**Codex 的做法**

Codex 有 pending approvals 和 reviewer 策略，高风险操作会进入审批等待状态。

**Claude 的做法**

Claude 有 permission queue 和权限弹窗，工具执行前可以等待用户批准。

**最终方案**

增加 `approval_gate`：

```text
如果 step.requires_approval = false:
  继续执行

如果 step.requires_approval = true 且无 approved decision:
  生成 ApprovalDecision(status=pending)
  loop_status = waiting_for_approval
  暂停执行

如果 approved:
  继续 execute_tools
```

审批 payload 必须包含：

- 环境
- SQL 预览
- 风险等级
- 影响行数预估
- 回滚方案
- 验证方式

**权衡原因**

Codex 的审批状态严谨，Claude 的权限交互产品化。mini-agent 应同时保留严谨状态和清晰用户确认。

**效果**

用户确认的是精确 SQL 和回滚方案，而不是泛泛地同意“继续执行”。

### 6.7 设计点七：执行后必须验证

**Codex 的做法**

Codex 在修改代码后通常会验证，例如运行测试、检查结果或说明未验证的风险。

**Claude 的做法**

Claude 的任务状态需要被持续更新，完成一个 todo 前应确认任务条件满足。

**最终方案**

增加 `verify_step` 节点，读取当前 `TaskStep.success_criteria` 和 `DBObservation`：

```text
criteria 全部满足 -> step completed
缺少证据 -> step blocked 或回到 observe
验证失败 -> replan 或 error_handler
```

**权衡原因**

数据库操作“工具成功”不等于“任务完成”。必须把成功标准和证据绑定起来。

**效果**

执行 `CREATE INDEX` 后，会验证索引是否存在、执行计划是否使用索引，而不是直接宣布优化完成。

### 6.8 设计点八：增加 replan_gate

**Codex 的做法**

Codex 会在任务中途根据新信息更新计划，而不是机械执行旧计划。

**Claude 的做法**

Claude 的 todo/task 可以被持续修改，适合根据工具结果和用户反馈调整路线。

**最终方案**

增加 `replan_gate`，触发条件包括：

```text
tool_error
missing_permission
risk_escalated
impact_too_large
unexpected_result
user_changed_goal
clarification_answered
```

触发后：

```text
loop_status = replanning
replan_trigger = <reason>
回到 planner，基于现有 observations 和用户反馈生成新计划
```

**权衡原因**

数据库执行中出现新信息非常常见，强行继续旧计划风险高。

**效果**

如果预估影响行数过大，系统会停止执行并重新规划，而不是继续原计划。

### 6.9 设计点九：用户中途输入是一等事件

**Codex 的做法**

Codex 有 input queue / steering 机制，可以在 turn 中途吸收用户新输入或转向。

**Claude 的做法**

Claude 的 REPL 和远程会话支持 interrupt，用户可以中断长任务。

**最终方案**

mini-agent 增加 `user_feedback_gate`：

```text
每个高风险节点前检查是否有 user_interrupt/user_feedback
  -> 如果是约束补充，更新 DBTaskIntent 和 DBTaskPlan
  -> 如果是停止，loop_status=blocked/cancelled
  -> 如果是改目标，触发 replan
```

**权衡原因**

数据库任务经常需要用户补充环境、范围和执行限制。用户反馈不能只作为普通聊天消息。

**效果**

用户中途说“这是生产库，只允许只读”，系统会移除写步骤并重新规划。

### 6.10 设计点十：终止条件基于计划状态

**Codex 的做法**

Codex 会区分任务是否真正完成、是否还需要验证、是否有后续建议。

**Claude 的做法**

Claude 的 todo/task 完成状态决定任务是否结束，而不是只看模型当前有没有工具调用。

**最终方案**

Agent Loop 的结束条件改为：

```text
所有步骤 completed/skipped -> final_report
存在 pending approval -> waiting_for_approval
存在 blocked -> blocked_report
存在 failed 且可恢复 -> replan
存在 failed 且不可恢复 -> error_report
```

**权衡原因**

“本轮没有工具调用”不是数据库任务完成的可靠信号。

**效果**

Agent 不会在生成建议后误以为任务完成，如果计划里还有审批、执行或验证步骤。

### 6.11 设计点十一：数据库语义错误处理

**Codex 的做法**

Codex 有 error handler 和 retry 思路，工具失败后会分析原因并调整。

**Claude 的做法**

Claude 的工具执行失败可以回到模型或任务系统，修改后续策略。

**最终方案**

把错误归类为 `DBError`：

```text
syntax_error -> 让 LLM 修 SQL
permission_denied -> 请求用户授权或停止
lock_timeout -> 建议低峰执行或只报告风险
relation_not_found -> 回到 observe/clarify
unique_violation -> 回滚或重新规划
connection_error -> retry 或报告环境问题
```

**权衡原因**

数据库错误有明确语义，不能只作为普通字符串重试。

**效果**

权限不足不会无限 retry，锁等待不会被误判成 SQL 失败。

### 6.12 设计点十二：持久化每轮关键状态

**Codex 的做法**

Codex 使用 session/turn state 保存审批、用户输入、计划等异步状态。

**Claude 的做法**

Claude 使用 AppState/task state 保存任务、权限、通知、远程会话等状态。

**最终方案**

每轮持久化：

- `current_step_id`
- `tool_policy`
- `approval_decisions`
- `db_observations`
- `verification_results`
- `executed_sql`
- `affected_rows`
- `replan_trigger`

**权衡原因**

数据库任务需要恢复和审计，尤其是已经审批或执行过的 SQL。

**效果**

任务中断后可以恢复；事后可以追踪为什么执行了某条 SQL。

### 6.13 设计点十三：最终回答基于计划和证据

**Codex 的做法**

Codex 最终总结通常会说明做了什么、验证了什么、还有什么风险。

**Claude 的做法**

Claude 可以基于 todo/task 完成状态输出任务结果。

**最终方案**

增加 `final_report` 节点，读取：

- `DBTaskPlan`
- `DBObservation`
- `ApprovalDecision`
- `VerificationResult`

输出：

```text
做了什么
没有做什么
是否修改数据库
证据是什么
审批了什么
验证结果如何
剩余风险和下一步建议
```

**权衡原因**

数据库任务最终报告必须可审计，不能只靠 LLM 回忆聊天记录。

**效果**

用户能清楚知道：“本次只做了只读诊断，没有执行变更；发现慢点来自顺序扫描；建议创建复合索引，但尚未执行。”

## 7. 推荐 Graph 结构

目标结构：

```text
START
  -> intent_analyzer
  -> intent_validator
  -> clarification_gate
  -> workflow_planner
  -> task_planner
  -> step_scheduler
  -> llm_reason
  -> tool_policy_gate
  -> approval_gate
  -> execute_tools
  -> normalize_observation
  -> verify_step
  -> update_plan
  -> replan_gate
      -> task_planner
      -> step_scheduler
      -> final_report
      -> error_handler
```

第一阶段可以先落地轻量版本：

```text
task_planner
  -> step_scheduler
  -> llm_reason
  -> tool_policy_gate
  -> human_approval
  -> execute_tools
  -> verify_step
  -> update_plan
```

## 8. 路由规则

### 8.1 route_after_scheduler

```text
has running step -> llm_reason
all completed/skipped -> final_report
waiting approval -> approval_gate
blocked -> error_handler or replan_gate
```

### 8.2 route_after_policy_gate

```text
policy allowed and no approval needed -> execute_tools
policy allowed but approval needed -> approval_gate
policy rejected -> llm_reason or verify_step with blocked result
```

### 8.3 route_after_verify

```text
verification passed -> update_plan
verification failed but recoverable -> replan_gate
verification blocked -> final blocked report
```

### 8.4 route_after_replan_gate

```text
replan_trigger exists -> task_planner
more runnable steps -> step_scheduler
all done -> final_report
```

## 9. 实施计划

### 阶段一：步骤调度与计划状态驱动

1. 增加 `current_step_id` 和 `loop_status`。
2. 增加 `step_scheduler`。
3. `llm_reason` 只接收当前步骤上下文。
4. 终止条件改为读取 `DBTaskPlan.steps`。

验收标准：

- 当前步骤依赖未完成时不会执行。
- 所有步骤完成后才进入 final。

### 阶段二：工具策略门

1. 增加 `tool_policy_gate`。
2. 根据 `TaskStep.tool_policy` 拦截工具调用。
3. read_only 阶段拒绝写 SQL。

验收标准：

- `read_only_tools` 阶段不能执行 DDL/DML。
- `no_tools` 阶段不能调用工具。

### 阶段三：结构化观察和验证

1. 增加 `DBObservation`。
2. 增加 `normalize_observation`。
3. 增加 `verify_step`。
4. 验证结果写入 state。

验收标准：

- EXPLAIN 结果能被识别为 `explain_plan`。
- execute 后必须有 verify。

### 阶段四：审批和用户反馈闭环

1. 增加 `ApprovalDecision`。
2. approval payload 包含 SQL、影响、回滚、验证方式。
3. 增加 `user_feedback_gate`。

验收标准：

- 高风险 SQL 必须等待用户批准。
- 用户改口“只读”后触发重规划。

### 阶段五：重规划和最终报告

1. 增加 `replan_gate`。
2. 增加 `DBError` 分类。
3. 增加 `final_report`。

验收标准：

- 影响行数过大触发重规划。
- 最终回答基于计划、观察、审批和验证记录。

## 10. 最终取舍总结

Codex 值得借鉴的是：

- turn/session state。
- 计划更新。
- 审批状态。
- 用户中途输入。
- 执行前权限边界。

Claude 值得借鉴的是：

- todo/task 状态管理。
- permission context。
- 交互式 interrupt。
- 工具执行和 UI 状态联动。

mini-agent 的最终方案是：

```text
以 DBTaskPlan 为主线
  -> step_scheduler 选择当前步骤
  -> llm_reason 只围绕当前步骤推理
  -> tool_policy_gate 限制工具
  -> approval_gate 处理高风险
  -> execute_tools 执行
  -> normalize_observation 结构化结果
  -> verify_step 检查完成标准
  -> update_plan 推进状态
  -> replan_gate 处理异常
  -> final_report 基于证据总结
```

这个方案避免了两种极端：

- 不让 LLM 完全自由循环，避免数据库风险失控。
- 不把流程写死到无法应对复杂任务，保留重规划和用户反馈能力。

最终目标是让 mini-agent 成为一个能按计划、安全、可审计地管理 PostgreSQL 的 Agent，而不是一个只是能调用数据库工具的聊天机器人。

