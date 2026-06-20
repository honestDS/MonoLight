# MonoLight 系统架构设计说明

## 1. 设计哲学

MonoLight 采用"管控分离、协议标准、安全优先"的设计理念。系统核心由 Profile 驱动架构、PromptLibrary 提示词资产库、统一模型渠道、可审计工具链与知识库检索链路组成。系统通过内部标准消息协议隔离前端协议、模型厂商协议和工具执行协议，使对话、Embedding、Rerank、知识库、工具调用和管理控制台可以独立演进。

## 2. 总体目录结构

当前项目由后端 FastAPI 应用、前端 Vue 管理控制台、测试套件与配置文件组成。

- `main.py`: FastAPI 应用创建与启动入口。
- `app/`: 后端应用主体。
  - `api/`: HTTP 与 WebSocket API 路由。
  - `adapters/`: 对话通信适配器。
  - `core/`: 调度、上下文、安全、日志、工具、检索、审计、国际化与工具函数。
  - `models/`: SQLModel/Pydantic 领域实体与内部消息模型。
  - `providers/`: LLM、Embedding、Rerank、Database、Vector 基础设施 Provider。
  - `schemas/`: API 通用响应与认证请求契约。
  - `transformers/`: 模型协议转换器。
  - `handler.py`: FastAPI 异常处理、中间件与根路由注册。
  - `tasks.py`: 后台周期任务。
  - `warning_filters.py`: 运行时告警过滤。
- `dashboard/`: 前端管理控制台。
- `tests/`: 初始化、集成与单元测试。
- `requirements.txt`: 后端依赖。
- `pytest.ini`: 测试配置。
- `ruff.toml`: Ruff 代码检查配置。

## 3. 后端分层架构

### 3.0 应用入口与生命周期

- `main.py`
  - 创建 FastAPI 应用。
  - 在 `lifespan` 中通过 `app.providers.database.bootstrap.init_system_data` 初始化数据库结构与系统种子数据。
  - 初始化 LogManager。
  - 启动 `background_log_cleaner` 后台日志清理任务。
  - 注册 API Router 与全局 Handler。
  - 通过 `APP_PORT` 环境变量启动 Uvicorn。
- `app/handler.py`
  - 注册 `/` 与 `/favicon.ico`。
  - 注册 CORS 中间件。
  - 注册 HTTP 中间件 `locale_middleware`，基于 `Accept-Language` 设置当前请求语言。
  - 注册 SQLAlchemy、Starlette HTTP、Pydantic Validation、业务异常与兜底异常处理器。
  - LLM 异常以 OpenAI Chat Completion 风格错误消息返回。
- `app/tasks.py`
  - `background_log_cleaner`: 每 24 小时清理超过保留期的系统日志。
- `app/core/paths.py`
  - 统一维护数据目录、临时目录、SQLite 数据库路径、ChromaDB 持久化路径、日志路径与 favicon 路径。

### 3.1 API 层 (`app/api/v1`)

API 层负责外部请求鉴权、路由分发、请求参数接收与统一响应包装。

- `auth.py`: 登录认证、JWT 令牌签发与管理员账号重置。
- `users.py`: 用户账户管理。
- `chat.py`: 核心对话接口。
  - HTTP Chat Completions。
  - WebSocket 流式对话。
  - 会话列表、历史、删除。
  - 会话 Markdown 开关设置。
  - 会话标题生成。
- `profile.py`: Profile 创建、列表、激活、更新与删除。
- `channels.py`: 模型渠道管理。
  - Channel 用途区分：`CHAT`、`EMBEDDING`、`RERANK`。
  - 提供向量维度检测接口。
- `prompts.py`: PromptLibrary 提示词资产维护。
- `files.py`: 文件上传与下载管理，支持对话附件传递。
- `knowledge_base.py`: 知识库 CRUD、文档导入、文本分块、向量写入、检索测试、文档查看与删除。
- `system.py`: 系统日志查询与实时日志 WebSocket 推送。

### 3.2 通信适配层 (`app/adapters`)

适配层抹平 HTTP 与 WebSocket 差异，将外部请求转换为统一调度器输入。

- `base.py`: 对话适配器抽象基类。
- `chat_web.py`: HTTP 非流式对话适配。
- `chat_ws.py`: WebSocket 流式对话适配。

### 3.3 调度控制层 (`app/core`)

- `dispatcher.py`: 核心对话调度器。
  - 支持非流式与流式调度。
  - 管理会话锁状态。
  - 获取活跃 Profile。
  - 获取动态工具清单与知识库白名单。
  - 执行工具调用循环。
  - 合并追加用户消息。
  - 控制最大工具轮次。
  - 处理工具结果落库与助手消息保存。
- `context.py`: 上下文管理器，按 Profile 上下文窗口配置装载并截断历史消息。
- `security.py`: 密码哈希、JWT 创建与当前用户解析。
- `exceptions.py`: 业务异常体系。
- `log.py`: 基于 loguru 的日志系统，支持控制台/文件/数据库日志与 WebSocket 广播。
- `log_broadcaster.py`: 实时日志广播器。
- `constants.py`: 统一错误消息、提示消息与常量定义。
- `prompts.py`: 系统级提示词模板中心，包括安全审计、动态确认、工具轮次限制、知识库提示等模板。

### 3.4 调度器子流程 (`app/core/utils/dispatcher`)

调度器复杂流程被拆分为单一职责函数，便于测试与维护。

- `validate_profile_and_cfg.py`: 校验当前 Profile 与运行时配置。
- `inject_system_prompt.py`: 注入系统提示词与可用知识库目录。
- `markdown_instruction.py`: 根据会话 Markdown 开关向用户消息追加输出要求。
- `prepare_messages.py`: 准备调度上下文消息。
- `append_new_user_messages.py`: 追加新用户消息。
- `fetch_and_merge_new_user_messages.py`: 拉取并合并并发期间新增的用户消息。
- `handle_parallel_tool_limit.py`: 并行工具调用限流与提示。
- `audit_tool_call.py`: 工具调用审计入口。
- `process_single_tool.py`: 单个工具执行、运行时上下文注入与结果处理。
- `process_markdown_response.py`: 在禁用 Markdown 的会话中剥离助手消息 Markdown 标记；使用 Python-Markdown `fenced_code` 扩展处理代码块。
- `save_message.py`: 通用消息保存。
- `save_initial_message.py`: 保存初始用户消息。
- `mark_initial_message_processed.py`: 标记初始消息已处理。
- `save_assistant_message.py`: 保存助手消息并根据会话配置处理 Markdown。
- `save_tool_response.py`: 保存工具响应消息。

### 3.5 通用工具函数 (`app/core/utils`)

- `config.py`: Profile 配置标准化工具。
- `message_assembler.py`: 多模态与附件消息装配。
- `message_parser.py`: 消息解析。
- `session.py`: 会话标题生成工具。
- `system.py`: 系统环境探测，补充 OS、CPU、内存等上下文。
- `text_splitter.py`: 知识库文本分块，支持段落切分、长文本硬切分与重叠窗口。
- `time.py`: 时区感知时间工具。
- `tokenizer.py`: Token 估算工具。

### 3.6 安全审计层 (`app/core/middleware`)

- `auditor.py`: 工具安全审计模块。
  - 对 `execute_shell`、`write_file` 等高风险工具进行 LLM 风险评分。
  - 支持动态确认 Token 校验。
  - 审计结果可直接放行、要求确认、拒绝或按风险分拦截。

### 3.7 国际化层 (`app/core/i18n`)

- `context.py`: 使用 `contextvars` 保存当前请求/会话语言。
- `locale.py`: 语言标准化。
- `translator.py`: 翻译加载、查找与回退。
- `locales/zh` 与 `locales/en`: 后端错误码、通用文案、聊天、LLM、Profile、Prompt、Channel、User、Validation 文案。

## 4. 模型协议与渠道层

### 4.1 Transformer 层 (`app/transformers`)

- `base.py`
  - `BaseTransformer`: 对话生成、流式生成、内部消息与厂商协议互转抽象。
  - `BaseEmbeddingTransformer`: Embedding 抽象，包含单次与批量向量化。
  - `BaseRerankTransformer`: Rerank 抽象，包含原始 rerank 调用与文本重排。
- `openai.py`
  - OpenAI 兼容协议实现。
  - 支持 Chat Completions 非流式、SSE 流式、Embeddings、批量向量化、Rerank 兼容调用。
  - 负责 InternalMessage 与 OpenAI 消息格式互转。

### 4.2 基础设施 Provider 层 (`app/providers`)

- `llm/client.py`: LLM 统一客户端。
  - 持有 Transformer 注册表。
  - 根据 ChannelType 路由到具体 Transformer。
  - 返回 InternalResponse。
- `embedding/client.py`: Embedding 统一客户端。
  - 根据 ChannelType 分发到实现 `BaseEmbeddingTransformer` 的 Transformer。
- `rerank/client.py`: Rerank 统一客户端。
  - 根据 ChannelType 分发到实现 `BaseRerankTransformer` 的 Transformer。
- `database/`
  - `client.py`: SQLAlchemy/SQLModel 异步 Engine、Session 工厂、测试环境数据库 URL 切换与 FastAPI DB 依赖。
  - `bootstrap.py`: 数据库结构同步、默认 Prompt、默认 Profile、默认配置补全。
  - `__init__.py`: 导出 `AsyncSessionLocal`、`Base`、`DATABASE_URL`、`engine`、`get_db`。
- `vector/`
  - `chroma.py`: ChromaDB 持久化客户端，负责 collection 创建、获取、删除、向量条目删除与读取。
  - `__init__.py`: 导出 Chroma collection 管理函数。

## 5. 知识库、检索与重排层

知识库链路由 API、领域模型、文本分块、Embedding、Vector、Hybrid Retrieval 与 Rerank 共同构成。

- `app/api/v1/knowledge_base.py`: 知识库 API 编排入口。
- `app/core/embedding/knowledge_base.py`
  - 读取 Profile 中的向量模型配置。
  - 调用 EmbeddingClient 生成向量。
  - 查询当前 Profile 可用知识库。
  - 构造知识库目录提示词。
  - 执行知识库查询。
- `app/core/retrieval/`
  - `hybrid.py`: 稠密向量检索与 BM25 稀疏检索并发执行，并通过 RRF 融合。
  - `sparse.py`: BM25 稀疏检索。
  - `tokenizer.py`: 中英文混合分词。
  - `fusion.py`: Reciprocal Rank Fusion 排名融合。
  - `schemas.py`: 检索 Chunk 与 Hit 内部结构。
- `app/core/rerank/`
  - `knowledge_base.py`: 对融合候选进行远程 Reranker 精排，并支持失败降级。
  - `schemas.py`: Rerank 相关内部结构。
- `app/core/tools/knowledge_base_query.py`: Agent 知识库查询工具。
  - 校验运行时知识库白名单。
  - 归一化 LLM 传入的 `knowledge_base_id`。
  - 使用内部固定 top_k。
  - 返回精简来源与内容，避免向模型暴露冗余检索元数据。
- `app/models/knowledge_base.py`: KnowledgeBase、KnowledgeBaseDocument、检索测试请求/响应等模型。
- `app/providers/vector/chroma.py`: ChromaDB 向量持久化。
- `requirements.txt`: 知识库链路依赖包含 `chromadb`、`rank-bm25`、`jieba`。

## 6. 领域模型与 Schema 层

### 6.1 领域模型 (`app/models`)

- `user.py`: 用户实体与用户创建、更新、响应模型。
- `channel.py`: 模型渠道实体。
  - `ChannelType`: 模型协议类型。
  - `ModelUsage`: 模型用途，包含 `CHAT`、`EMBEDDING`、`RERANK`。
  - Rerank Channel 要求配置 `base_url`。
- `profile.py`: Profile 实体与 ProfileConfig 嵌套配置。
  - `channel`: 对话模型、Embedding、Rerank、知识库检索数量、上下文窗口等配置。
  - `security`: 审计渠道、审计模型与风险阈值。
  - `tool`: Shell 超时、并发工具数量、最大工具轮次、Firecrawl API Key 等工具配置。
  - `other`: 预留扩展配置。
- `prompt.py`: PromptLibrary 提示词资产实体。
- `message.py`: InternalMessage、InternalToolCall、InternalResponse、消息持久化实体与 ChatCompletionRequest。
- `session.py`: 对话会话实体，包含 `enable_markdown`。
- `active_session.py`: 活跃会话锁实体。
- `knowledge_base.py`: 知识库与知识库文档实体。
- `system_log.py`: 系统运行日志与审计日志实体。

### 6.2 API Schema (`app/schemas`)

- `response.py`: StandardResponse、PageData、LLMResponse 等统一响应结构。
- `auth.py`: 身份验证请求契约。

## 7. 资源执行层 (`app/core/tools`)

工具执行层向 Agent 暴露外部资源调用能力。

- `__init__.py`: 工具 Schema 注册表与执行器映射；根据 Profile 可用知识库动态暴露 `query_knowledge_base`。
- `base.py`: BaseExecutor 抽象基类。
  - 定义异步工具执行接口。
  - 提供 `run_sync` 将同步函数放入线程执行。
  - 提供 `set_runtime_context` 注入 db、profile、session_id、allowed_knowledge_base_ids。
- `shell.py`: Shell 指令执行器。
  - 在用户隔离目录下运行命令。
  - 使用 Profile 中的 Shell 超时配置。
  - 返回标准 JSON 结果与系统信息。
- `file_writer.py`: 受控文件写入器。
- `firecrawl_search.py`: Firecrawl 搜索工具。
  - 使用 `AsyncV1FirecrawlApp.search` 原生异步方法。
  - 将工具入参 `scrape_options` 转为 SDK 所需 `V1ScrapeOptions`。
  - 兼容 snake_case 参数与 SDK v1 camelCase 参数。
- `firecrawl_scrape.py`: Firecrawl 网页抓取工具。
  - 使用 `AsyncV1FirecrawlApp.scrape_url` 原生异步方法。
  - 将工具 schema 的 `raw_html` 转为 SDK v1 需要的 `rawHtml`。
- `knowledge_base_query.py`: 知识库查询工具。

## 8. CRUD 层 (`app/core/crud`)

CRUD 层位于业务逻辑与关系型数据库之间，封装通用异步持久化操作。

- `base.py`: CRUD 抽象基类。
- `user.py`: 用户账户管理。
- `profile.py`: Profile 管理与激活配置查询。
- `prompt.py`: PromptLibrary 管理。
- `channel.py`: 模型渠道管理与按名称查询。
- `session.py`: 对话会话管理。
- `active_session.py`: 活跃会话锁管理。
- `message.py`: 历史消息存储、分页查询、会话列表与会话删除。
- `log.py`: 系统日志与审计日志持久化。

## 9. 标准通信规程

- 内部对话协议：调度器、LLMClient、Transformer 与工具链之间使用 InternalMessage、InternalToolCall、InternalResponse，避免直接透传前端或厂商原始结构。
- 模型供应商协议：当前实现以 OpenAI 兼容协议为主，对话、Embedding 与 Rerank 能力分别通过 LLMClient、EmbeddingClient、RerankClient 分发。
- 知识库工具协议：系统提示词只注入可用知识库目录；模型必须通过动态工具 `query_knowledge_base` 检索内容；工具侧再次校验运行时白名单。
- Markdown 输出协议：会话级 Markdown 开关通过调度器向当前用户消息追加输出指令；保存助手消息时按会话配置剥离或保留 Markdown；前端 Markdown 模式使用 `markdown-it` 渲染。
- 工具审计协议：高风险工具在执行前进入 AuditMiddleware；审计结果可放行、要求动态确认、拒绝或按风险阈值拦截。
- 数据隔离协议：认证用户由 `get_current_user` 解析；消息、会话、工具执行、临时目录与日志链路携带 uid/session_id。
- API 响应协议：管理类接口统一使用 StandardResponse；对话补全接口返回 OpenAI 风格 LLMResponse。
- 国际化协议：后端基于 `Accept-Language` 设置请求语言；WebSocket 在对应 handler 中单独设置语言上下文；前端通过 vue-i18n 管理多语言词条。

## 10. 前端展现层 (`dashboard`)

前端为基于 Vue 3、Vue CLI、Element Plus、Vue Router、Pinia、axios、vue-i18n、markdown-it、highlight.js 的管理控制台。

### 10.1 前端入口与基础设施

- `dashboard/src/main.js`: Vue 应用入口，注册 Pinia、Router、Element Plus、Element Plus Icons 与 i18n。
- `dashboard/src/App.vue`: 根组件。
- `dashboard/src/router/index.js`: 前端路由。
- `dashboard/src/api/index.js`: axios API 客户端封装。
- `dashboard/src/constants/index.js`: 前端默认配置常量。
- `dashboard/src/utils/index.js`: 前端通用工具函数。

### 10.2 前端页面 (`dashboard/src/views`)

- `ChatView.vue`: 聊天主界面。
  - 支持会话列表、附件展示、图片预览、Markdown/纯文本切换、流式/非流式切换。
  - Markdown 渲染使用 markdown-it、highlight.js 与 github-markdown-css。
  - 链接渲染使用 Element Plus Link 风格；禁用 linkify-it 裸域名模糊识别，避免中文前缀与域名连写时误识别。
- `KnowledgeBase.vue`: 知识库管理与检索测试页面。
- `ProfilesView.vue`: Profile 配置管理页面。
- `ChannelsView.vue`: 模型渠道管理页面。
- `PromptsView.vue`: PromptLibrary 管理页面。
- `UsersView.vue`: 用户管理页面。
- `HistoryLogs.vue`: 历史系统日志页面。
- `RealTimeLogs.vue`: 实时日志页面。
- `LoginView.vue`: 登录页面。

### 10.3 前端组件与组合式逻辑

- `dashboard/src/components/`
  - `BaseDataTable.vue`: 通用数据表格。
  - `LanguageSwitcher.vue`: 语言切换组件。
  - `StatusTag.vue`: 状态标签。
  - `VirtualizedCode.vue`: 长文本/代码虚拟化展示。
- `dashboard/src/composables/`
  - `useDeleteConfirm.js`: 删除确认逻辑。
  - `useResizeObserver.js`: ResizeObserver 封装。
  - `useToolParser.js`: 工具调用解析。
  - `useWebSocket.js`: WebSocket 基础封装。
  - `chat/useChatSession.js`: 聊天聚合组合式入口。
  - `chat/useChatState.js`: 聊天状态。
  - `chat/useChatTransport.js`: HTTP/WebSocket 传输。
  - `chat/useMessageProcessor.js`: 消息处理。
  - `chat/useSessionManager.js`: 会话管理。
- `dashboard/src/i18n/`
  - `index.js`: 前端 i18n 初始化。
  - `locales/zh` 与 `locales/en`: chat、common、historyLogs、knowledgeBase、login、profiles、prompts、channels、realTimeLogs、users 语言包。
- `dashboard/src/assets/`
  - `css/`: 前端样式。
  - `svg/`: 图标资源。

## 11. 测试与质量保障

- `tests/conftest.py`: 测试公共夹具与环境配置。
- `tests/initialization/`
  - `test_db_init.py`: 数据库初始化与系统种子数据测试。
- `tests/integration/`
  - `test_auth.py`
  - `test_chat.py`
  - `test_profiles.py`
  - `test_prompts.py`
  - `test_channels.py`
  - `test_users.py`
- `tests/unit/`
  - 核心配置、上下文、调度器、安全、审计、工具、Channel、Schema、Transformer、Embedding、Rerank、混合检索与 Markdown 响应处理单元测试。
  - 包含 Firecrawl 工具原生异步调用测试、知识库工具测试、Shell 工具测试、OpenAI Transformer 流式/Embedding/Rerank 测试等。
- 代码质量：
  - Python 使用 Ruff 检查与安全自动修复。
  - 前端通过 `npm --prefix dashboard run build` 验证构建。
  - 文档更新必须基于当前源码结构与实际文件列表。