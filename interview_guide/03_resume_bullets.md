# 简历写法

## 一句话项目描述

MiniDB Agent：基于 LangGraph 和 PostgreSQL 工具体系实现的数据库运维智能体，支持自然语言诊断慢 SQL、分析表结构和索引、执行安全审批、生成审计报告。

## 简历项目经历写法

项目：MiniDB Agent - PostgreSQL 数据库管理智能体

- 设计并实现面向 PostgreSQL 运维场景的终端智能体，支持慢 SQL 诊断、表结构分析、索引建议、锁检查、健康检查和维护操作规划。
- 参考 Codex/Claude Code 的 Agent Loop 和 CLI 协作模式，实现任务理解、计划生成、步骤调度、工具调用、结果验证、错误恢复和交付报告的完整闭环。
- 设计 PostgreSQL 工具注册与调用体系，将连接检查、`pg_stat_statements` 分析、EXPLAIN、索引建议、ANALYZE、并发建索引等操作封装为结构化工具。
- 实现数据库安全护栏：按环境区分只读/写入权限，对 DDL、DML、维护操作进行风险分类，写操作必须人工审批并绑定 SQL hash。
- 优化 CLI 体验，支持目标目录一键启动、自动拉起本地服务、数据库连接配置持久化、slash command、会话恢复和流式工具结果展示。
- 构建错误处理与自我修复机制，解决模型 tool_call 协议异常、SQL 语义错误、工具失败、递归超限和交付状态不一致等问题。
- 使用真实 PostgreSQL 实例完成端到端验证，成功发现 `public.big_orders_demo` 慢 SQL，输出耗时、平均耗时、IO、临时块、优化建议和审计报告。

## 简历技术栈

Python、LangGraph、LangChain、PostgreSQL、psycopg、Rich、Pydantic、pytest、Agent Loop、Tool Calling、Human-in-the-loop、SQL Safety、CLI Engineering

## 可量化表达

- 设计 20+ 个 PostgreSQL 领域工具。
- 覆盖 140+ 个单元/集成测试。
- 支持从自然语言到诊断报告的端到端数据库调优流程。
- 将写操作从“模型直接执行”改为“审批绑定 SQL hash 后执行”。

## 面试时不要这么说

不要只说“我接了一个大模型 API”。这个项目的重点不是 API，而是工程化智能体系统：状态机、工具边界、安全策略、错误恢复、交互体验和交付质量。
