# MonoLight 系统架构设计说明

## 1. 设计哲学
MonoLight 采用“管控分离、协议标准、安全优先”的设计理念。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成，通过解耦系统提示词与模型参数，实现高度灵活的 AI 行为定义。

## 2. 核心分层架构

### 2.0 应用入口
- main.py: 应用入口。负责 FastAPI 实例初始化、中间件（CORS, Auditor）挂载、异常处理器注册以及全局路由集成。

### 2.1 API 层 (API Layer)
负责外部请求的鉴权、路由分发与统一响应包装。
- app/api/v1/auth.py: 用户认证与 JWT 令牌管理。
- app/api/v1/users.py: 账户系统与权限管理。
- app/api/v1/chat.py: 核心对话接口，支持会话上下文的自动装载。
- app/api/v1/profile.py: 模型配置档的 CRUD 与状态切换。
- app/api/v1/providers.py: 模型供应商元数据管理。
- app/api/v1/prompts.py: 提示词资产库维护。
- app/api/v1/files.py: 文件上传与下载管理，支持基于 Session 的临时存储。
- app/api/v1/system.py: 系统监控。支持运行状态获取与系统日志的实时 WebSocket 推送。

### 2.2 逻辑调度层 (Control Layer)
- app/core/dispatcher: 核心调度器。负责 Agent 状态管理、工具调用链编排及安全审计决策。
- app/core/context: 上下文管理器。负责窗口截断及工具调用状态的序列化与还原。
- app/core/log: 全局日志系统。支持按照 UID 或会话 ID 进行分级日志记录。
- app/core/log_broadcaster.py: 日志广播器。基于异步模式将实时日志分发至所有活跃的 WebSocket 订阅者。
- app/core/security: 安全防护层。执行 UID 级联数据隔离校验。
- app/core/middleware/auditor.py: 安全审计逻辑。负责评估代码及指令的潜在风险分值与原因。
- app/core/exceptions: 异常体系。封装 StandardResponse 友好的业务异常基类。
- app/core/utils/: 工具集锦。
  - config.py: 配置中心。实现配置泵（Standardization Pump）机制。
  - tokenizer.py: Token 计算工具。基于加权算法进行 Token 预估。
  - message_parser.py: 消息解析工具。
  - message_assembler.py: 消息装配工具。支持多模态及工具调用消息的标准化封装。
  - dt.py: 时区感知的时间处理工具，支持自定义偏移量。
  - session.py: 会话增强工具。支持基于 LLM 自动生成会话摘要标题。
  - system.py: 系统环境探测工具。获取 CPU、内存、OS 等元数据供 Agent 上下文参考。
- app/adapters: 通信适配层。抹平不同通信协议（如 Web HTTP, WebSocket）与内部调度器之间的差异。
  - app/adapters/base.py: 适配器抽象基类。
  - app/adapters/chat_web.py: 常规 HTTP 对话适配。
  - app/adapters/chat_ws.py: WebSocket 流式对话适配。

### 2.3 向量化服务层 (Embedding Layer)
提供统一的文本向量化能力，支持 RAG 及语义搜索。
- app/embedding/client.py: 向量化统一客户端。
- app/embedding/config.py: 向量化模块独立配置管理。
- app/embedding/utils.py: 向量化通用工具类。
- app/embedding/transformers/: 向量化协议转换。
  - base.py: 向量化转换抽象基类。
  - openai.py: 远程 OpenAI Embedding 协议适配。
  - local.py: 基于 Sentence-Transformers 的本地化模型适配。

### 2.4 协议转换层 (Transformer Layer)
- app/transformers/base.py: 协议转换抽象基类。定义厂商无关的统一生成接口与编解码契约。
- app/transformers/openai.py: OpenAI 协议适配实现。负责将 Internal 系列消息模型与 OpenAI 协议进行双向映射。

### 2.5 数据驱动层 (Provider Layer)
- app/providers/llm/client.py: 厂商中立调用客户端。持有 Transformer 注册表，负责根据 Profile 配置动态路由至具体的协议适配器，并执行最终的 InternalResponse 封装。
- app/providers/database.py: 异步数据库引擎。负责 SQLAlchemy 异步 Engine 的初始化、Session 工厂管理以及基于环境（如 Pytest）的动态数据库连接路由。
- app/providers/init_db.py: 系统初始化引导。负责物理数据库表的创建（Migration）以及默认 Profile、Prompt 模板的种子数据植入（Seeding）。

### 2.6 领域模型与验证层 (Domain & Schema Layer)
- app/models/: 基于 SQLModel 的物理实体定义。
  - user.py: 用户实体。
  - profile.py: 配置档实体。
  - prompt.py: 提示词实体。
  - provider.py: 供应商实体。
  - message.py: 消息实体。定义了 InternalMessage 交换标准。
  - session.py & active_session.py: 对话会话及其活跃状态跟踪实体。
  - system_log.py: 持久化系统运行与审计日志实体。
- app/schemas/: 基于 Pydantic 的逻辑验证契约。负责 API 输入输出的数据校验、敏感字段过滤及业务配置档（ProfileConfig）的嵌套结构验证。
- app/schemas/response.py: 统一响应契约。定义了 StandardResponse 标准结构，确保所有 API 输出符合前端预期的分级反馈格式。
- app/schemas/auth.py: 身份验证契约。专门负责 JWT 签发、令牌解析以及登录请求的数据验证。

### 2.7 资源执行层 (Execution Layer)
- app/core/tools/: 外部资源调用工具链。
  - base.py: 原子工具抽象基类。定义了工具执行的标准接口与环境隔离规范。
  - shell.py: Shell 指令执行器。负责受控环境下的子进程调用与执行超时控制。
  - file_writer.py: 受控文件写入器。支持在安全隔离的临时目录内进行文件操作。

### 2.8 全局配置与常量 (Constants Layer)
- app/core/constants.py: 统一的消息资产库。
- app/core/prompts.py: 系统级提示词模板中心。

### 2.9 核心 CRUD 层
- app/core/crud: 业务逻辑与数据持久化之间的缓冲，采用 Repository 模式。
  - base.py: CRUD 抽象基类，封装通用异步数据库操作。
  - user.py: 用户账户管理。
  - profile.py: 模型配置档管理。
  - prompt.py: 提示词资产管理。
  - provider.py: 模型供应商管理。
  - session.py & active_session.py: 对话会话与上下文索引管理。
  - message.py: 历史消息存储。
  - log.py: 系统与审计日志持久化。

### 2.10 前端展现层 (Presentation Layer)
- dashboard/: 基于 Vue 3 + Element Plus 的管理控制台。提供模型配置可视化、对话测试及系统监控。

## 3. 标准通信规程
- 通信协议：内部组件通信强制使用 InternalMessage 对象，严禁透传原始字典。
- 配置校验：所有配置变更必须通过 ProfileConfig 结构化模型验证。
- 异常反馈：业务逻辑异常统一通过 StandardResponse 返回给前端。
- 审计闭环：高危物理操作（Shell 执行）必须经过中间件审计评分，由调度器决定拦截、确认或放行。

## 4. 质量保障
- 物理证据前置：开发与文档更新必须基于源码事实。
- 自动化测试：集成测试覆盖 API 全流程，单元测试覆盖核心转换与调度逻辑。
- tests/unit/: 单元测试。验证核心组件（如 Transformer 编解码、Security 权限判定）的原子逻辑。
- tests/integration/: 集成测试。覆盖 API 路由、数据库交互及 LLM 调用的端到端链路。
- tests/initialization/: 初始化测试。专门验证数据库种子数据（Seeding）与表迁移（Migration）的正确性。
