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

### 2.2 逻辑调度层 (Control Layer)
- app/core/dispatcher: 核心调度器。负责 Agent 状态管理、工具调用链编排及安全审计决策。
- app/core/context: 上下文管理器。负责 Token 预估、窗口截断及工具调用状态的序列化与还原。
- app/core/log: 全局日志系统。支持按照 UID 或会话 ID 进行分级日志记录。
- app/core/security: 安全防护层。执行 UID 级联数据隔离校验。
- app/core/middleware/auditor.py: 安全审计逻辑。负责评估代码及指令的潜在风险分值与原因。
- app/core/exceptions: 异常体系。封装 StandardResponse 友好的业务异常基类。
- app/core/utils: 工程工具集。核心功能包含 config.py 中的配置泵（Standardization Pump）。
- app/adapters：外部资源适配层。作为系统与异构外部服务通信的桥梁，负责抹平官方 SDK 或私有客户端协议之间的接口差异，实现外部依赖的透明化接入与标准契约对齐。

### 2.3 协议转换层 (Transformer Layer)
- app/transformers/base.py: 协议转换抽象基类。定义厂商无关的统一生成接口与编解码契约。
- app/transformers/openai.py: OpenAI 协议适配实现。负责将 Internal 系列消息模型与 OpenAI 协议进行双向映射。

### 2.4 数据驱动层 (Provider Layer)
- app/providers/llm/client.py: 厂商中立调用客户端。持有 Transformer 注册表，负责根据 Profile 配置动态路由至具体的协议适配器，并执行最终的 InternalResponse 封装。
- app/providers/database.py: 异步数据库引擎。负责 SQLAlchemy 异步 Engine 的初始化、Session 工厂管理以及基于环境（如 Pytest）的动态数据库连接路由。
- app/providers/init_db.py: 系统初始化引导。负责物理数据库表的创建（Migration）以及默认 Profile、Prompt 模板的种子数据植入（Seeding）。

### 2.5 领域模型与验证层 (Domain & Schema Layer)
- app/models/: 基于 SQLModel 的物理实体定义。负责映射数据库表结构、定义字段约束（如 Unique, Index）以及声明实体间的关联关系（Relationship）。
- app/models/message.py: 系统核心领域模型。定义了跨层通信的 InternalMessage 与 InternalResponse 交换标准。
- app/schemas/: 基于 Pydantic 的逻辑验证契约。负责 API 输入输出的数据校验、敏感字段过滤（如密码隐藏）及业务配置档（ProfileConfig）的嵌套结构验证。
- app/schemas/response.py: 统一响应契约。定义了 StandardResponse 标准结构，确保所有 API 输出符合前端预期的分级反馈格式。
- app/schemas/auth.py: 身份验证契约。专门负责 JWT 签发、令牌解析以及登录请求的数据验证。

### 2.6 资源执行层 (Execution Layer)
- app/core/tools/shell.py: Shell 指令原子执行器。负责受控环境下的子进程调用与执行超时控制。

### 2.7 全局配置与常量 (Constants Layer)
- app/core/constants.py: 统一的消息资产库。定义了系统中所有面向用户的异常消息。

### 2.8 核心 CRUD 层
- app/core/crud: 该层作为业务逻辑与数据持久化之间的缓冲，负责屏蔽复杂的数据库查询细节。

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
