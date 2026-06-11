# zuixiaoagent — 终端操作型编程智能体

基于 LangChain 生态全套组件构建的终端操作型编程智能体，支持 Shell 命令执行、文件操作、代码搜索、人工审批拦截、任务自动规划、多轮对话记忆持久化。

**支持双 LLM Provider：** DeepSeek (`deepseek-chat` / `deepseek-reasoner`) 和 OpenAI (`gpt-4o` / `gpt-4-turbo`)，通过 `LLM_PROVIDER` 环境变量一键切换。

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph StateGraph                     │
│                                                             │
│  START → task_planner → memory_compactor → llm_reason       │
│              │                                    │          │
│              │                         ┌──────────┴───────┐ │
│              │                         │  human_approval   │ │
│              │                         │  (interrupt HITL) │ │
│              │                         └──────────┬───────┘ │
│              │                                    │          │
│              │                         ┌──────────┴───────┐ │
│              │                         │  execute_tools    │ │
│              │                         └──────────┬───────┘ │
│              │                                    │          │
│              └────────────────────────────────────┘          │
│                                    │                        │
│                         ┌──────────┴───────┐               │
│                         │  error_handler    │               │
│                         └──────────┬───────┘               │
│                                    │                        │
│                              ┌─────┴─────┐                 │
│                              │   END      │                 │
│                              └───────────┘                 │
│                                                             │
│  Checkpointer: SQLite (dev) / PostgreSQL (prod)             │
│  Store: LangGraph Store (vector-indexed long-term memory)   │
│  Tracing: LangSmith (full node-level observability)         │
└─────────────────────────────────────────────────────────────┘
```

## 核心特性

| 特性 | 实现方式 |
|------|----------|
| **分层记忆** | 短期滑动窗口 + 工作记忆 key-value + 长期 LangGraph Store 向量检索 |
| **人工审批** | `interrupt()` 原语 + `interrupt_before` 静态断点，高危命令弹窗确认 |
| **Shell 沙箱** | 命令白名单 + 危险命令黑名单 + Pydantic 参数强校验 |
| **插件化技能** | `SkillRegistry` 自动扫描 `plugins/` 目录，无需修改 Graph |
| **任务规划** | LLM 拆解长指令为多步子任务 DAG，顺序执行 + 依赖解析 |
| **断点续跑** | LangGraph checkpointing 自动持久化，支持时间旅行和 fork |
| **超时熔断** | 节点级超时 + shell 命令超时 + 错误处理器重试/放弃 |
| **上下文压缩** | Token 阈值触发 LLM 摘要压缩，保留最近消息 |
| **LangSmith 观测** | 自动追踪每个节点耗时、LLM 入参出参、工具调用、报错堆栈 |

## 快速启动

### 前置条件

- Python 3.11+
- DeepSeek API Key（推荐）或 OpenAI API Key
- (可选) LangSmith API Key — 用于观测追踪
- (生产) PostgreSQL 16+

### 一键启动

```bash
# 1. 安装依赖
make install

# 2. 编辑环境变量
vim .env  # 已预配置 DeepSeek，只需确认 DEEPSEEK_API_KEY

# 3. 启动开发服务器
make dev
# → LangGraph Server: http://localhost:2024
# → LangGraph Studio: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

### CLI 客户端

```bash
# 在另一个终端
make cli

# 或手动指定参数
python -m cli.main --url http://localhost:2024
```

### 运行测试

```bash
make test
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LLM_PROVIDER` | - | `deepseek` | LLM 提供商：`deepseek` 或 `openai` |
| `DEEPSEEK_API_KEY` | ✅(deepseek) | - | DeepSeek API 密钥 |
| `OPENAI_API_KEY` | ✅(openai) | - | OpenAI API 密钥 |
| `DEEPSEEK_BASE_URL` | - | `https://api.deepseek.com` | DeepSeek API 地址 |
| `LANGSMITH_API_KEY` | 推荐 | - | LangSmith 追踪密钥 |
| `LANGSMITH_TRACING` | - | `true` | 是否启用 LangSmith |
| `LANGSMITH_PROJECT` | - | `zuixiaoagent` | LangSmith 项目名 |
| `POSTGRES_URI` | - | (空=SQLite) | PostgreSQL 连接串 |
| `LLM_MODEL` | - | `gpt-4o` | LLM 模型标识 |
| `LLM_TEMPERATURE` | - | `0` | LLM 温度 |
| `MAX_RETRIES` | - | `3` | 最大重试次数 |
| `NODE_TIMEOUT_SECONDS` | - | `60` | 节点超时（秒） |
| `MEMORY_WINDOW_TOKENS` | - | `8000` | 短期记忆窗口 |
| `MEMORY_COMPACT_THRESHOLD` | - | `12000` | 触发压缩的 token 阈值 |
| `AGENT_API_KEY` | - | (空=无鉴权) | Server API 鉴权密钥 |
| `AGENT_LOG_LEVEL` | - | `INFO` | 日志级别 |

## 项目结构

```
zuixiaoagent/
├── agent/                    # 核心 Agent
│   ├── graph.py              # StateGraph 组装与编译
│   ├── state.py              # AgentState Pydantic 定义
│   ├── config.py             # 环境变量配置
│   ├── nodes/                # 图节点
│   │   ├── llm_node.py       # LLM 推理
│   │   ├── tool_executor.py  # 工具执行
│   │   ├── human_approval.py # 人工审批拦截
│   │   ├── task_planner.py   # 任务规划
│   │   ├── memory_compactor.py # 上下文压缩
│   │   └── error_handler.py  # 错误处理/重试
│   └── edges/routes.py       # 条件路由
├── memory/                   # 分层记忆
│   ├── short_term.py         # 短期滑动窗口
│   ├── working.py            # 工作记忆
│   ├── long_term.py          # 长期持久化
│   └── manager.py            # 记忆编排器
├── tools/                    # 工具系统
│   ├── base.py               # AgentTool 基类
│   ├── shell.py              # Shell 执行（白名单+沙箱）
│   ├── registry.py           # 插件注册中心
│   └── builtin/              # 内置工具
├── plugins/                  # 插件目录（自动发现）
├── cli/                      # CLI 客户端
├── server/                   # Server + Auth 中间件
├── tests/                    # 测试
├── langgraph.json            # LangGraph Server 配置
├── requirements.txt          # 依赖
├── Makefile                  # 一键启动
├── .env.example              # 环境变量模板
└── README.md                 # 本文档
```

## Web UI

连接 CopilotKit Agent Chat UI：

```bash
# 在另一个项目中（需 Node.js）
npx create-copilot-app@latest
# 配置指向 http://localhost:2024 的 LangGraph SSE endpoint
```

或直接访问 LangGraph Studio 进行可视化调试：
`https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024`

## CLI 命令

```
> 任意消息           发送给 Agent
/help              显示帮助
/quit              退出
/new               新建会话
/resume <id>       恢复会话
/sessions          列出最近会话
/history           查看当前会话检查点历史
/info              查看 Agent 配置
/plan              查看当前执行计划
/clear             清屏
```

## 添加新工具（插件）

在 `plugins/` 目录下创建 `.py` 文件：

```python
from tools.base import AgentTool
from pydantic import BaseModel, Field

class MyInput(BaseModel):
    param: str = Field(description="Parameter description")

class MyTool(AgentTool):
    name: str = "my_tool"
    description: str = "What my tool does"
    args_schema: type[BaseModel] = MyInput

    def _run(self, param: str) -> str:
        return f"Result: {param}"
```

重启服务器，工具自动注册。无需修改 Graph 代码。

## 生产部署

```bash
# Docker Compose
docker compose up -d

# 或手动
pip install -r requirements.txt
langgraph up --port 2024
```

## License

MIT
