# 设计如何确定

## 总体架构

MiniDB Agent 采用“CLI + 本地服务 + LangGraph 状态机 + PostgreSQL 工具 + 安全策略 + 交付系统”的架构。

用户通过 CLI 输入任务；CLI 自动启动本地服务并创建新会话；LangGraph 负责任务编排；LLM 负责理解、规划和选择工具；工具层负责真实数据库操作；安全层负责审批和权限控制；交付层负责报告和审计。

## 为什么不用简单 while loop

数据库任务不是一次问答。它通常需要观察、诊断、提案、审批、执行、验证和报告。如果用简单 while loop，很难精确控制每一步的权限、证据和终止条件。

因此项目采用状态机：每个节点只负责一件事，比如 `intent_analyzer` 只理解任务，`task_planner` 只生成步骤，`tool_policy_gate` 只做安全判断，`execute_tools` 只执行工具，`verify_step` 只验证当前步骤。

## 任务理解设计

任务理解模块不枚举死所有意图，而是抽取结构化字段：领域、主要意图、候选意图、目标环境、目标数据库、风险等级、是否需要审批、缺失信息和下一步动作。

这样做的好处是灵活：新增数据库场景时，不一定要增加一个硬编码意图，只要工具、提示词和规划规则能处理即可。

## 规划设计

规划采用“LLM 规划 + 确定性兜底”。LLM 能处理复杂自然语言，确定性规则处理常见数据库任务，比如列出表、慢 SQL 诊断、只读分析、写操作审批流程。

这样既有灵活性，也避免模型在简单任务上反复澄清或规划错误。

## 工具设计

PostgreSQL 工具统一返回结构化结果，包括 success、result_type、summary、payload、row_count、sqlstate、duration_ms 等字段。

这样 Agent 不需要从自由文本里猜工具结果，CLI 和报告也能稳定展示。

## 安全设计

安全不是靠一句系统提示，而是多层控制：

- 工具元数据标记 read_only、destructive、requires_approval。
- SQL 分类判断 DDL、DML、维护、事务控制。
- 环境策略限制 production/unknown 写操作。
- 写操作必须有审批记录。
- 审批绑定 SQL hash，避免审批漂移。

## 错误恢复设计

错误恢复参考 Codex/Claude 的思路：工具失败必须可见，能修就修，不能修就明确停止。

项目里处理了几类问题：

- SQL 语义错误：给模型一次修复机会，重复失败就阻塞。
- report 阶段误调用工具：降级为基于已有证据写报告。
- provider tool_call 协议错误：清理不合法消息顺序。
- GraphRecursionError：提高递归上限并检测 run error，避免静默失败。

## 交付设计

最终交付不是简单回答，而是 final report、audit report、manifest、quality gate。报告里包含主要发现、证据、SQL 交付项、风险、验证和下一步。
