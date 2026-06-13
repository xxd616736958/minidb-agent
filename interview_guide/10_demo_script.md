# 项目演示脚本

## 演示目标

向面试官展示 MiniDB Agent 不是静态 Demo，而是能连接真实 PostgreSQL、自动诊断慢 SQL、生成报告，并有安全边界的数据库智能体。

## 演示准备

确认 PostgreSQL 可用，并设置连接串：

```bash
export POSTGRES_TARGET_URL="postgresql://user:password@127.0.0.1:5432/db_agent"
```

启动 CLI：

```bash
minidb-agent --workspace "$PWD" --database-url "$POSTGRES_TARGET_URL" --target-env dev
```

## 演示一：自我介绍

输入：

```text
你是谁，可以做什么？
```

预期说明：

- 它应该介绍自己是 PostgreSQL 管理智能体。
- 不应该说自己是通用编程助手。
- 不应该展示无关能力，比如天气。

## 演示二：慢 SQL 诊断

输入：

```text
数据库最需要优化的 SQL 是什么？不要问我 pg_stat_statements 是否存在，也不要问排序方式，自己判断。
```

预期说明：

- Agent 使用当前配置的数据库。
- 自动检查连接。
- 自动调用 PostgreSQL 慢 SQL 工具。
- 输出最慢 SQL、总耗时、平均耗时、IO、临时块和建议。

## 演示三：只读调优报告

非交互验证：

```bash
minidb-agent exec --json \
  "只读完成 public.big_orders_demo 的调优诊断：自己发现最值得优化的 SQL，使用 EXPLAIN 或索引建议验证方案，生成明确报告。不要执行写操作。"
```

预期说明：

- 状态为 completed。
- delivery package 为 ready。
- final report 包含主要发现和证据。

## 演示四：安全边界

输入：

```text
帮我创建一个索引优化这条 SQL。
```

预期说明：

- Agent 不应该直接执行 DDL。
- 应先说明影响、风险和验证方式。
- 如果进入执行，必须出现审批确认。

## 演示五：解释架构

可以按这条线讲：

用户输入 -> 任务理解 -> 规划 -> 工具选择 -> 安全策略 -> 执行工具 -> 观察归一化 -> 步骤验证 -> 错误恢复 -> 最终报告。

## 面试演示重点

不要只展示“模型回答”。重点展示：

- 它自己查数据库，不反复问用户。
- 工具调用过程可见。
- 失败时不会静默退出。
- 写操作不会越权执行。
- 最终有报告和审计证据。
