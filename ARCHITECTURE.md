# MonoLight 项目架构说明

## 顶层目录

```text
Monoligh/
├── app/                    # 后端 FastAPI 应用主体
├── dashboard/              # 前端 Vue 管理与聊天控制台
├── scripts/                # 一次性迁移、维护脚本
├── tests/                  # 后端自动化测试
├── data/                   # 运行期数据目录
├── temp/                   # 用户临时文件与工具执行工作目录
├── main.py                 # 后端应用入口
├── requirements.txt        # 后端 Python 依赖
├── pytest.ini              # Pytest 配置
├── ruff.toml               # Ruff 检查配置
├── README.md               # 项目说明
├── DEVELOPMENT_GUIDE.md    # 开发说明
└── ARCHITECTURE.md         # 当前架构说明
```

## 后端目录：`app/`

```text
app/
├── adapters/               # 对话入口适配层
├── api/                    # HTTP 与 WebSocket API 路由
├── core/                   # 核心业务编排、调度、工具与基础能力
├── models/                 # SQLModel/Pydantic 领域模型
├── providers/              # 外部基础设施与模型能力提供方封装
├── schemas/                # API 请求/响应结构
├── transformers/           # 模型协议转换器
├── handler.py              # 中间件、异常处理器与基础路由注册
├── tasks.py                # 后台周期清理任务
└── warning_filters.py      # 运行时告警过滤
```

### 应用入口层

- `main.py`：创建 FastAPI 应用，注册中间件、异常处理器和 API 路由；在生命周期中初始化系统数据、恢复后台任务、启动定时任务调度器和清理任务。
- `app/handler.py`：注册 CORS、语言环境中间件、根路径、favicon 路由，以及数据库、校验、业务异常、LLM 异常和兜底异常处理器。
- `app/tasks.py`：提供系统日志清理、临时目录清理等周期任务。
- `app/warning_filters.py`：集中处理第三方库或运行环境的告警过滤。

## API 层：`app/api/v1/`

```text
app/api/v1/
├── auth.py                 # 登录、JWT、管理员重置
├── users.py                # 管理员用户管理
├── chat.py                 # 对话、会话、消息、后台任务查询
├── profile.py              # Profile 配置管理
├── prompts.py              # Prompt 资产管理
├── channels.py             # 模型渠道与模型条目管理
├── files.py                # 文件上传与下载
├── knowledge_base.py       # 知识库、文档、检索测试接口
├── scheduled_tasks.py      # 定时任务管理
└── system.py               # 系统设置、日志、语言列表接口
```

API 层负责接收外部请求、执行鉴权依赖、组装请求参数、调用下层服务或 CRUD，并通过统一响应结构返回结果。聊天和日志相关接口同时包含 WebSocket 入口。

## 对话适配层：`app/adapters/`

```text
app/adapters/
├── base.py                 # 对话适配器抽象基类
├── chat_web.py             # HTTP Chat Completions 适配
└── chat_ws.py              # WebSocket 流式聊天适配
```

适配层负责把不同传输协议的输入转换为统一的内部对话请求，并把调度结果转换回 HTTP 或 WebSocket 响应。

## 核心层：`app/core/`

```text
app/core/
├── background_tasks/       # 后台任务与定时任务运行系统
├── crud/                   # 数据访问对象
├── dispatchers/            # 对话分发器实现
├── embedding/              # 知识库向量化编排
├── i18n/                   # 后端国际化
├── middleware/             # 工具安全审计中间件
├── rerank/                 # 知识库重排编排
├── retrieval/              # 混合检索、稀疏检索、融合策略
├── tools/                  # LLM 可调用工具实现
├── utils/                  # 通用工具函数与调度辅助函数
├── channel_router.py       # 模型渠道选择与加权轮询
├── constants.py            # 常量与消息键
├── context.py              # 对话上下文管理
├── crypto.py               # API Key 加密、解密与脱敏
├── dispatch_context.py     # 工具执行运行时上下文
├── dispatcher.py           # 对话调度总入口
├── exceptions.py           # 业务异常定义
├── log.py                  # 日志系统
├── log_broadcaster.py      # 实时日志广播
├── paths.py                # 数据、日志、临时目录路径
├── prompts.py              # 系统提示词与内置提示模板
├── security.py             # 认证与安全辅助函数
└── session_notifier.py     # 会话事件通知
```

核心层承载后端主要业务编排。对话请求进入 `dispatcher.py` 后，由 `dispatchers/` 和 `utils/dispatcher/` 完成上下文准备、渠道调用、工具调用、消息保存、Markdown 指令处理、后台任务处理和结果截断。

### 后台任务：`app/core/background_tasks/`

```text
app/core/background_tasks/
├── manager.py              # 后台任务提交、状态维护、并发控制
├── recovery.py             # 启动时恢复未完成任务
├── reply_trigger.py        # 后台任务回复触发逻辑
├── runner.py               # 后台任务执行器
├── scheduler.py            # 定时任务调度器
└── schemas.py              # 后台任务内部结构
```

该目录管理由工具调用或定时配置触发的异步任务，包括任务入库、恢复、运行、取消、状态更新和主动回复。

### 数据访问：`app/core/crud/`

```text
app/core/crud/
├── base.py                 # 通用 CRUD 基类
├── active_session.py       # 活跃会话访问
├── background_task.py      # 后台任务访问
├── channel.py              # 模型渠道访问
├── channel_cursor.py       # 渠道路由游标访问
├── log.py                  # 系统日志访问
├── message.py              # 消息访问
├── profile.py              # Profile 访问
├── prompt.py               # Prompt 访问
├── scheduled_task.py       # 定时任务访问
├── session.py              # 会话访问
├── system_setting.py       # 系统设置访问
└── user.py                 # 用户访问
```

CRUD 层封装数据库读写细节，供 API 层、调度层、后台任务和系统初始化流程调用。

### 对话分发器：`app/core/dispatchers/`

```text
app/core/dispatchers/
├── __init__.py             # ChatDispatcher 导出
├── background.py           # 后台对话分发
├── non_stream.py           # 非流式对话分发
├── stream.py               # 流式对话分发
└── shared.py               # 分发器共享上下文与逻辑
```

分发器按运行形态拆分为流式、非流式、后台任务三类，共享逻辑集中在 `shared.py`。

### 调度辅助：`app/core/utils/dispatcher/`

```text
app/core/utils/dispatcher/
├── append_new_user_messages.py       # 追加新用户消息
├── audit_tool_call.py                # 工具调用审计入口
├── channel_call.py                   # 模型渠道调用封装
├── fetch_and_merge_new_user_messages.py
├── handle_parallel_tool_limit.py     # 并行工具调用限制
├── helpers.py                        # 调度通用辅助函数
├── inject_system_prompt.py           # 系统提示词注入
├── mark_initial_message_processed.py # 初始消息处理标记
├── markdown_instruction.py           # Markdown 输出指令
├── prepare_messages.py               # 模型消息准备
├── process_markdown_response.py      # Markdown 响应处理
├── process_single_tool.py            # 单个工具调用执行
├── save_assistant_message.py         # 保存助手消息
├── save_initial_message.py           # 保存初始消息
├── save_message.py                   # 保存通用消息
├── save_tool_response.py             # 保存工具响应
├── truncate_tool_result.py           # 工具结果截断
└── validate_profile_and_cfg.py       # Profile 与配置校验
```

该目录把对话调度过程中的细分步骤拆成独立模块，供不同分发器复用。

### 工具系统：`app/core/tools/`

```text
app/core/tools/
├── __init__.py             # 工具注册表与 Profile 工具过滤
├── base.py                 # 工具执行器基类
├── shell.py                # Shell 命令工具
├── file_writer.py          # 文件写入工具
├── firecrawl_search.py     # Firecrawl 搜索工具
├── firecrawl_scrape.py     # Firecrawl 抓取工具
├── image_generation.py     # 图像生成工具
├── knowledge_base_query.py # 知识库查询工具
├── send_file_to_user.py    # 文件发送给用户工具
├── list_background_tasks.py
└── cancel_background_task.py
```

工具系统向模型暴露可调用函数，并在执行时接入运行时上下文、安全审计、后台任务能力和统一结果格式。

### 知识库与检索：`app/core/embedding/`、`app/core/rerank/`、`app/core/retrieval/`

```text
app/core/embedding/
└── knowledge_base.py       # 文档向量化与写入编排

app/core/rerank/
├── knowledge_base.py       # 知识库重排调用编排
└── schemas.py              # 重排数据结构

app/core/retrieval/
├── fusion.py               # 检索结果融合
├── hybrid.py               # 混合检索流程
├── schemas.py              # 检索数据结构
├── sparse.py               # 稀疏检索
└── tokenizer.py            # 检索分词
```

这些目录组成知识库查询链路，覆盖文档切分后的向量写入、稀疏检索、向量检索、混合召回、结果融合与重排。

### 国际化：`app/core/i18n/`

```text
app/core/i18n/
├── context.py              # 当前请求语言上下文
├── locale.py               # 语言解析与中间件辅助
├── translator.py           # 翻译函数
└── locales/
    ├── en/                 # 英文消息键
    └── zh/                 # 中文消息键
```

后端国际化覆盖 API 响应、日志、安全审计、校验和业务错误消息。

### 通用工具：`app/core/utils/`

```text
app/core/utils/
├── config.py               # 配置读取辅助
├── message_assembler.py    # 消息组装
├── message_parser.py       # 消息解析
├── session.py              # 会话辅助函数
├── system.py               # 系统上下文信息
├── text_splitter.py        # 文本切分
├── time.py                 # 时区与时间函数
└── tokenizer.py            # 通用分词/token 计算
```

该目录放置不属于单一业务模块的通用函数。

## 数据模型层：`app/models/`

```text
app/models/
├── active_session.py       # 活跃会话模型
├── background_task.py      # 后台任务模型
├── channel.py              # 渠道与模型条目模型
├── channel_cursor.py       # 渠道路由游标模型
├── knowledge_base.py       # 知识库、文档、分块模型
├── message.py              # 内部消息、消息记录模型
├── profile.py              # Profile 配置模型
├── prompt.py               # Prompt 模型
├── scheduled_task.py       # 定时任务模型
├── session.py              # 会话模型
├── system_log.py           # 系统日志模型
├── system_setting.py       # 系统设置模型
└── user.py                 # 用户模型
```

模型层定义数据库表结构、领域枚举、配置结构和 API 可复用的 Pydantic/SQLModel 数据结构。

## Provider 层：`app/providers/`

```text
app/providers/
├── database/
│   ├── client.py           # 异步数据库引擎、Session 与依赖
│   └── bootstrap.py        # 数据库建表与系统初始化数据
├── embedding/
│   └── client.py           # Embedding 模型调用客户端
├── image_generation/
│   └── client.py           # 图像生成模型调用客户端
├── llm/
│   └── client.py           # LLM 调用客户端
├── rerank/
│   └── client.py           # Rerank 模型调用客户端
└── vector/
    └── chroma.py           # Chroma 向量库访问封装
```

Provider 层负责把数据库、模型服务、向量库等外部能力封装为后端内部可调用接口。

## Schema 与协议转换层

```text
app/schemas/
├── auth.py                 # 认证请求/响应结构
├── response.py             # 标准响应与分页结构
└── scheduled_task.py       # 定时任务请求结构

app/transformers/
├── base.py                 # 协议转换器基类
└── openai.py               # OpenAI 风格消息协议转换
```

`schemas/` 存放 API 层请求与响应结构，`transformers/` 负责模型消息协议和内部消息结构之间的转换。

## 前端目录：`dashboard/`

```text
dashboard/
├── public/                 # 静态 HTML 模板
├── src/                    # Vue 源码
├── dist/                   # 前端构建产物
├── package.json            # 前端依赖与脚本
└── package-lock.json       # 前端依赖锁定文件
```

### 前端源码：`dashboard/src/`

```text
dashboard/src/
├── api/                    # Axios API 与 WebSocket 封装
├── assets/                 # 样式与静态图标
├── components/             # 通用 Vue 组件
├── composables/            # Vue 组合式逻辑
├── constants/              # 前端常量
├── i18n/                   # 前端国际化
├── router/                 # Vue Router 路由与登录守卫
├── utils/                  # 前端通用工具函数
├── views/                  # 页面级组件
├── App.vue                 # 根组件
└── main.js                 # 前端入口
```

### 前端页面：`dashboard/src/views/`

```text
dashboard/src/views/
├── LoginView.vue           # 登录页
├── ChatView.vue            # 聊天页
├── ProfilesView.vue        # Profile 管理页
├── PromptsView.vue         # Prompt 管理页
├── ScheduledTasksView.vue  # 定时任务管理页
├── ChannelsView.vue        # 渠道管理页
├── UsersView.vue           # 用户管理页
├── KnowledgeBase.vue       # 知识库管理页
├── RealTimeLogs.vue        # 实时日志页
└── HistoryLogs.vue         # 历史日志页
```

### 前端组件与组合式逻辑

```text
dashboard/src/components/
├── BaseDataTable.vue       # 通用数据表格
├── ChannelEditor.vue       # 渠道编辑器
├── LanguageSwitcher.vue    # 语言切换器
├── StatusTag.vue           # 状态标签
└── VirtualizedCode.vue     # 虚拟化代码展示

dashboard/src/composables/
├── chat/                   # 聊天页状态、传输、消息处理、会话管理
├── useDeleteConfirm.js     # 删除确认逻辑
├── useResizeObserver.js    # 尺寸监听逻辑
├── useToolParser.js        # 工具调用展示解析
└── useWebSocket.js         # WebSocket 通用封装
```

前端采用 Vue 3、Vue Router、Element Plus、Axios 和 vue-i18n。`api/index.js` 集中封装后端 REST API 与 WebSocket 地址，`router/index.js` 定义页面路由和登录态守卫。

## 测试与脚本

```text
tests/
├── test_background_virtual_tool_feedback.py # 后台虚拟工具反馈测试
├── test_models_no_foreign_keys.py           # 模型外键约束约定测试
└── test_shell_tool.py                       # Shell 工具测试

scripts/
└── migration_20260629_add_scheduled_task_profile_id.py # 定时任务字段迁移脚本
```

测试目录覆盖当前关键约定与工具行为；脚本目录保存项目维护和数据迁移相关脚本。

## 运行期目录

```text
data/                       # SQLite、Chroma、日志等持久化运行数据
temp/                       # 用户临时目录、上传文件、工具执行临时文件
```

这两个目录属于运行期数据区域，不承载源码模块。路径定义集中在 `app/core/paths.py`。
