# 项目 Wiki 百科

## 项目定位

MiniDB Agent 是一个 PostgreSQL 管理智能体，运行在终端中，面向数据库诊断、调优、变更规划和审计交付。

它的使用方式接近 Codex 和 Claude Code：用户在目标工作目录启动 CLI，输入自然语言任务，Agent 会在当前工作区和配置的数据库实例上完成分析，并实时展示正在执行的步骤、工具调用和结果摘要。

## 核心能力

- 连接 PostgreSQL 并展示安全的目标库信息。
- 自动识别数据库任务意图。
- 自动规划数据库任务步骤。
- 调用结构化 PostgreSQL 工具。
- 采集慢 SQL、表结构、索引、锁、健康状态。
- 生成 EXPLAIN 计划和索引建议。
- 对写操作、DDL、维护操作进行审批拦截。
- 记录 SQL hash、审批、验证、审计报告。
- 生成最终交付包。

## 主要模块

- CLI 模块：负责启动、配置、会话恢复、slash command、流式展示。
- 任务理解模块：把用户输入转成领域、意图、风险、目标库、缺失信息。
- 规划模块：把任务拆成 observe、diagnose、propose、approve、execute、verify、report 等步骤。
- Agent Loop：状态机驱动每一步执行，连接 LLM、工具、安全策略和验证节点。
- 工具模块：封装 PostgreSQL 操作，返回结构化结果。
- 安全模块：判断工具是否允许执行，写操作是否需要审批，SQL 是否匹配审批。
- 错误恢复模块：处理 SQL 错误、工具失败、模型 tool_call 协议错误、递归超限等问题。
- 交付模块：生成 final report、audit report、manifest 和 quality gate。

## 典型流程

用户输入：“数据库最需要优化的 SQL 是什么？”

Agent 会：

1. 识别为 PostgreSQL 性能诊断任务。
2. 使用配置里的数据库目标，不反复询问环境、版本、pg_stat_statements 是否存在。
3. 规划为慢 SQL 采集和报告两个步骤。
4. 调用 `postgres_top_queries`、`postgres_connection_check` 等工具。
5. 根据 `pg_stat_statements` 返回总耗时、平均耗时、IO、临时块。
6. 生成最终报告，列出最值得优化的 SQL 和证据。

## 安全边界

只读工具可以直接执行；写操作、DDL 和维护动作必须进入审批流程。审批时绑定 SQL hash、环境、影响说明和验证标准，避免“用户批准 A，实际执行 B”。

## 项目亮点

- 工具调用不是字符串拼接，而是结构化工具协议。
- 状态机不是简单 while loop，而是有步骤、验证、恢复和交付终态。
- CLI 不是打印内部日志，而是按 Codex 风格展示“正在做什么、调用了什么、结果是什么”。
- PostgreSQL 安全不是靠提示词，而是靠工具权限、SQL 分类、审批绑定和环境策略。
