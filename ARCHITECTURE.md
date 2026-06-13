# MonoLight 系统架构设计说明

## 1. 设计哲学
MonoLight 采用“管控分离、协议标准、安全优先”的设计理念。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成，通过解耦系统提示词与模型参数，实现高度灵活的 AI 行为定义。

## 2. 核心分层架构

### 2.0 应用入口
- main.py: FastAPI 应用入口。负责 lifespan 启动流程、通过 `app/providers/database/bootstrap.py` 执行系统数据初始化、日志初始化、CORS 中间件挂载、全局异常处理器注册以及 API 路由集成。
- app/core/paths.py: 数据目录路径定义与初始化工具，负责维护 `data/`、SQLite 数据库路径与 ChromaDB 持久化路径。

### 2.1 API 层 (API Layer)
负责外部请求鉴权、路由分发与统一响应包装。
- app/api/v1/auth.py: 登录认证、JWT 令牌签发与管理员账号重置。
- app/api/v1/users.py: 用户账户管理，包含新增、列表、更新与删除。
- app/api/v1/chat.py: 核心对话接口，提供 HTTP Chat Completions、WebSocket 流式对话、会话列表、历史、删除、Markdown 设置与标题生成；标题生成基于当前 ProfileConfig 读取对话供应商，并拦截仅用于 Embedding 的供应商。
- app/api/v1/profile.py: Profile 的创建、列表、激活、更新与删除。
- app/api/v1/providers.py: 模型供应商元数据管理，支持 CHAT/EMBEDDING 用途区分，并提供向量维度检测接口。
- app/api/v1/prompts.py: PromptLibrary 提示词资产维护。
- app/api/v1/files.py: 文件上传与下载管理，支持对话附件路径传递。
- app/api/v1/knowledge_base.py: 知识库 API，负责知识库 CRUD、文档导入、文本分块、向量写入、检索测试、文档查看与删除。
- app/api/v1/system.py: 系统监控与日志接口，支持系统日志查询与实时 WebSocket 推送。

### 2.2 通信适配层 (Adapter Layer)
抹平 HTTP 与 WebSocket 通信协议差异，将外部请求转换为统一调度器调用。
- app/adapters/base.py: 对话适配器抽象基类。
- app/adapters/chat_web.py: 常规 HTTP 对话适配。
- app/adapters/chat_ws.py: WebSocket 流式对话适配。

### 2.3 逻辑调度层 (Control Layer)
- app/core/dispatcher.py: 核心对话调度器。负责非流式与流式调度、会话锁状态机、活跃 Profile 获取、动态工具清单与知识库白名单获取、工具调用循环、追加消息合并、最大工具轮次控制与结果落库。
- app/core/context.py: 上下文管理器。负责按照 Profile 上下文窗口配置装载与截断历史消息。
- app/core/log.py: 全局日志系统。基于 loguru 记录运行日志、工具调用日志，并写入 WebSocket 广播器与数据库日志。
- app/core/log_broadcaster.py: 日志广播器。将实时日志异步分发给活跃 WebSocket 订阅者。
- app/core/security.py: 认证与用户安全模块。负责密码哈希、JWT 创建与当前用户解析。
- app/core/middleware/auditor.py: 工具安全审计模块。对 `execute_shell` 与 `write_file` 等高风险工具调用执行 LLM 风险评分、动态确认 Token 校验、拦截或放行决策。
- app/core/exceptions.py: 业务异常体系。封装认证、权限、资源、参数、服务端与 LLM 异常。
- app/core/utils/dispatcher/: 调度器子流程模块集合，将 Profile 校验、系统提示词注入、可用知识库目录注入、会话 Markdown 指令注入、消息准备、追加消息合并、并行工具限流、工具审计、工具运行时上下文传递、Markdown 响应处理、消息保存等逻辑拆分为单一职责函数。
- app/core/utils/config.py: 配置标准化工具，实现扁平配置到 ProfileConfig 嵌套结构的标准化映射。
- app/core/utils/tokenizer.py: Token 估算工具。
- app/core/utils/message_parser.py: 消息解析工具。
- app/core/utils/message_assembler.py: 多模态与附件消息装配工具。
- app/core/utils/text_splitter.py: 知识库文本分块工具，支持段落切分、长文本硬切分与重叠窗口。
- app/core/utils/time.py: 时区感知的时间工具。
- app/core/utils/session.py: 会话标题生成工具，基于 LLM 生成摘要标题。
- app/core/utils/system.py: 系统环境探测工具，为 Shell 工具结果补充 OS、CPU、内存等上下文。

### 2.3.1 对话增强子流程
- 知识库上下文注入：`inject_system_prompt.py` 会查询当前 Profile 可用知识库，使用 `KNOWLEDGE_BASES_WRAPPER` 以结构化 JSON 注入系统提示词，只暴露知识库元数据，不直接注入文档内容。
- 会话 Markdown 指令：`markdown_instruction.py` 根据会话 `enable_markdown` 设置，将 Markdown 输出要求追加到当前用户消息，支持文本与多模态消息结构。
- 动态工具暴露：`app/core/tools/__init__.py` 根据 Profile 可用知识库动态添加 `query_knowledge_base` 工具，并为 `knowledge_base_id` 注入 enum 白名单与描述映射。
- 工具运行时上下文：`BaseExecutor.set_runtime_context` 向工具执行器传递 db、profile、session_id 与 allowed_knowledge_base_ids；`process_single_tool.py` 在执行工具前设置该上下文。

### 2.4 协议转换层 (Transformer Layer)
- app/transformers/base.py: 协议转换抽象基类集合，包含 `BaseTransformer` 对话能力抽象与 `BaseEmbeddingTransformer` 向量化能力抽象，避免强制所有对话协议实现 Embedding 能力。
- app/transformers/openai.py: OpenAI 兼容协议适配实现。负责 InternalMessage 与 OpenAI Chat Completions 消息的双向映射，支持非流式生成、SSE 流式生成、Embeddings 调用、批量向量化、Embedding Base URL 规范化与动态维度回退。

### 2.5 模型与数据供应层 (Provider Layer)
- app/providers/llm/client.py: LLM 统一客户端。持有 Transformer 注册表，根据协议名称路由到具体 Transformer，并将原始模型响应封装为 InternalResponse。
- app/providers/embedding/client.py: Embedding 统一客户端。持有 `BaseEmbeddingTransformer` 注册表，根据 ProviderType 路由到具体 Embedding Transformer。
- app/providers/rerank/client.py: Rerank 统一客户端。持有 `BaseRerankTransformer` 注册表，根据 ProviderType 路由到具体的重排服务实现，支持对检索结果文本进行精排（Rerank）。
- app/providers/database/: 关系型数据库 Provider 包。
  - __init__.py: 对外导出 `AsyncSessionLocal`、`Base`、`DATABASE_URL`、`engine` 与 `get_db`，保持常用数据库依赖导入入口简洁。
  - client.py: 异步数据库引擎。负责 SQLAlchemy/SQLModel 异步 Engine、Session 工厂、测试环境数据库 URL 切换与 FastAPI 依赖注入。
  - bootstrap.py: 系统数据库引导。负责数据库结构同步、默认 Prompt、默认 Profile 与配置补全等启动期初始化流程。
- app/providers/vector/: 向量数据库 Provider 包。
  - __init__.py: 对外导出 Chroma collection 管理函数。
  - chroma.py: ChromaDB 持久化客户端。负责 collection 创建、获取、删除、向量条目删除与条目读取。
- requirements.txt: Provider 与检索链路依赖包括 `chromadb`、`rank-bm25` 与 `jieba`，分别支撑向量持久化、BM25 稀疏检索和中英文分词。

### 2.6 知识库与向量检索层 (Knowledge Base & Vector Layer)
知识库流程由 API、领域模型、文本分块、Embedding Provider、Vector Provider、混合检索与重排精排（Rerank）模块共同组成。向量化与重排能力分别通过 `EmbeddingClient` 与 `RerankClient` 分发到兼容协议实现。
- app/api/v1/knowledge_base.py: 编排知识库业务流程，调用 Profile 中的 embedding_provider_id、embedding_model_id 与 embedding_dimensions 配置完成文档导入、向量写入、检索测试与文档管理。
- app/core/embedding/knowledge_base.py: 知识库核心业务函数。负责读取 Profile 绑定的向量模型配置、调用 EmbeddingClient 生成向量、查询当前 Profile 可用知识库、构造知识库提示词清单与执行知识库查询。
- app/core/rerank/: 检索精排子系统。在初步混合检索并得出 RRF 融合候选集后，当 Profile 配置了有效的重排提供商时，负责将候选文本和原查询送入远程 Reranker 打分，实现更精准的内容筛选与排序。
- app/core/retrieval/: 检索子系统。
  - hybrid.py: 稠密向量检索与 BM25 稀疏检索并发执行，并通过 RRF 融合结果。
  - sparse.py: BM25 稀疏检索实现。
  - tokenizer.py: 中英文混合分词工具。
  - fusion.py: Reciprocal Rank Fusion 排名融合。
  - schemas.py: 检索 Chunk 与 Hit 内部结构，支持记录 RRF 融合分与 Rerank 评分。
- app/core/utils/text_splitter.py: 文档导入时的文本分块组件。
- app/core/tools/knowledge_base_query.py: Agent 知识库查询工具执行器，运行时注入知识库白名单与数据库/Profile 上下文，强制使用内部固定 top_k，并只返回来源与内容，避免向模型暴露冗余检索元数据。
- app/models/knowledge_base.py: 定义 KnowledgeBase、KnowledgeBaseDocument 以及知识库请求/响应模型。
- app/providers/embedding/client.py: Embedding 统一调用入口。
- app/providers/vector/chroma.py: ChromaDB 持久化向量集合管理。
- app/transformers/base.py 与 app/transformers/openai.py: Embedding 抽象与 OpenAI 兼容 Embeddings 协议实现。

### 2.7 领域模型与验证层 (Domain & Schema Layer)
- app/models/: 基于 SQLModel/Pydantic 的数据库实体、内部消息对象与 API 请求响应模型。
  - user.py: 用户实体与用户创建、更新、响应模型。
  - provider.py: 模型供应商实体，包含 ProviderType 与 ModelUsage，其中 ModelUsage 区分 CHAT、EMBEDDING 与 RERANK。
  - profile.py: Profile 实体与 ProfileConfig 嵌套配置模型，包含 provider、security、tool、other 四类运行时配置；ProviderConfig 涵盖了嵌入配置及 Rerank 远程调用的关联与超时配置。
  - prompt.py: PromptLibrary 提示词资产实体。
  - message.py: InternalMessage、InternalToolCall、InternalResponse、消息持久化实体与 ChatCompletionRequest。
  - session.py & active_session.py: 对话会话与活跃会话锁实体。
  - knowledge_base.py: 知识库、知识库文档与知识库接口模型。
  - system_log.py: 系统运行日志与审计日志实体。
- app/schemas/response.py: 统一响应契约，定义 StandardResponse、PageData、LLMResponse 等前后端通信结构。
- app/schemas/auth.py: 身份验证请求契约。

### 2.8 资源执行层 (Execution Layer)
- app/core/tools/: Agent 外部资源调用工具链。
  - __init__.py: 工具 Schema 注册表与工具执行器映射。
  - base.py: 原子工具抽象基类，定义工具执行接口、用户临时目录隔离基础能力与工具运行时上下文注入入口。
  - shell.py: Shell 指令执行器，在 `temp/temp_{uid}` 下运行命令，读取 Profile 超时配置，并返回标准 JSON 结果与系统信息。
  - file_writer.py: 受控文件写入器。
  - firecrawl_search.py: Firecrawl 搜索工具。
  - firecrawl_scrape.py: Firecrawl 网页抓取工具。
  - knowledge_base_query.py: 知识库查询工具，校验运行时知识库白名单，归一化 LLM 传入的 knowledge_base_id，并返回精简来源与内容。

### 2.9 全局配置与杂项子系统 (Constants & Utilities Layer)
- app/core/constants.py: 统一错误消息、提示消息与常量定义。
- app/core/prompts.py: 系统级提示词模板中心，包含审计提示词、确认提示词、工具轮次限制提示等。
- app/core/i18n/: 国际化多语言翻译引擎。基于 `contextvars` 在中间件/生命周期注入当前会话/请求语言，通过动态加载语言包与缺失回退机制，实现业务错误码、文案提示与异常描述的多语言自动翻译。

### 2.10 核心 CRUD 层
- app/core/crud/: 业务逻辑与关系型数据库持久化之间的 Repository 层。
  - base.py: CRUD 抽象基类，封装通用异步数据库操作。
  - user.py: 用户账户管理。
  - profile.py: Profile 管理与激活配置查询。
  - prompt.py: PromptLibrary 管理。
  - provider.py: 模型供应商管理与按名称查询。
  - session.py & active_session.py: 对话会话管理与活跃会话锁管理。
  - message.py: 历史消息存储、分页查询、会话列表与会话删除。
  - log.py: 系统日志与审计日志持久化。

### 2.11 前端展现层 (Presentation Layer)
- dashboard/: 基于 Vue 3、Vue CLI、Element Plus、Vue Router、Pinia、axios 与 vue-i18n 的管理控制台。
- dashboard/src/views/: 页面视图包括 ChatView、KnowledgeBase、ProfilesView、ProvidersView、PromptsView、UsersView、HistoryLogs、RealTimeLogs 与 LoginView。
- dashboard/src/api/index.js: 前端 API 客户端封装。
- dashboard/src/components/: 通用表格、状态标签、虚拟化代码展示以及 LanguageSwitcher 多语言切换等组件。
- dashboard/src/composables/: CRUD、删除确认、WebSocket、工具解析、聊天相关组合式逻辑。
- dashboard/src/i18n/: 前端国际化语言包管理模块，负责多语言词条挂载与切换逻辑。

## 3. 标准通信规程
- 内部对话协议：调度器、LLMClient 与 Transformer 之间使用 InternalMessage、InternalToolCall、InternalResponse，避免在核心链路直接透传前端原始结构。
- 模型供应商协议：当前注册的对话模型协议为 OpenAI 兼容协议，由 LLMClient 分发到 OpenAITransformer；Embedding 能力通过 EmbeddingClient 分发到实现了 BaseEmbeddingTransformer 的具体 Transformer。
- 知识库工具协议：系统提示词只注入可用知识库目录；模型必须通过动态暴露的 `query_knowledge_base` 工具检索内容，工具侧再次校验运行时白名单。
- Markdown 输出协议：会话级 Markdown 开关通过调度器在当前用户消息上追加系统指令，保存助手消息时再进行 Markdown 响应处理，保证前端显示设置与模型输出约束一致。
- 配置校验：Profile 运行时配置必须通过 ProfileConfig 标准化与校验后使用。
- API 响应：管理类接口统一使用 StandardResponse；对话补全接口返回 OpenAI 风格的 LLMResponse。
- 审计闭环：高风险工具调用在调度器工具执行前进入 AuditMiddleware；审计结果可直接放行、要求动态 Token 确认、因 Token 不匹配拒绝或按高风险分值拦截。
- 数据隔离：认证用户由 get_current_user 解析；消息、会话、工具临时目录等链路携带 uid/session_id 进行隔离和日志追踪。

## 4. 质量保障
- 物理证据前置：开发与文档更新必须基于源码事实。
- 自动化测试：集成测试覆盖 API 全流程，单元测试覆盖核心转换与调度逻辑。
- tests/unit/: 单元测试。验证核心配置、上下文管理、调度器、安全、审计、工具、Provider、Schema 与 Transformer 的原子逻辑。
- tests/integration/: 集成测试。覆盖认证、用户、对话、Profile、Prompt、Provider 等 API 与数据库交互链路。
- tests/initialization/: 初始化测试。验证数据库结构同步与系统种子数据初始化。
