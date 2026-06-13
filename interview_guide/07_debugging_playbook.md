# 调试与 Bug 修复

## 调试原则

遇到 Agent 不可用时，不要只看模型回答，要按链路排查：

1. CLI 是否启动正确。
2. 服务是否健康。
3. 数据库配置是否传到服务。
4. 意图是否识别正确。
5. 计划是否合理。
6. 工具是否可见。
7. 工具调用是否成功。
8. 安全策略是否误拦截。
9. verify_step 是否错误判定。
10. final_report 是否正确交付。

## 典型问题 1：一直要求澄清

原因：任务理解模块把可从配置获得的信息当成缺失信息。

修复：把数据库 URL、target env、target database 从 CLI runtime config 注入状态；只对真正无法推断且有风险的信息提问。

## 典型问题 2：工具调用结果不展示

原因：CLI 把内部事件都打出来，真正有用的工具开始/结束和摘要反而被淹没。

修复：参考 Codex 输出，只展示“正在运行什么工具、结果摘要、失败原因”；verbose 模式才展示内部状态。

## 典型问题 3：GraphRecursionError

原因：工具失败后 verify/recovery 反复把任务重新送回同一步，无法达到终止条件。

修复：给读 SQL 错误一次修复机会；重复同类错误则明确 blocked；CLI 检测 run error，避免静默结束。

## 典型问题 4：模型输出 tool_call 标记文本

原因：部分模型会把工具调用以 DSML/XML 文本形式返回，而不是原生 tool_calls。

修复：在模型边界解析并规范化工具调用；用户界面过滤工具调用协议文本。

## 典型问题 5：provider 报 tool_calls 顺序错误

原因：OpenAI-compatible API 要求 assistant 的 tool_calls 后面必须紧跟对应 ToolMessage；恢复流程中插入 SystemMessage 会破坏协议。

修复：调用模型前清理消息历史，只保留合法的 tool_call/ToolMessage 连续结构。

## 典型问题 6：report 阶段调用工具被拒绝

原因：报告步骤策略是 no_tools，但模型仍想补充证据。

修复：在 report 阶段将工具拒绝降级为“基于已有证据生成报告”的提示，不把整个任务判死。

## 典型问题 7：交付包状态 blocked 但任务 completed

原因：旧的 loop_status 或质量门规则与真实 runtime 状态不一致。

修复：交付状态以 `db_task_runtime.task_status`、error_reports、quality gate 综合判断；只对写 SQL 强制审批和 safety report。

## 调试命令

```bash
.venv/bin/python -m pytest tests/test_agent_loop.py tests/test_cli_module.py -q
.venv/bin/python -m pytest tests/test_postgres_tools.py tests/test_artifact_delivery.py -q
.venv/bin/python -m compileall agent cli tools delivery tests
```

真实验证：

```bash
minidb-agent --database-url "$POSTGRES_TARGET_URL" --target-env dev
```

只读诊断：

```bash
minidb-agent exec --json "数据库最需要优化的 SQL 是什么？"
```
