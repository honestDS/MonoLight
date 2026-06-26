# MonoLight 系统架构设计说明

## 1. 设计目标

MonoLight 采用“管控分离、协议标准、安全优先”的架构思路。系统以 Profile 驱动运行配置，以 Channel 承载模型能力，以 Prompt 作为提示词资产，以统一调度器串联对话、工具、知识库与后台任务。前端协议、模型厂商协议、工具执行协议、检索协议彼此隔离，便于独立演进。

## 2. 总体目录结构

当前项目由后端 FastAPI 应用、前端 Vue 管理控制台、测试套件与运行配置组成。

- `main.py`：FastAPI 应用创建、生命周期管理与启动入口。
- `app/`：后端应用主体。
  - `api/`：HTTP 与 WebSocket API 路由。
  - `adapters/`：聊天协议适配层。
  - `core/`：调度、路由、上下文、安全、加密、日志、检索、审计、国际化、工具与通用函数。
  - `models/`：SQLModel/Pydantic 领域模型与内部消息模型。
  - `providers/`：数据库、LLM、Embedding、Rerank、Vector、图像生成基础设施适配。
  - `schemas/`：API 请求与统一响应契约。
  - `transformers/`：模型协议转换器。
  - `handler.py`：异常处理、中间件与根路由注册。
  - `tasks.py`：后台周期任务。
  - `warning_filters.py`：运行时告警过滤。
- `dashboard/`：前端管理控制台。
- `tests/`：初始化、集成与单元测试。
- `requirements.txt`：后端依赖。
- `pytest.ini`：测试配置。
- `ruff.toml`：Ruff 代码检查配置。

## 3. 后端分层架构

### 3.1 应用入口与生命周期

- `main.py`
  - 创建 FastAPI 应用。
  - 在 `lifespan` 中初始化数据库结构与系统种子数据。
  - 恢复未完成的后台任务。
  - 初始化日志系统并启动日志清理、临时目录清理任务。
  - 注册 API Router 与全局 Handler，业务 API 统一挂载在 `/api/v1` 下。
  - 通过 `APP_PORT` 环境变量启动 Uvicorn。
- `app/handler.py`
  - 注册 `/` 与 `/favicon.ico`。
  - 注册 CORS 中间件。
  - 注册 HTTP 中间件 `locale_middleware`，基于 `Accept-Language` 设置当前请求语言。
  - 注册 SQLAlchemy、Starlette HTTP、Pydantic Validation、业务异常与兜底异常处理器。
  - LLM 异常以 OpenAI Chat Completion 风格错误消息返回。
- `app/tasks.py`
  - `background_log_cleaner`：按保留天数清理系统日志。
  - `background_temp_cleaner`：按 Profile 配置清理临时目录。
- `app/core/paths.py`
  - 统一维护数据目录、临时目录、SQLite 数据库路径、ChromaDB 持久化路径、日志路径与 favicon 路径。
- `app/core/crypto.py`
  - 使用环境变量 `MONOLIGH_ENCRYPTION_KEY` 提供 32 字节密钥。
  - 通过 XOR + Base64 对渠道 API Key 做轻量加密存储，并以 `enc:v1:` 前缀标识当前密文版本。
  - 提供 API Key 解密、加密与日志脱敏工具。

### 3.2 API 层 (`app/api/v1`)

API 层负责外部请求鉴权、路由分发、请求参数接收与统一响应包装。

- `auth.py`：登录认证、JWT 令牌签发与管理员账号重置。
- `users.py`：用户账户管理，实际由 `main.py` 挂载到 `/api/v1/admin/user/*`。
- `chat.py`：核心对话接口。
  - HTTP Chat Completions。
  - WebSocket 流式对话。
  - 会话列表、历史、删除。
  - 会话 Markdown 开关设置。
  - 会话标题生成。
- `profile.py`：Profile 创建、列表、激活、更新与删除。
- `channels.py`：模型渠道管理。
  - Channel 维护 `model_ids` 模型条目列表，按 `CHAT`、`EMBEDDING`、`RERANK`、`IMAGE_GENERATION` 区分用途。
  - 创建、更新、删除渠道时校验模型条目、Rerank `base_url`、渠道名称与 URL 协议。
  - 渠道模型 ID 重命名时同步 Profile 中的渠道规则与审计模型引用。
  - 渠道模型条目删除或渠道删除时清理 Profile 中失效的渠道规则与审计模型引用。
  - 提供向量维度检测接口。
- `prompts.py`：PromptLibrary 提示词资产维护。
- `files.py`：文件上传与下载管理，支持对话附件传递。
- `knowledge_base.py`：知识库 CRUD、文档导入、文本分块、向量写入、检索测试、文档查看与删除。
- `system.py`：系统日志查询、实时日志 WebSocket 推送与后端可用语言列表接口。

### 3.3 通信适配层 (`app/adapters`)

适配层抹平 HTTP 与 WebSocket 差异，将外部请求转换为统一调度器输入。

- `base.py`：对话适配器抽象基类。
- `chat_web.py`：HTTP 非流式对话适配。
- `chat_ws.py`：WebSocket 流式对话适配。

### 3.4 调度控制层 (`app/core`)

- `channel_router.py`：渠道路由器。
  - 基于 Profile 中的 `ChannelConfig.rules` 按优先级分组选择渠道。
  - 同一优先级组内按 `weight` 展开并加权轮询。
  - 通过数据库持久化游标支持多 worker 共享轮询位置。
  - 渠道或模型条目不可用时自动跳过。
- `context.py`：按 Profile 上下文窗口配置装载并截断历史消息。
- `security.py`：密码哈希、JWT 创建与当前用户解析。
- `session_notifier.py`：会话状态通知与广播辅助。
- `exceptions.py`：业务异常体系。
- `prompts.py`：系统级提示词模板中心，包括安全审计、动态确认、工具轮次限制、知识库提示等模板。
- `log.py`：基于 loguru 的日志系统，支持控制台、文件、数据库日志与 WebSocket 广播。
- `log_broadcaster.py`：实时日志广播器。
- `middleware/auditor.py`：工具安全审计模块，对高风险工具进行风险评分与动态确认控制。
- `i18n/`：国际化上下文、语言归一化与翻译加载。
- `background_tasks/`：后台任务状态机、恢复、执行与回复触发。
- `retrieval/`：混合检索、稀疏检索、融合与分词工具。
- `rerank/`：Rerank 统一封装与知识库重排逻辑。
- `embedding/`：知识库向量化辅助逻辑。
- `tools/`：工具执行实现与 Schema 注册。
- `utils/`：通用工具函数与调度器拆分逻辑。

### 3.5 调度器拆分结构 (`app/core/dispatcher.py` / `app/core/dispatchers` / `app/core/utils/dispatcher`)

调度器能力已从单文件拆分为“兼容导出层 + mixin 实现 + 通用 helper”的结构，便于按职责演进。

- `app/core/dispatcher.py`：兼容导出层，继续对外暴露 `ChatDispatcher` 及旧有常用函数入口。
- `app/core/dispatchers/__init__.py`：组合后台、校验、非流式与流式调度能力。
- `app/core/dispatchers/shared.py`：初始消息校验与通用调度前置逻辑。
- `app/core/dispatchers/background.py`：后台主动回复与相关消息流处理。
- `app/core/dispatchers/non_stream.py`：非流式聊天调度主流程。
- `app/core/dispatchers/stream.py`：流式聊天调度主流程。
- `app/core/utils/dispatcher/helpers.py`：调度器共享 helper，包含消息重组、工具结果过滤、文件提取与背景主动回复校验等逻辑。
- `app/core/utils/dispatcher/validate_profile_and_cfg.py`：校验当前 Profile 配置可用性。
- `app/core/utils/dispatcher/inject_system_prompt.py`：注入系统提示词与知识库目录。
- `app/core/utils/dispatcher/markdown_instruction.py`：根据会话 Markdown 偏好生成用户信息附加要求。
- `app/core/utils/dispatcher/prepare_messages.py`：准备聊天请求消息。
- `app/core/utils/dispatcher/append_new_user_messages.py`：追加新用户消息。
- `app/core/utils/dispatcher/fetch_and_merge_new_user_messages.py`：拉取并合并轮次间新用户消息。
- `app/core/utils/dispatcher/handle_parallel_tool_limit.py`：并行工具调用限流与提示。
- `app/core/utils/dispatcher/audit_tool_call.py`：工具调用审计入口。
- `app/core/utils/dispatcher/process_single_tool.py`：单个工具执行、运行时上下文注入与结果处理。
- `app/core/utils/dispatcher/truncate_tool_result.py`：按字符估算 Token 并截断过长工具结果。
- `app/core/utils/dispatcher/process_markdown_response.py`：用于解析 Markdown 的会话内嵌结果标记。
- `app/core/utils/dispatcher/save_message.py`：通用消息保存。
- `app/core/utils/dispatcher/save_initial_message.py`：保存初始用户消息。
- `app/core/utils/dispatcher/mark_initial_message_processed.py`：标记初始消息已处理。
- `app/core/utils/dispatcher/save_assistant_message.py`：保存助手消息并处理会话内 Markdown。
- `app/core/utils/dispatcher/save_tool_response.py`：保存工具响应消息。

### 3.6 通用工具函数 (`app/core/utils`)

- `config.py`：Profile 配置标准化工具。
- `message_assembler.py`：多模态与附件消息装配。
- `message_parser.py`：消息解析。
- `session.py`：会话标题生成工具。
- `system.py`：系统环境探测。
- `text_splitter.py`：知识库文本分块。
- `time.py`：时区感知时间工具。
- `tokenizer.py`：Token 估算工具。

### 3.7 安全审计层 (`app/core/middleware`)

- `auditor.py`：工具安全审计模块。
  - 对 `execute_shell`、`write_file` 等高风险工具进行风险评分。
  - 支持动态确认 Token 校验。
  - 审计结果可直接放行、要求确认、拒绝或按风险分拦截。

### 3.8 国际化层 (`app/core/i18n`)

- `context.py`：使用 `contextvars` 保存当前请求/会话语言。
- `locale.py`：语言标准化。
- `translator.py`：翻译加载、查找与回退。
- `locales/zh` 与 `locales/en`：后端错误码、通用文案、聊天、LLM、Profile、Prompt、Channel、User、Validation 文案。

## 4. 模型协议与基础设施层

### 4.1 Transformer 层 (`app/transformers`)

- `base.py`
  - `BaseTransformer`：对话生成、流式生成、内部消息与厂商协议互转抽象。
  - `BaseEmbeddingTransformer`：Embedding 抽象，包含单次与批量向量化。
  - `BaseRerankTransformer`：Rerank 抽象，包含原始 rerank 调用与文本重排。
  - `BaseImageGenerationTransformer`：图像生成抽象。
- `openai.py`
  - OpenAI 兼容协议实现。
  - 支持 Chat Completions 非流式、SSE 流式、Embeddings、Rerank、Images 兼容调用。
  - 负责 InternalMessage 与 OpenAI 消息格式互转。

### 4.2 基础设施 Provider 层 (`app/providers`)

- `llm/client.py`：LLM 统一客户端，根据 `protocol` 路由到具体 Transformer。
- `embedding/client.py`：Embedding 统一客户端，根据 ChannelType 分发实现。
- `rerank/client.py`：Rerank 统一客户端，根据 ChannelType 分发实现。
- `image_generation/client.py`：图像生成统一客户端，根据 ChannelType 分发实现。
- `database/`
  - `client.py`：SQLAlchemy/SQLModel 异步 Engine、Session 工厂、测试环境数据库 URL 切换与 FastAPI DB 依赖。
  - `bootstrap.py`：数据库结构同步、默认 Prompt、默认 Profile、默认配置补全。
- `vector/chroma.py`：ChromaDB 持久化客户端与 collection 管理函数。

## 5. 知识库、检索与重排层

知识库链路由 API、领域模型、文本分块、Embedding、Vector、Hybrid Retrieval 与 Rerank 共同构成。

- `app/api/v1/knowledge_base.py`：知识库 API 编排入口。
- `app/core/embedding/knowledge_base.py`
  - 读取 Profile 中的嵌入模型配置。
  - 调用 EmbeddingClient 生成向量。
  - 查询当前 Profile 可用知识库。
  - 构造知识库目录提示词。
  - 执行知识库查询。
- `app/core/retrieval/`
  - `hybrid.py`：稠密向量检索与 BM25 稀疏检索并发执行，并通过 RRF 融合。
  - `sparse.py`：BM25 稀疏检索。
  - `tokenizer.py`：中英文混合分词。
  - `fusion.py`：Reciprocal Rank Fusion 排名融合。
  - `schemas.py`：检索 Chunk 与 Hit 内部结构。
- `app/core/rerank/`
  - `knowledge_base.py`：对融合候选进行远程 Reranker 精排，并支持失败降级。
  - `schemas.py`：Rerank 相关内部结构。
- `app/core/tools/knowledge_base_query.py`：Agent 知识库查询工具。
  - 校验运行时知识库白名单。
  - 归一化 LLM 传入的 `knowledge_base_id`。
  - 使用内部固定 `top_k`。
  - 返回精简来源与内容，避免向模型暴露冗余检索元数据。
- `app/models/knowledge_base.py`：知识库、文档与检索测试模型。
- `app/providers/vector/chroma.py`：ChromaDB 向量持久化。
- `requirements.txt`：知识库链路依赖包含 `chromadb`、`rank-bm25`、`jieba`。

## 6. 领域模型与 Schema 层

### 6.1 领域模型 (`app/models`)

- `user.py`：用户实体与用户创建、更新、响应模型。
- `channel.py`：模型渠道实体、模型条目与渠道规则配置。
  - `ChannelType`：模型协议类型，当前包含 `OPENAI`。
  - `ModelUsage`：模型用途，包含 `CHAT`、`EMBEDDING`、`RERANK`、`IMAGE_GENERATION`。
  - `ChannelModelItem`：渠道下单个模型条目的完整配置，包含模型 ID、用途、多模态能力、上下文窗口、生成参数、超时、图片生成参数与向量维度等字段。
  - `ModelChannel`：渠道数据库实体，保存渠道名称、协议类型、加密 API Key、Base URL、启用状态与模型条目列表。
  - `ChannelRule`：Profile 中引用渠道模型条目的路由规则，包含 `channel_id`、`model_id`、`priority`、`weight` 与启用状态。
  - `ChannelConfig`：按用途独立的渠道配置，包含失败重试、各类超时、Rerank 候选数量、知识库返回数量与路由规则。
- `channel_cursor.py`：渠道加权轮询游标实体。
  - `cursor_key` 形如 `{profile_id}:{usage}:{priority}`，用于标识一个优先级组的轮询游标。
  - `position` 保存下一次取用的展开序列下标，支持多 worker 共享轮询位置。
- `profile.py`：Profile 实体与 ProfileConfig 嵌套配置。
  - `channel`：按用途分为 `chat_channel`、`embedding_channel`、`rerank_channel`、`image_generation_channel`，各组使用 `ChannelConfig` 维护路由规则、超时与知识库检索参数。
  - `security`：审计渠道 ID、审计模型 ID 与风险阈值。
  - `tool`：Shell 超时、并发工具数量、最大工具轮次、Firecrawl API Key、工具执行器线程池大小、文件发送限制等工具配置。
  - `other`：杂项系统配置，当前包含 `log_locale` 与 `temp_dir_max_size_mb`。
- `prompt.py`：PromptLibrary 提示词资产实体。
- `message.py`：InternalMessage、InternalToolCall、InternalResponse、消息持久化实体与 ChatCompletionRequest。
- `session.py`：对话会话实体，包含 `enable_markdown`。
- `active_session.py`：活跃会话锁实体。
- `knowledge_base.py`：知识库与知识库文档实体。
- `background_task.py`：后台任务实体与执行状态。
- `system_log.py`：系统运行日志与审计日志实体。

### 6.2 API Schema (`app/schemas`)

- `response.py`：`StandardResponse`、`PageData`、`LLMResponse` 等统一响应结构。
- `auth.py`：身份验证请求契约。

## 7. 资源执行层 (`app/core/tools`)

工具执行层向 Agent 暴露外部资源调用能力。

- `__init__.py`：工具 Schema 注册表与执行器映射；根据 Profile 可用知识库动态暴露 `query_knowledge_base`。
- `base.py`：`BaseExecutor` 抽象基类。
  - 定义异步工具执行接口。
  - 提供 `run_sync` 将同步函数放入线程执行。
  - 提供 `set_runtime_context` 注入 db、profile、session_id、allowed_knowledge_base_ids。
- `shell.py`：Shell 指令执行器，在用户隔离目录下运行命令并返回标准 JSON 结果。
- `file_writer.py`：受控文件写入器。
- `send_file_to_user.py`：将文件回传给前端用户。
- `list_background_tasks.py`：列出后台任务。
- `cancel_background_task.py`：取消后台任务。
- `image_generation.py`：图像生成工具。
- `firecrawl_search.py`：Firecrawl 搜索工具。
- `firecrawl_scrape.py`：Firecrawl 网页抓取工具。
- `knowledge_base_query.py`：知识库查询工具。

## 8. CRUD 层 (`app/core/crud`)

CRUD 层位于业务逻辑与关系型数据库之间，封装通用异步持久化操作。

- `base.py`：CRUD 抽象基类。
- `user.py`：用户账户管理。
- `profile.py`：Profile 管理与激活配置查询。
- `prompt.py`：PromptLibrary 管理。
- `channel.py`：模型渠道管理与按名称查询。
- `channel_cursor.py`：渠道加权轮询游标管理。
- `session.py`：对话会话管理。
- `active_session.py`：活跃会话锁管理。
- `message.py`：历史消息存储、分页查询、会话列表与会话删除。
- `background_task.py`：后台任务存取与状态更新。
- `log.py`：系统日志与审计日志持久化。

## 9. 标准通信规程

- 内部对话协议：调度器、LLMClient、Transformer 与工具链之间使用 InternalMessage、InternalToolCall、InternalResponse，避免直接透传前端或厂商原始结构。
- 模型供应商协议：当前实现以 OpenAI 兼容协议为主，对话通过 LLMClient 按 `protocol` 分发，Embedding、Rerank 与图像生成通过对应客户端按 ChannelType 分发。
- 知识库工具协议：系统提示词只注入可用知识库目录；模型必须通过动态工具 `query_knowledge_base` 检索内容；工具侧再次校验运行时白名单。
- Markdown 输出协议：会话级 Markdown 开关通过调度器向当前用户消息追加输出指令；保存助手消息时按会话配置剥离或保留 Markdown；前端 Markdown 模式使用 `markdown-it` 渲染。
- 工具审计协议：高风险工具在执行前进入 AuditMiddleware；审计结果可放行、要求动态确认、拒绝或按风险阈值拦截。
- 数据隔离协议：认证用户由 `get_current_user` 解析；消息、会话、工具执行、临时目录与日志链路携带 uid/session_id。
- API 响应协议：管理类接口统一使用 `StandardResponse`；对话补全接口返回 OpenAI 风格 `LLMResponse`。
- 国际化协议：后端基于 `Accept-Language` 设置请求语言；WebSocket 通过 `lang` 查询参数在对应 handler 中单独设置语言上下文；前端通过 `vue-i18n` 管理多语言词条，并通过 `/api/v1/system/i18n/locales` 获取后端可用语言列表。

## 10. 前端展现层 (`dashboard`)

前端为基于 Vue 3、Vue CLI、Element Plus、Vue Router、Pinia、axios、vue-i18n、markdown-it、highlight.js 的管理控制台。

### 10.1 前端入口与基础设施

- `dashboard/src/main.js`：Vue 应用入口，注册 Pinia、Router、Element Plus、Element Plus Icons 与 i18n。
- `dashboard/src/App.vue`：根组件。
- `dashboard/src/router/index.js`：前端路由。
- `dashboard/src/api/index.js`：axios API 客户端封装，默认使用 `/api/v1` 基础路径，自动附加 Bearer Token 与 `Accept-Language` 请求头，并维护聊天/系统日志 WebSocket 地址。
- `dashboard/src/constants/index.js`：前端默认配置常量。
- `dashboard/src/utils/index.js`：前端通用工具函数。
- `dashboard/package.json`：前端脚本与依赖，使用 Vue CLI 构建而非 Vite。

### 10.2 前端页面 (`dashboard/src/views`)

- `ChatView.vue`：聊天主界面。
  - 支持会话列表、附件展示、图片预览、Markdown/纯文本切换、流式/非流式切换。
  - Markdown 渲染使用 `markdown-it`、`highlight.js` 与 `github-markdown-css`。
  - 链接渲染使用 Element Plus Link 风格；禁用 `linkify-it` 裸域名模糊识别，避免中文前缀与域名连写时误识别。
- `KnowledgeBase.vue`：知识库管理与检索测试页面。
- `ProfilesView.vue`：Profile 配置管理页面。
- `ChannelsView.vue`：模型渠道管理页面。
- `PromptsView.vue`：PromptLibrary 管理页面。
- `UsersView.vue`：用户管理页面。
- `HistoryLogs.vue`：历史系统日志页面。
- `RealTimeLogs.vue`：实时日志页面。
- `LoginView.vue`：登录页面。

### 10.3 前端组件与组合式逻辑

- `dashboard/src/components/`
  - `BaseDataTable.vue`：通用数据表格。
  - `ChannelEditor.vue`：渠道规则编辑组件，用于在 Profile 中维护按用途分组的渠道路由规则。
  - `LanguageSwitcher.vue`：语言切换组件。
  - `StatusTag.vue`：状态标签。
  - `VirtualizedCode.vue`：长文本/代码虚拟化展示。
- `dashboard/src/composables/`
  - `useDeleteConfirm.js`：删除确认逻辑。
  - `useResizeObserver.js`：ResizeObserver 封装。
  - `useToolParser.js`：工具调用解析。
  - `useWebSocket.js`：WebSocket 基础封装。
  - `chat/useChatSession.js`：聊天聚合组合式入口。
  - `chat/useChatState.js`：聊天状态。
  - `chat/useChatTransport.js`：HTTP/WebSocket 传输。
  - `chat/useMessageProcessor.js`：消息处理。
  - `chat/useSessionManager.js`：会话管理。
- `dashboard/src/i18n/`
  - `index.js`：前端 i18n 初始化。
  - `locales/zh` 与 `locales/en`：`chat`、`common`、`historyLogs`、`knowledgeBase`、`login`、`profiles`、`prompts`、`channels`、`realTimeLogs`、`users` 语言包。
- `dashboard/src/assets/`
  - `css/`：前端样式。
  - `svg/`：图标资源。

## 11. 测试与质量保障

- `tests/conftest.py`：测试公共夹具与环境配置。
- `tests/initialization/test_db_init.py`：数据库初始化与系统种子数据测试。
- `tests/integration/`
  - `test_auth.py`
  - `test_chat.py`
  - `test_profiles.py`
  - `test_prompts.py`
  - `test_providers.py`
  - `test_users.py`
- `tests/unit/`
  - 核心配置、上下文、调度器、安全、审计、渠道路由、Provider、工具、Channel、Schema、Transformer、Embedding、Rerank、检索与 Markdown 响应处理单元测试。
  - 包含渠道加权轮询、模型 ID 重命名同步、Rerank 配置校验、OpenAI 流式超时等专项测试。
  - 包含 Firecrawl 工具调用测试、知识库工具测试、Shell 工具测试、OpenAI Transformer 流式/Embedding/Rerank 测试等。
- 代码质量：
  - Python 使用 Ruff 检查与安全自动修复。
  - 前端通过 `npm --prefix dashboard run build` 验证构建。
  - 文档更新应基于当前源码结构与实际文件列表。
