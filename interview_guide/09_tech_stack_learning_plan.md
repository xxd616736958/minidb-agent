# 技术栈学习路线

## 第一阶段：PostgreSQL 基础

必须掌握：

- 表、索引、视图、事务。
- EXPLAIN 和执行计划。
- `pg_stat_statements`。
- `pg_stat_activity`、锁和等待事件。
- VACUUM、ANALYZE、统计信息。
- 常见索引：btree、GIN、BRIN。

学习目标：能解释为什么一个 SQL 慢，以及应该看哪些证据。

## 第二阶段：Python 工程

必须掌握：

- Pydantic 数据模型。
- psycopg 连接 PostgreSQL。
- pytest 测试。
- CLI 参数解析。
- Rich 终端展示。
- 日志和异常处理。

学习目标：能写稳定的工具层，而不是临时脚本。

## 第三阶段：Agent 基础

必须掌握：

- Tool Calling。
- Agent Loop。
- 状态机。
- 上下文管理。
- Human-in-the-loop。
- 安全护栏。

学习目标：理解智能体不是“while 调模型”，而是“模型 + 工具 + 状态 + 权限 + 终止条件”。

## 第四阶段：LangGraph

必须掌握：

- State。
- Node。
- Edge。
- Conditional routing。
- checkpoint/thread。
- stream updates。
- recursion limit。

学习目标：能画出 Agent 从用户输入到最终报告的状态流转。

## 第五阶段：Codex/Claude Code 设计思想

重点学习：

- 在当前工作区启动。
- 默认新会话，显式 resume。
- 工具调用过程可见。
- 权限请求明确。
- 错误要可见、可恢复、可终止。
- CLI 输出要简洁，verbose 才显示内部细节。

学习目标：能说明为什么好的 Agent 产品不是“多打印日志”，而是“展示用户关心的执行过程”。

## 推荐学习节奏

第一周：PostgreSQL 慢 SQL 和 EXPLAIN。

第二周：Python + psycopg + pytest，把数据库操作封装成工具。

第三周：LangGraph 状态机，把工具串成 Agent Loop。

第四周：安全审批、CLI 体验、错误恢复和报告交付。

## 面试前必须能手写/讲清

- 一个 Tool 的输入输出结构。
- 一个 Agent Loop 的节点顺序。
- SQL 写操作审批如何绑定 hash。
- 为什么 EXPLAIN ANALYZE 不是普通只读。
- 为什么 report 阶段不能继续无限调用工具。
- GraphRecursionError 怎么定位。
