# MonoLight 系统架构设计说明

## 1. 设计哲学
MonoLight 采用“管控分离、协议标准、安全优先”的设计理念。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成，通过解耦系统提示词与模型参数，实现高度灵活的 AI 行为定义。

## 2. 核心分层架构

### 2.1 API 层 (API Layer)
负责外部请求的鉴权、路由分发与统一响应包装。
- app/api/v1/auth.py: 用户认证与 JWT 令牌管理。
- app/api/v1/users.py: 账户系统与权限管理。
- app/api/v1/chat.py: 核心对话接口，支持会话上下文的自动装载。
- app/api/v1/profile.py: 模型配置档的 CRUD 与状态切换。
- app/api/v1/providers.py: 模型供应商元数据管理。
- app/api/v1/prompts.py: 提示词资产库维护。

### 2.2 逻辑调度层 (Control Layer)
- app/core/dispatcher: 核心调度器。实现 Agent 的多轮反思循环与工具调用决策。
- app/core/context: 上下文管理器。负责 Token 预估、窗口截断及工具调用状态的序列化与还原。
- app/core/log: 全局日志系统。支持按照 UID 或会话 ID 进行分级日志记录。
- app/core/security: 安全防护层。执行 UID 级联数据隔离校验。
- app/core/middleware: 中间件逻辑。auditor.py 提供基于 LLM 的敏感指令风险审计。
- app/core/exceptions: 异常体系。封装 StandardResponse 友好的业务异常基类。
- app/core/utils: 工程工具集。核心功能包含 config.py 中的配置泵（Standardization Pump）。

### 2.3 协议转换层 (Transformer Layer)
- app/transformers/base.py: 定义 Transformer 抽象基类与标准交换模型 InternalMessage / InternalResponse。
- app/transformers/openai.py: 适配 OpenAI Chat Completion 协议，实现端到端的双向协议转换。

### 2.4 数据驱动层 (Provider Layer)
- app/providers/llm/client.py: 通用 LLM 客户端，集成了 Transformer 路由逻辑。
- app/providers/database.py: 异步数据库基座，管理 SQLAlchemy 异步 Session 生命周期。
- app/providers/init_db.py: 系统引导逻辑。负责默认 Profile 与 Prompt 的数据库种子化（Seeding）。

### 2.5 领域模型与验证层 (Domain & Schema Layer)
- app/models/: SQLAlchemy 物理模型定义。
  - user.py, profile.py, provider.py, prompt.py, message.py
- app/schemas/: Pydantic 数据验证模型。
  - profile.py (包含嵌套的 ProfileConfig 模型), response.py, message.py 等。

### 2.6 资源执行层 (Execution Layer)
- app/core/tools/shell.py: Shell 指令执行器。支持超时控制与基于审计评分的安全执行。

## 3. 标准通信规程
- 通信协议：内部组件通信强制使用 InternalMessage 对象，严禁透传原始字典。
- 配置校验：所有配置变更必须通过 ProfileConfig 结构化模型验证。
- 异常反馈：业务逻辑异常统一通过 StandardResponse 返回给前端。
- 审计闭环：高危物理操作（Shell 执行）必须经过中间件审计评分，由调度器决定拦截、确认或放行。

## 4. 质量保障
- 物理证据前置：开发与文档更新必须基于源码事实。
- 自动化测试：集成测试覆盖 API 全流程，单元测试覆盖核心转换与调度逻辑。
