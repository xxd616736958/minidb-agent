# PostgreSQL 管理智能体：具体工具实现模块设计

## 1. 背景

mini-agent 已经完成了以下基础能力：

```text
任务理解与意图建模
  -> 将用户请求转成粗粒度 DBTaskIntent

规划与任务分解
  -> 将数据库任务拆成 observe / diagnose / propose / approve / execute / verify / report

上下文管理
  -> 维护 StepContextPacket、DBWorkingSet、DBObservation、ResultDigest

记忆系统
  -> 保存用户偏好、安全约束、数据库事实和任务经验

工具注册与调用
  -> 用 ToolSpec、ToolCatalog、ToolCallPolicyGate 控制工具可见性、调用权限和审计
```

这些模块解决了“智能体什么时候能用工具、哪些工具能被模型看到、调用前如何审批”的问题，但还没有解决“PostgreSQL 管理智能体到底应该有哪些具体工具、每个工具内部如何安全实现、工具结果如何成为数据库领域证据”的问题。

本设计文档聚焦具体工具实现模块。

## 2. 模块定位

具体工具实现模块位于工具注册与调用模块之下，是 PostgreSQL 能力的真实执行层。

推荐关系：

```text
DBTaskIntent / DBTaskPlan / TaskStep
  -> ToolCatalog 动态选择工具
  -> ToolCallPolicyGate 调用前审批与拦截
  -> PostgreSQL 具体工具
       -> PostgresConnectionManager
       -> SafeSqlClassifier / SafeSqlDriver
       -> Domain Tool Implementation
  -> ToolExecutionResult
  -> DBObservation / ResultDigest / VerificationResult / MemoryCandidate
```

核心思想是：模型不直接操作数据库连接，也不直接拥有万能 SQL 执行权；它只能调用一组边界清晰、风险分级、结果结构化的 PostgreSQL 领域工具。

## 3. 设计目标

1. 用 PostgreSQL 领域工具替代万能 SQL 工具。
2. 将只读、诊断、模拟、写入、维护工具彻底分离。
3. 所有 SQL 执行前经过 AST 级别安全分类。
4. 所有只读工具在数据库层强制只读事务和超时。
5. 所有写工具必须绑定审批、SQL 摘要、影响说明和回滚方案。
6. 所有工具统一输出 `ToolExecutionResult`，再转成 `DBObservation`。
7. 工具结果默认截断、脱敏、摘要化，避免泄露和上下文膨胀。
8. 高级 DBA 能力先做可解释、可验证的轻量实现，再逐步引入复杂算法。
9. 支持未来接入 postgres-mcp 或其他 MCP 工具，但外部工具不能绕过 mini-agent 的策略门禁。

## 4. 非目标

1. 不把 mini-agent 直接改造成 MCP server。
2. 不直接照搬 postgres-mcp 的 unrestricted 任意 SQL 工具。
3. 不让 shell / psql 成为主要数据库访问方式。
4. 不在第一阶段实现完整数据库调优算法平台。
5. 不把原始大结果集、完整慢查询文本或连接凭据写入长期记忆。

## 5. 当前 mini-agent 的主要问题

### 5.1 缺少 PostgreSQL 专用工具

当前工具主要是文件、搜索、shell 等通用工具。对于数据库管理任务，Agent 缺少 schema 探查、EXPLAIN、慢 SQL、健康检查、索引建议、dry run 和受控写入工具。

### 5.2 SQL 安全判断仍偏粗糙

当前策略门禁已能用工具名和 SQL 正则阻断部分写 SQL，但数据库工具内部还没有 AST 级别 SQL 解析和强校验。

### 5.3 数据库连接与执行环境尚未统一

后续如果每个工具自行创建连接、处理超时、处理错误和脱敏，容易出现安全策略不一致、连接泄露和日志泄密。

### 5.4 工具结果还缺少领域结构

当前 `ToolMessage` 主要是文本。PostgreSQL 管理需要把 schema、执行计划、慢 SQL、健康检查、影响行数、错误码等变成稳定的领域对象。

## 6. 推荐工具体系

第一阶段推荐实现以下工具：

```text
连接与安全基础层：
  postgres_connection_check
  postgres_sql_classify

结构观察工具：
  postgres_list_schemas
  postgres_list_objects
  postgres_object_detail

只读查询工具：
  postgres_query_readonly

诊断工具：
  postgres_explain
  postgres_top_queries
  postgres_health_check
  postgres_lock_inspect

索引建议工具：
  postgres_index_advisor
  postgres_hypothetical_index_test

写入前验证工具：
  postgres_dry_run

写入工具：
  postgres_execute_write

维护工具：
  postgres_analyze_table
  postgres_vacuum_table
  postgres_create_index_concurrently
```

第一阶段不要求所有工具一次性完成，但工具接口和安全边界应按这个体系设计，避免后续返工。

## 7. 核心数据结构

### 7.1 PostgreSQLToolResult

```python
class PostgreSQLToolResult(TypedDict):
    tool_name: str
    success: bool
    result_type: Literal[
        "connection_status",
        "sql_classification",
        "schema_summary",
        "object_detail",
        "query_result",
        "explain_plan",
        "top_queries",
        "health_report",
        "lock_report",
        "index_advice",
        "dry_run_report",
        "write_result",
        "maintenance_result",
        "sql_error",
        "tool_error",
    ]
    summary: str
    payload: dict[str, Any]
    row_count: Optional[int]
    affected_rows: Optional[int]
    sqlstate: Optional[str]
    duration_ms: int
    truncated: bool
    sensitive_fields_masked: list[str]
```

### 7.2 SQLClassification

```python
class SQLClassification(TypedDict):
    normalized_sql_hash: str
    statement_count: int
    primary_type: Literal[
        "read_only",
        "explain",
        "diagnostic",
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
        "unknown",
    ]
    risk_level: Literal["low", "medium", "high", "critical"]
    read_only: bool
    destructive: bool
    requires_approval: bool
    requires_transaction: bool
    detected_operations: list[str]
    blocked_reasons: list[str]
    warnings: list[str]
```

### 7.3 ExplainPlanObservation

```python
class ExplainPlanObservation(TypedDict):
    plan_hash: str
    root_node_type: str
    total_cost: float
    plan_rows: int
    planning_time_ms: Optional[float]
    execution_time_ms: Optional[float]
    scan_types: list[str]
    join_types: list[str]
    relation_names: list[str]
    has_seq_scan: bool
    has_index_scan: bool
    warnings: list[str]
    raw_plan_ref: Optional[str]
```

### 7.4 IndexAdvice

```python
class IndexAdvice(TypedDict):
    table: str
    columns: list[str]
    index_method: str
    create_sql: str
    estimated_size_bytes: Optional[int]
    before_cost: Optional[float]
    after_cost: Optional[float]
    improvement_ratio: Optional[float]
    evidence: list[str]
    warnings: list[str]
    requires_approval_to_apply: bool
```

## 8. 设计点与参考权衡

### 8.1 设计点一：用 PostgreSQL 领域工具替代万能 SQL 工具

**Codex 的设计方案**

Codex 的工具体系强调工具由统一 runtime 编排，模型通过工具调用表达动作，真正执行时由工具 handler、审批、sandbox 和事件流负责处理。Codex 可以使用 shell 这类通用工具，但高风险操作会进入审批和受控执行路径。

**Claude 的设计方案**

Claude 的工具设计强调细粒度能力边界，例如 Read、Write、Edit、Bash、WebFetch 等能力分开，每个工具有自己的只读、破坏性、权限检查和展示逻辑。模型不是拿一个万能能力做所有事，而是在不同工具之间选择。

**postgres-mcp 的设计方案**

postgres-mcp 没有只暴露一个数据库连接包装器，而是提供 `list_schemas`、`list_objects`、`get_object_details`、`explain_query`、`analyze_db_health`、`get_top_queries`、`analyze_query_indexes` 等领域工具。同时它也提供 `execute_sql`，但会根据 access mode 改变权限。

**最终权衡方案**

mini-agent 不采用一个万能 `postgres_execute(sql)` 作为主要工具，而是实现一组 PostgreSQL 领域工具。自由 SQL 只保留为受控的 `postgres_query_readonly` 和 `postgres_execute_write`，并且二者工具名、元数据、权限和审批完全分离。

**达成效果**

模型更容易选择正确工具，系统更容易做权限控制和审计。观察结构、分析慢 SQL、生成索引建议、执行写入会走不同路径，避免把所有风险压到一个 SQL 字符串上。

### 8.2 设计点二：统一 PostgreSQL 连接管理与执行环境

**Codex 的设计方案**

Codex 将工具执行放入统一 runtime，执行环境、权限、工作区、sandbox 和事件记录由系统管理，而不是让每个工具自由决定如何执行。

**Claude 的设计方案**

Claude 的工具执行依赖 permission context 和工具自身的 `checkPermissions` / `validateInput`。工具可以有自己的执行逻辑，但权限和用户可见提示由统一机制串起来。

**postgres-mcp 的设计方案**

postgres-mcp 有 `DbConnPool` 和 `SqlDriver`，统一管理连接池、连接测试、连接关闭、错误处理和密码脱敏。数据库 URL 可来自环境变量或命令行，连接失败时会脱敏错误信息。

**最终权衡方案**

mini-agent 增加 `PostgresConnectionManager` 和 `PostgresDriver`。所有 PostgreSQL 工具必须通过它获取连接，统一设置连接池大小、statement timeout、lock timeout、只读事务、错误归一化、连接串脱敏和执行耗时统计。

**达成效果**

每个工具不再重复处理连接和错误，数据库凭据不会散落在日志、模型上下文和记忆里。连接状态也可以作为 `postgres_connection_check` 的结果进入 Agent 诊断流程。

### 8.3 设计点三：SQL 安全分类必须基于 AST，而不是正则

**Codex 的设计方案**

Codex 对 shell 命令和外部执行动作会进行风险判断、审批和 sandbox 选择。它不会简单相信模型生成的命令是安全的，而是在执行前进行系统侧控制。

**Claude 的设计方案**

Claude 工具通常通过 input schema、`validateInput` 和 `checkPermissions` 做调用前校验。工具本身必须能拒绝不合法输入，不能完全依赖 prompt。

**postgres-mcp 的设计方案**

postgres-mcp 的 `SafeSqlDriver` 使用 `pglast` 解析 SQL，检查允许的 statement type、AST node type、函数调用、locking clause、EXPLAIN ANALYZE、扩展创建等。它比关键词正则更可靠。

**最终权衡方案**

mini-agent 实现 `PostgresSqlClassifier`，优先使用 PostgreSQL AST parser 解析 SQL，输出 `SQLClassification`。策略门禁可以继续做第一层粗拦截，但具体数据库工具执行前必须用 classifier 做硬校验。

**达成效果**

安全判断从“模型是否遵守提示词”升级为“执行前系统强制解析”。多语句、伪装注释、函数副作用、锁语句、危险维护语句都可以被更准确地识别。

### 8.4 设计点四：只读查询工具必须双重只读

**Codex 的设计方案**

Codex 的 sandbox 思想是将潜在危险动作放入受控执行环境，并通过权限控制限制它能影响的范围。

**Claude 的设计方案**

Claude 的 read-only 工具边界清晰。只读工具必须不会改变外部状态，否则模型和用户都无法正确理解工具风险。

**postgres-mcp 的设计方案**

postgres-mcp 在 restricted mode 下使用 `SafeSqlDriver`，并且在底层执行时强制 `BEGIN TRANSACTION READ ONLY`，执行结束 rollback。即使调用者传入 `force_readonly=False`，SafeSqlDriver 也会覆盖为只读。

**最终权衡方案**

`postgres_query_readonly` 必须先通过 AST 校验，再通过数据库层 `BEGIN TRANSACTION READ ONLY` 执行，并设置 `statement_timeout`、最大返回行数和最大结果大小。只读工具不得执行 `EXPLAIN ANALYZE`、`VACUUM`、`ANALYZE`、`CREATE EXTENSION` 等可能影响数据库状态或负载的语句。

**达成效果**

即使模型生成了伪装成查询的危险 SQL，也会被 AST 和数据库只读事务双重拦截。用户可以放心让 Agent 在 observe / diagnose 阶段读取数据和元信息。

### 8.5 设计点五：Schema 与对象探查要封装成专用工具

**Codex 的设计方案**

Codex 做代码任务时倾向先读取文件、搜索代码、观察环境，再做修改。这个模式可以迁移到数据库任务：先观察 schema 和对象，再生成诊断或变更方案。

**Claude 的设计方案**

Claude 的 Read 类工具强调稳定读取边界，模型不需要自己构造复杂读取动作。读取结果也会被控制展示。

**postgres-mcp 的设计方案**

postgres-mcp 提供 `list_schemas`、`list_objects`、`get_object_details`，将 schema、table、view、sequence、extension、columns、constraints、indexes 等查询封装成工具。

**最终权衡方案**

mini-agent 实现 `postgres_list_schemas`、`postgres_list_objects`、`postgres_object_detail`。这些工具只接受 schema name、object name、object type 等结构化参数，内部使用参数化查询或安全 identifier builder，不让模型自己拼 information_schema SQL。

**达成效果**

数据库结构探查稳定、低风险、可缓存，可以直接更新 `DBWorkingSet`。后续生成 SQL、解释查询、建议索引时有可靠的结构上下文。

### 8.6 设计点六：EXPLAIN 工具默认安全，ANALYZE 单独升级风险

**Codex 的设计方案**

Codex 对高风险执行动作会要求明确审批或用户确认。即使一个工具整体可用，具体参数也可能改变风险等级。

**Claude 的设计方案**

Claude 工具的 `checkPermissions` 可以基于输入判断是否需要权限。例如同一类工具在不同参数下可能是只读，也可能是破坏性。

**postgres-mcp 的设计方案**

postgres-mcp 的 `explain_query` 支持普通 EXPLAIN、`EXPLAIN ANALYZE` 和 hypothetical indexes。其 ExplainPlanTool 还会处理 bind variables，并在 PostgreSQL 版本支持时使用 generic plan。

**最终权衡方案**

mini-agent 实现 `postgres_explain`。默认只执行 `EXPLAIN (FORMAT JSON)`。如果参数 `analyze=True`，工具风险从 low/medium 升级为 high，因为它会真实执行查询；生产环境必须要求用户确认或审批。`hypothetical_indexes` 不能和 `analyze=True` 同时使用。

**达成效果**

Agent 可以安全读取执行计划，并在确有必要时用受控方式获取真实执行指标。慢查询优化流程更接近 DBA 实践，而不是只凭 SQL 文本猜。

### 8.7 设计点七：执行计划必须转成结构化 Artifact

**Codex 的设计方案**

Codex 的工具执行结果会进入事件流和状态，后续节点可以基于这些结果继续推理、验证和汇报。

**Claude 的设计方案**

Claude 工具通常会控制输出大小，并将工具结果以用户和模型都能理解的方式展示。长结果不能无限注入上下文。

**postgres-mcp 的设计方案**

postgres-mcp 的 `ExplainPlanArtifact` 将 JSON plan 转成 `PlanNode` 树，提取 cost、rows、actual time、buffer、relation、filter，并支持 readable text 和 plan diff。

**最终权衡方案**

mini-agent 实现 `ExplainPlanObservation`。工具保留原始 JSON 的引用或摘要，但注入上下文的是结构化摘要：root node、scan types、join types、relations、cost、rows、是否 seq scan、是否 index scan、关键 warning。

**达成效果**

执行计划可以被验证模块、索引建议模块、报告模块和记忆系统复用。系统不需要反复让模型从大段 EXPLAIN 文本里猜关键事实。

### 8.8 设计点八：慢 SQL 工具以真实 workload 为入口

**Codex 的设计方案**

Codex 倾向用工具收集真实环境证据，例如读文件、跑测试、查看输出，而不是只基于用户描述行动。

**Claude 的设计方案**

Claude 的工具参数通常通过 schema 限制，例如排序方式、limit、过滤条件等，让模型在明确选项中选择。

**postgres-mcp 的设计方案**

postgres-mcp 的 `get_top_queries` 使用 `pg_stat_statements`，支持按 resources、mean_time、total_time 排序，并限制返回条数。

**最终权衡方案**

mini-agent 实现 `postgres_top_queries`。输入包括 sort_by、limit、min_calls、min_mean_time_ms，可选 schema/database filter。输出脱敏后的 query 摘要、queryid、calls、mean time、total time、rows、block 读写、temp blocks 等。

**达成效果**

性能优化可以从真实 workload 出发。Agent 能先定位最值得优化的 SQL，再进入 EXPLAIN 和索引建议，而不是只分析用户随手贴的一条语句。

### 8.9 设计点九：数据库健康检查按 DBA 维度组织

**Codex 的设计方案**

Codex 会把复杂任务拆成可验证步骤，每一步用工具收集证据，然后形成结论。

**Claude 的设计方案**

Claude 擅长用枚举参数和明确描述控制工具使用，避免模型传入任意自然语言导致工具行为不稳定。

**postgres-mcp 的设计方案**

postgres-mcp 的 `analyze_db_health` 支持 index、connection、vacuum、sequence、replication、buffer、constraint、all 等健康检查维度。

**最终权衡方案**

mini-agent 实现 `postgres_health_check`，复用这些健康维度作为枚举参数，并将每个维度输出为 `health_report` observation。每项报告包含 status、severity、evidence、recommendation 和 follow_up_tools。

**达成效果**

用户问“数据库是否健康”时，系统能给出分项证据，而不是泛泛回答。后续规划模块也可以根据健康检查结果自动生成诊断步骤。

### 8.10 设计点十：锁与连接诊断独立成工具

**Codex 的设计方案**

Codex 对运行中环境的观察会作为后续动作依据，尤其是命令输出、失败原因和进程状态。

**Claude 的设计方案**

Claude 工具边界清楚，适合把一个高频诊断场景做成单独工具，而不是让模型临时拼复杂命令。

**postgres-mcp 的设计方案**

postgres-mcp 的 health check 包含 connection 维度，但没有将锁等待、阻塞链、长事务作为非常细的独立工具暴露出来。

**最终权衡方案**

mini-agent 在 postgres-mcp 的 connection health 基础上进一步拆出 `postgres_lock_inspect`。该工具只读查询 `pg_locks`、`pg_stat_activity`、blocking pid、等待事件、长事务和 idle in transaction，输出阻塞链摘要。

**达成效果**

用户问“为什么卡住”“谁阻塞了谁”“连接为什么满了”时，Agent 可以直接给出结构化诊断，而不是让模型生成复杂且容易错的系统表 SQL。

### 8.11 设计点十一：索引建议先做证据链，后做复杂算法

**Codex 的设计方案**

Codex 强调先观察、再提出修改、最后验证。对于性能优化，也应该先拿证据，再提出变更。

**Claude 的设计方案**

Claude 的工具能力边界提醒我们：建议工具和执行工具应该分开。给出索引建议不等于创建索引。

**postgres-mcp 的设计方案**

postgres-mcp 的 Database Tuning Advisor 会基于 workload 生成候选索引、估算索引大小、用 HypoPG 比较执行计划成本，并按预算和收益筛选推荐。

**最终权衡方案**

mini-agent 第一阶段实现轻量 `postgres_index_advisor`：输入 SQL 或 top queries，结合 schema、已有索引、EXPLAIN、HypoPG 单项模拟，输出候选索引、收益证据、预计大小、风险和 `CREATE INDEX CONCURRENTLY` 建议。暂不照搬完整 DTA 贪心搜索和 LLM optimizer。

**达成效果**

索引建议有证据、有成本、有验证路径，同时实现复杂度可控。后续可以逐步引入 postgres-mcp 的完整候选生成和预算优化算法。

### 8.12 设计点十二：HypoPG 模拟作为写入前的低风险验证

**Codex 的设计方案**

Codex 的 sandbox 思路是：高风险动作先在受控环境里验证，减少直接影响真实工作区或系统的概率。

**Claude 的设计方案**

Claude 对 destructive 工具有明确标记和权限检查；一个工具是否安全取决于它真实改变了什么外部状态。

**postgres-mcp 的设计方案**

postgres-mcp 支持 hypothetical indexes，使用 HypoPG 模拟索引，再比较 EXPLAIN plan，而不真实创建索引。

**最终权衡方案**

mini-agent 实现 `postgres_hypothetical_index_test`。它只接受结构化 index definition，不接受任意 SQL 片段；内部创建虚拟索引、执行 EXPLAIN、生成 before/after plan diff，最后 reset HypoPG 状态。

**达成效果**

Agent 可以先证明某个索引可能有效，再请求用户审批真实创建索引。这样把“建议”与“执行变更”之间增加了可靠证据层。

### 8.13 设计点十三：dry run 是写入工具的前置协议

**Codex 的设计方案**

Codex 对高风险操作会尽量通过审批、sandbox、失败回退和结果验证降低风险。

**Claude 的设计方案**

Claude 的 `checkPermissions` 可以在工具执行前根据具体输入要求用户确认，工具也可以拒绝缺失必要上下文的请求。

**postgres-mcp 的设计方案**

postgres-mcp 的 restricted mode 强调只读安全，但 unrestricted 任意 SQL 不提供 mini-agent 所需的审批绑定和回滚协议。

**最终权衡方案**

mini-agent 实现 `postgres_dry_run`。对于 DML，先在事务中执行影响范围估算或受控执行后 rollback；对于 DDL 和维护操作，能预演就预演，不能安全预演就返回原因、风险和替代检查。dry run 结果必须包含 SQL hash、预估影响行数、锁风险、失败风险、回滚建议。

**达成效果**

写入前用户能看到具体影响，而不是只看到模型的一句“我准备执行”。审批有真实证据作为依据。

### 8.14 设计点十四：写 SQL 执行工具必须绑定审批对象

**Codex 的设计方案**

Codex 的高风险工具执行会进入 approval flow，审批绑定具体工具请求和上下文，不应被无限复用。

**Claude 的设计方案**

Claude 的 destructive 工具会通过权限上下文、工具名、输入和规则来源向用户展示风险。权限判断发生在具体调用上。

**postgres-mcp 的设计方案**

postgres-mcp 在 unrestricted mode 提供 `execute_sql`，并用 `destructiveHint=True` 标记风险。但它不负责 mini-agent 内部的任务步骤审批、SQL hash 绑定和验证闭环。

**最终权衡方案**

mini-agent 实现 `postgres_execute_write`，要求传入 `approval_id`、SQL hash、target_environment、impact_summary、rollback_summary、expected_affected_rows。工具执行前重新计算 SQL hash，并确认审批对象未过期、未被复用于其他 SQL、环境一致、步骤一致。

**达成效果**

用户批准的是某个具体 SQL、具体环境、具体影响说明，而不是笼统批准“可以写库”。审计记录可以明确追溯审批与执行的对应关系。

### 8.15 设计点十五：维护工具必须细粒度拆分

**Codex 的设计方案**

Codex 对命令执行风险敏感，越接近系统级副作用的动作越需要明确审批和可观察结果。

**Claude 的设计方案**

Claude 的工具拆分方式说明，不同副作用应由不同工具承载，例如 Write 和 Bash 不混在一起。

**postgres-mcp 的设计方案**

postgres-mcp 的 SafeSqlDriver 在某些场景允许 ANALYZE、VACUUM、CREATE EXTENSION hypopg 等读写边界比较特殊的操作，这是为了诊断能力做出的权衡。

**最终权衡方案**

mini-agent 不把 ANALYZE、VACUUM、REINDEX、CREATE INDEX、CREATE EXTENSION 混在普通查询或万能维护 SQL 里，而是拆成 `postgres_analyze_table`、`postgres_vacuum_table`、`postgres_create_index_concurrently`、`postgres_extension_check` 等工具。每个工具声明自己的风险和审批要求。

**达成效果**

维护操作的权限、风险、锁影响、超时和验证标准都可单独控制。生产环境不会因为“只读诊断”误执行维护操作。

### 8.16 设计点十六：工具结果必须统一脱敏、截断和摘要化

**Codex 的设计方案**

Codex 的工具输出会进入上下文和历史记录，因此需要控制输出体积，并让后续节点能基于摘要继续工作。

**Claude 的设计方案**

Claude 工具常包含最大输出限制、结果渲染和敏感信息处理。工具输出不是无限原样给模型。

**postgres-mcp 的设计方案**

postgres-mcp 有 `obfuscate_password`，会隐藏连接 URL 和错误信息里的密码。但大部分工具仍以文本内容返回，结构化和字段级脱敏不是它的主要关注点。

**最终权衡方案**

mini-agent 在所有 PostgreSQL 工具中统一使用 `ResultLimiter` 和 `SensitiveDataMasker`。默认限制行数、列宽、payload 大小，屏蔽 password、token、secret、email、phone、credential、connection string 等敏感字段。大结果只进入 `ResultDigest`。

**达成效果**

真实数据库数据不会轻易泄露进模型上下文、日志和长期记忆。工具结果也不会因为过大导致上下文压缩频繁触发。

### 8.17 设计点十七：工具输出统一转成 ToolExecutionResult 和 DBObservation

**Codex 的设计方案**

Codex 的工具调用会形成可追踪事件，后续推理、用户展示、恢复和调试都依赖这些结构化轨迹。

**Claude 的设计方案**

Claude 的工具结果既要给模型继续推理，也要给用户理解，因此工具需要稳定的展示和结果映射机制。

**postgres-mcp 的设计方案**

postgres-mcp 有 `ErrorResult`、`ExplainPlanArtifact`、`IndexTuningResult` 等领域对象，但 MCP 工具最终多返回 text content。

**最终权衡方案**

mini-agent 所有 PostgreSQL 工具内部可以有领域对象，但统一出口必须是 `PostgreSQLToolResult`，再映射为现有 `ToolExecutionResult` 和 `DBObservation`。文本只是展示层，不作为系统内部唯一事实来源。

**达成效果**

验证模块可以检查 `affected_rows`，报告模块可以引用 `health_report`，记忆系统可以保存 schema summary，Agent Loop 可以基于结构化 observation 继续执行。

### 8.18 设计点十八：外部 MCP 工具可接入，但不能绕过本地策略

**Codex 的设计方案**

Codex 对 MCP 工具会做 exposure 控制和调用编排，不是发现 MCP 工具后无条件暴露给模型。

**Claude 的设计方案**

Claude 的 MCP 工具也会进入 permission context、deny rules、tool pool assembly 和权限展示流程。

**postgres-mcp 的设计方案**

postgres-mcp 本身是 MCP server，提供工具注解和数据库能力，但它不知道 mini-agent 当前的任务步骤、长期安全记忆、审批对象和上下文预算。

**最终权衡方案**

mini-agent 可以把 postgres-mcp 作为外部能力来源，但必须用 `MCPToolAdapter` 将 MCP tool annotations 映射为本地 `ToolSpec`，并经过 ToolCatalog、ToolCallPolicyGate、Approval、ToolInvocationRecord 和结果归一化。高风险 MCP 工具默认不自动暴露。

**达成效果**

mini-agent 能复用 postgres-mcp 的成熟 PostgreSQL 能力，同时保持自身的任务规划、安全护栏、审批和审计体系不被绕过。

### 8.19 设计点十九：工具调用要服务于 DBA 工作流，而不是孤立函数

**Codex 的设计方案**

Codex 的 Agent Loop 会围绕任务目标持续执行：观察、行动、读取结果、修正计划、验证。

**Claude 的设计方案**

Claude 工具使用受上下文、权限和用户交互影响。工具不是越多越好，而是应该在合适阶段出现。

**postgres-mcp 的设计方案**

postgres-mcp 的工具集合天然形成 DBA 流程：看 schema、看 top queries、explain、模拟索引、健康检查、给建议。

**最终权衡方案**

mini-agent 在规划模块中内置常见工具链：

```text
性能优化：
  postgres_top_queries
  -> postgres_object_detail
  -> postgres_explain
  -> postgres_hypothetical_index_test
  -> postgres_index_advisor
  -> postgres_dry_run
  -> approval
  -> postgres_execute_write
  -> postgres_explain / postgres_top_queries verify

健康检查：
  postgres_health_check
  -> postgres_lock_inspect / postgres_object_detail
  -> propose fixes
  -> approval if write or maintenance

数据排查：
  postgres_list_schemas
  -> postgres_list_objects
  -> postgres_object_detail
  -> postgres_query_readonly
  -> report
```

**达成效果**

Agent 更像一个按流程工作的数据库工程师，而不是每轮临场决定调用哪个函数。工具实现和规划模块能相互配合，减少错误路径。

## 9. 第一阶段落地顺序

### 9.1 基础层

1. `PostgresConnectionManager`
2. `PostgresDriver`
3. `PostgresSqlClassifier`
4. `ResultLimiter`
5. `SensitiveDataMasker`

### 9.2 低风险工具

1. `postgres_connection_check`
2. `postgres_list_schemas`
3. `postgres_list_objects`
4. `postgres_object_detail`
5. `postgres_query_readonly`
6. `postgres_explain`，先不支持 analyze

### 9.3 诊断工具

1. `postgres_top_queries`
2. `postgres_health_check`
3. `postgres_lock_inspect`

### 9.4 优化建议工具

1. `postgres_hypothetical_index_test`
2. `postgres_index_advisor`

### 9.5 高风险工具

1. `postgres_dry_run`
2. `postgres_execute_write`
3. `postgres_analyze_table`
4. `postgres_vacuum_table`
5. `postgres_create_index_concurrently`

## 10. 验收标准

1. 所有 PostgreSQL 工具都有 `ToolSpec` 元数据。
2. 只读步骤只暴露只读工具。
3. `postgres_query_readonly` 对写 SQL、多语句、危险函数、锁语句返回拒绝。
4. 只读工具在数据库层强制 read-only transaction。
5. `postgres_explain` 默认不执行 ANALYZE。
6. `postgres_execute_write` 没有有效审批时不能执行。
7. 工具结果统一包含耗时、row_count、affected_rows、sqlstate、truncated、sensitive_fields_masked。
8. 大结果会截断并生成 `ResultDigest`。
9. 连接串和错误信息中的密码不会进入日志、上下文或记忆。
10. 每个工具调用都有 `ToolInvocationRecord`。
11. 工具结果能被 `normalize_observation` 转成 `DBObservation`。

## 11. 关键取舍总结

1. 采用 Codex 的统一工具执行生命周期，但不采用通用 shell 作为数据库主要接口。
2. 采用 Claude 的细粒度工具和权限语义，但不把权限完全放在工具描述里，而是接入 mini-agent 的 TaskStep、ToolPolicy 和 Approval。
3. 采用 postgres-mcp 的 PostgreSQL 领域工具、安全 SQL、EXPLAIN、健康检查和索引建议思想，但不照搬 unrestricted 任意 SQL 工具，也不在第一阶段照搬完整 DTA 算法。
4. mini-agent 的最终方向是“受控 DBA 工具箱”，不是“能执行 SQL 的聊天机器人”。
