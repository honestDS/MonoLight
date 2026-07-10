# MonoLight 项目架构说明

## 顶层目录

```text
Monoligh/
├── .agents/                # Agent 相关本地配置目录
├── .clinerules/            # 项目级 Cline 规则
├── app/                    # 后端 FastAPI 应用主体
├── dashboard/              # 前端 Vue 管理与聊天控制台
├── data/                   # 运行期持久化数据目录
├── scripts/                # 一次性迁移、维护脚本
├── temp/                   # 用户临时文件与工具执行工作目录
├── tests/                  # 后端自动化测试
├── .env                    # 本地环境变量
├── .gitattributes          # Git 属性配置
├── .gitignore              # Git 忽略配置
├── ARCHITECTURE.md         # 当前架构说明
├── DEVELOPMENT_GUIDE.md    # 开发说明
├── LICENSE                 # 项目许可证
├── README.md               # 项目说明
├── logo.jpg                # 项目 Logo
├── main.py                 # 后端应用入口
├── start.py                # 跨平台进程启动器
├── pytest.ini              # Pytest 配置
├── requirements.txt        # 后端 Python 依赖
└── ruff.toml               # Ruff 检查配置
```

本说明仅描述项目源码、配置、运行期数据与维护脚本。`.git/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/`、`node_modules/` 等版本控制、虚拟环境、依赖缓存或工具缓存目录不作为源码架构的一部分展开。

## 后端目录：`app/`

```text
app/
├── adapters/               # 对话入口与消息平台适配层
├── api/                    # HTTP 与 WebSocket API 路由
├── core/                   # 核心业务编排、调度、工具与基础能力
├── models/                 # SQLModel/Pydantic 领域模型
├── providers/              # 外部基础设施与模型能力提供方封装
├── schemas/                # API 请求/响应结构
├── transformers/           # 模型协议转换器
├── workers/                # 独立后台进程入口
├── __init__.py             # 后端包标识
├── handler.py              # 中间件、异常处理器与基础路由注册
├── tasks.py                # 后台周期清理任务
└── warning_filters.py      # 运行时告警过滤
```

### 应用入口层

- `start.py`：跨平台统一启动器；读取 `.env` 中的 `APP_PORT`、`APP_HOST` 和 `APP_WORKERS`，在父进程中完成一次数据库与系统数据初始化，再按操作系统创建并托管 Uvicorn Web 进程与唯一后台 Worker。
- `main.py`：创建 FastAPI 应用，注册中间件、异常处理器和 API 路由；每个 Web Worker 在生命周期中恢复可原子认领的后台任务、启动周期清理任务和数据库会话事件订阅，不运行定时任务调度器或消息平台长轮询。
- `app/workers/message_platform.py`：独立后台进程入口；复用统一告警过滤配置，并使用进程文件锁确保单机只运行一个实例，统一承担定时任务调度、消息平台长轮询和 Outbox 投递。
- `app/handler.py`：注册 CORS、语言环境中间件、根路径、favicon 路由，以及数据库、校验、业务异常、LLM 异常和兜底异常处理器。
- `app/tasks.py`：提供系统日志清理、临时目录清理等周期任务。
- `app/warning_filters.py`：集中处理第三方库或运行环境的告警过滤。

## API 层：`app/api/v1/`

```text
app/api/v1/
├── auth.py                 # 登录、JWT、管理员重置
├── channels.py             # 模型渠道与模型条目管理
├── chat.py                 # 对话、会话、消息、后台任务查询
├── files.py                # 文件上传与下载
├── knowledge_base.py       # 知识库、文档、检索测试接口
├── message_platforms.py    # 消息平台管理与微信 OpenClaw 登录接口
├── profile.py              # Profile 配置管理
├── prompts.py              # Prompt 资产管理
├── scheduled_tasks.py      # 定时任务管理
├── system.py               # 系统设置、日志、语言列表接口
└── users.py                # 管理员用户管理
```

API 层负责接收外部请求、执行鉴权依赖、组装请求参数、调用下层服务或 CRUD，并通过统一响应结构返回结果。聊天和日志相关接口同时包含 WebSocket 入口；消息平台接口负责平台配置、启停、微信 OpenClaw 扫码登录与登录状态轮询。

## 对话适配层：`app/adapters/`

```text
app/adapters/
├── base.py                 # 对话适配器抽象基类
├── chat_web.py             # HTTP Chat Completions 适配
├── chat_ws.py              # WebSocket 流式聊天适配
└── weixin_openclaw/        # 微信 OpenClaw 消息平台适配包
    ├── __init__.py
    ├── adapter.py          # OpenClaw 对话适配入口
    ├── client.py           # OpenClaw HTTP 客户端
    ├── config.py           # OpenClaw 配置解析与默认值
    ├── constants.py        # OpenClaw 常量
    ├── crypto.py           # OpenClaw 敏感配置加解密
    ├── media.py            # OpenClaw 媒体与文件处理
    ├── message.py          # OpenClaw 消息解析与组装
    ├── response.py         # OpenClaw 响应结构处理
    └── schemas.py          # OpenClaw 内部数据结构
```

适配层负责把不同传输协议或消息平台的输入转换为统一的内部对话请求，并把调度结果转换回对应通道响应。微信 OpenClaw 适配包封装登录、长轮询收信、回调字段兼容、上下文 token、同步游标、媒体消息和消息发送，并复用内部聊天调度链路生成回复；业务异常和未知异常会被转换为本地化错误回复发送给用户。

## 核心层：`app/core/`

```text
app/core/
├── background_tasks/       # 后台任务与定时任务运行系统
├── crud/                   # 数据访问对象
├── dispatchers/            # 对话分发器实现
├── embedding/              # 知识库向量化编排
├── i18n/                   # 后端国际化
├── message_platforms/      # 消息平台后台轮询与通知管理
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
├── __init__.py
├── manager.py              # 后台任务提交、状态维护、并发控制
├── recovery.py             # 启动时恢复未完成任务
├── reply_trigger.py        # 后台任务回复触发逻辑
├── runner.py               # 后台任务执行器
├── scheduler.py            # 定时任务调度器
└── schemas.py              # 后台任务内部结构
```

该目录管理由工具调用或定时配置触发的异步任务，包括任务入库、恢复、运行、取消、状态更新和主动回复。定时任务调度器只在唯一后台 Worker 中启动，避免多个 Uvicorn Web Worker 重复触发同一计划任务。

### 消息平台：`app/core/message_platforms/`

```text
app/core/message_platforms/
├── __init__.py
├── base.py                 # 消息平台处理器抽象接口
├── manager.py              # 平台轮询任务与 Outbox 消费管理
├── notifier.py             # Web 通知或外部平台 Outbox 入队
├── process_lock.py         # 独立 Worker 单实例文件锁
└── weixin_openclaw.py      # 微信 OpenClaw 轮询、消息合并与会话事件处理
```

消息平台管理器只由 `app/workers/message_platform.py` 独立进程启动，通过平台处理器注册表加载可轮询平台，并为每个平台维护独立任务。Web Worker 只负责修改数据库中的平台配置；独立进程定期刷新配置并启动或停止对应任务。单机进程文件锁阻止重复启动消息平台 Worker，因此无需让所有 Web Worker 竞争平台租约。新增消息平台时实现 `MessagePlatformHandler` 并注册到管理器，无需把平台专属逻辑写入通用管理器。

外部平台主动事件采用持久化 Outbox：后台任务或计划任务生成事件后，`notifier.py` 将事件写入 `message_platform_outbox`；唯一后台进程原子认领事件并投递，失败时指数退避重试，进程异常退出后可通过认领租约恢复未完成事件。单次投递由管理器限制在 300 秒内，认领租约为 330 秒，保证正常投递不会因租约提前到期而被重复认领。相同事件使用确定性摘要键去重，投递语义为至少一次；如果外部平台不支持幂等，进程在“远端已接收但本地尚未标记成功”的极小窗口崩溃时仍可能重复发送。

WebSocket 主动事件采用数据库广播：事件写入 `session_event` 后，每个 Web Worker 使用独立递增游标读取并投递到本进程的 WebSocket 队列，因此连接所在进程都能观察到事件。新 Worker 从启动时的最新事件 ID 开始消费，避免重放历史消息；事件保留 24 小时并由通知轮询器定期清理。

微信 OpenClaw 处理器复用适配层的 `sync_buf` 状态，按平台配置的长轮询超时与轮询间隔拉取消息；收到消息后完成消息合并、上下文 token 保存和内部聊天调度，回复结果经适配器发送回 OpenClaw 会话。运行时状态、同步游标和错误信息通过消息平台 CRUD 写回数据库。

### 数据访问：`app/core/crud/`

```text
app/core/crud/
├── active_session.py       # 活跃会话访问
├── background_task.py      # 后台任务访问
├── base.py                 # 通用 CRUD 基类
├── channel.py              # 模型渠道访问
├── channel_cursor.py       # 渠道路由游标访问
├── log.py                  # 系统日志访问
├── message.py              # 消息访问
├── message_platform.py     # 消息平台访问
├── message_platform_outbox.py # 消息平台发件箱访问与原子认领
├── profile.py              # Profile 访问
├── prompt.py               # Prompt 访问
├── scheduled_task.py       # 定时任务访问
├── session.py              # 会话访问
├── session_event.py        # WebSocket 跨进程广播事件访问
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
├── shared.py               # 分发器共享上下文与逻辑
└── stream.py               # 流式对话分发
```

分发器按运行形态拆分为流式、非流式、后台任务三类，共享逻辑集中在 `shared.py`。

### 调度辅助：`app/core/utils/dispatcher/`

```text
app/core/utils/dispatcher/
├── __init__.py
├── append_new_user_messages.py       # 追加新用户消息
├── audit_tool_call.py                # 工具调用审计入口
├── channel_call.py                   # 模型渠道调用封装
├── fetch_and_merge_new_user_messages.py # 拉取并合并新增用户消息
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
├── cancel_background_task.py # 后台任务取消工具
├── file_writer.py          # 文件写入工具
├── firecrawl_scrape.py     # Firecrawl 抓取工具
├── firecrawl_search.py     # Firecrawl 搜索工具
├── image_generation.py     # 图像生成工具
├── knowledge_base_query.py # 知识库查询工具
├── list_background_tasks.py # 后台任务列表工具
├── send_file_to_user.py    # 文件发送给用户工具
└── shell.py                # Shell 命令工具
```

工具系统向模型暴露可调用函数，并在执行时接入运行时上下文、安全审计、后台任务能力和统一结果格式。

### 知识库与检索：`app/core/embedding/`、`app/core/rerank/`、`app/core/retrieval/`

```text
app/core/embedding/
├── __init__.py
└── knowledge_base.py       # 文档向量化与写入编排

app/core/rerank/
├── __init__.py
├── knowledge_base.py       # 知识库重排调用编排
└── schemas.py              # 重排数据结构

app/core/retrieval/
├── __init__.py
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
├── __init__.py
├── context.py              # 当前请求语言上下文
├── locale.py               # 语言解析与中间件辅助
├── translator.py           # 翻译函数
└── locales/                # 多语言消息键目录
```

后端国际化覆盖 API 响应、日志、安全审计、校验和业务错误消息。

### 通用工具：`app/core/utils/`

```text
app/core/utils/
├── dispatcher/             # 对话调度辅助模块
├── config.py               # 配置读取辅助
├── message_assembler.py    # 消息组装
├── message_parser.py       # 消息解析
├── session.py              # 会话辅助函数
├── system.py               # 系统上下文信息
├── text_splitter.py        # 文本切分
├── time.py                 # 时区与时间函数
└── tokenizer.py            # 通用分词/token 计算
```

该目录放置不属于单一业务模块的通用函数，并包含调度器拆分出的可复用步骤。

## 数据模型层：`app/models/`

```text
app/models/
├── __init__.py             # 模型包导出
├── active_session.py       # 活跃会话模型
├── background_task.py      # 后台任务模型
├── channel.py              # 渠道与模型条目模型
├── channel_cursor.py       # 渠道路由游标模型
├── knowledge_base.py       # 知识库、文档、分块模型
├── message.py              # 内部消息、消息记录模型
├── message_platform.py     # 消息平台模型、状态与响应结构
├── message_platform_outbox.py # 消息平台持久化发件箱模型
├── profile.py              # Profile 配置模型
├── prompt.py               # Prompt 模型
├── scheduled_task.py       # 定时任务模型
├── session.py              # 会话模型
├── session_event.py        # WebSocket 跨进程广播事件模型
├── system_log.py           # 系统日志模型
├── system_setting.py       # 系统设置模型
└── user.py                 # 用户模型
```

模型层定义数据库表结构、领域枚举、配置结构和 API 可复用的 Pydantic/SQLModel 数据结构。消息平台模型使用 `config` 保存平台私有配置、`state` 保存运行时状态；`token`、`bot_token` 等敏感配置入库前加密，API 响应会过滤这些敏感字段。`message_platform_outbox` 保存待投递事件、处理状态、认领所有者、租约、重试次数和错误信息；`session_event` 保存供所有 Web Worker 读取的短期主动会话事件。

## Provider 层：`app/providers/`

```text
app/providers/
├── __init__.py
├── database/
│   ├── __init__.py
│   ├── bootstrap.py        # 数据库建表与系统初始化数据
│   └── client.py           # 异步数据库引擎、Session 与依赖
├── embedding/
│   ├── __init__.py
│   └── client.py           # Embedding 模型调用客户端
├── image_generation/
│   ├── __init__.py
│   └── client.py           # 图像生成模型调用客户端
├── llm/
│   ├── __init__.py
│   └── client.py           # LLM 调用客户端
├── rerank/
│   ├── __init__.py
│   └── client.py           # Rerank 模型调用客户端
└── vector/
    ├── __init__.py
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
├── dist/                   # 前端构建产物
├── node_modules/           # 前端本地依赖安装目录
├── public/                 # 静态 HTML 模板
├── src/                    # Vue 源码
├── package-lock.json       # 前端依赖锁定文件
└── package.json            # 前端依赖与脚本
```

`node_modules/` 为本地安装依赖目录，不承载项目源码；源码主要集中在 `dashboard/src/`。

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
├── ChannelsView.vue        # 渠道管理页
├── ChatView.vue            # 聊天页
├── HistoryLogs.vue         # 历史日志页
├── KnowledgeBase.vue       # 知识库管理页
├── LoginView.vue           # 登录页
├── MessagePlatformsView.vue # 消息平台管理页
├── ProfilesView.vue        # Profile 管理页
├── PromptsView.vue         # Prompt 管理页
├── RealTimeLogs.vue        # 实时日志页
├── ScheduledTasksView.vue  # 定时任务管理页
└── UsersView.vue           # 用户管理页
```

### 前端组件与组合式逻辑

```text
dashboard/src/components/
├── BaseDataTable.vue       # 通用数据表格
├── ChannelEditor.vue       # 渠道编辑器
├── LanguageSwitcher.vue    # 语言切换器
├── MessagePlatformFormDialog.vue # 消息平台创建与编辑弹窗
├── StatusTag.vue           # 状态标签
├── VirtualizedCode.vue     # 虚拟化代码展示
└── weixin_oc/
    └── WeixinOcLoginDialog.vue # 微信 OpenClaw 登录弹窗

dashboard/src/composables/
├── chat/                   # 聊天页状态、传输、消息处理、会话管理
├── useDeleteConfirm.js     # 删除确认逻辑
├── useResizeObserver.js    # 尺寸监听逻辑
├── useToolParser.js        # 工具调用展示解析
└── useWebSocket.js         # WebSocket 通用封装
```

前端采用 Vue 3、Vue Router、Element Plus、Axios 和 vue-i18n。`api/index.js` 集中封装后端 REST API 与 WebSocket 地址，`router/index.js` 定义页面路由和登录态守卫。消息平台页面提供平台列表、状态展示、启停、删除、配置编辑、创建后扫码登录，以及微信 OpenClaw 二维码状态刷新。

## 测试与脚本

```text
tests/
└── unit/
    ├── test_background_virtual_tool_feedback.py # 后台虚拟工具反馈测试
    ├── test_message_platform_manager.py     # 消息平台处理器注册与事件路由测试
    ├── test_message_platform_outbox.py      # Outbox 去重、认领、恢复与重试测试
    ├── test_message_platform_process_lock.py # 消息平台进程单实例锁测试
    ├── test_models_no_foreign_keys.py       # 模型外键约束约定测试
    ├── test_session_notifier.py             # 数据库会话事件广播与清理测试
    ├── test_start.py                        # 跨平台启动器配置与命令测试
    └── test_shell_tool.py                   # Shell 工具测试

scripts/
├── migration_20260629_add_scheduled_task_profile_id.py # 定时任务字段迁移脚本
├── migration_20260703_add_message_platform.py          # 消息平台表迁移脚本
├── migration_20260709_add_chat_session_reply_target_source.py # 会话回复目标来源迁移脚本
├── migration_20260709_add_chat_session_source.py       # 会话来源字段迁移脚本
├── migration_20260710_add_message_platform_outbox.py   # 消息平台 Outbox 表迁移
└── migration_20260710_add_session_event.py             # WebSocket 会话事件广播表迁移
```

测试目录覆盖当前关键约定、工具行为和消息平台管理器；新增单元测试按规范放置在 `tests/unit/`。脚本目录保存项目维护和数据迁移相关脚本。

## 运行期目录

```text
data/                       # SQLite、Chroma、日志等持久化运行数据
temp/                       # 用户临时目录、上传文件、工具执行临时文件
```

这两个目录属于运行期数据区域，不承载源码模块。路径定义集中在 `app/core/paths.py`。`data/message-platform-worker.lock` 是消息平台独立进程的单机实例锁文件。
