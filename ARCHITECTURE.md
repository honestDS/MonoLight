# MonoLight 系统架构设计说明

## 1. 设计哲学
MonoLight 采用“管控分离、协议标准、安全优先”的设计理念。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成，通过解耦系统提示词与模型参数，并集成 Agent 自主执行能力，实现高度灵活的 AI 行为定义。

## 2. 核心分层架构

### 2.1 交互入口层 (Entry Layer)
- app/api/v1/auth: 统一账户中心，包含登录、会话保持及管理员紧急重置逻辑。
- app/api/v1/admin/user: 用户管控中心，仅管理员可访问。
- app/api/v1/chat: 会话引擎，包含 completions, sessions/list, sessions/delete。

### 2.2 逻辑调度层 (Control Layer)
- app/core/dispatcher: 核心调度器。负责 Profile 加载、上下文组装及 Agent 反思循环。
- app/core/exceptions: 全局异常基座。将底层技术报错转换为人类可读的业务文案。
- app/core/security: 安全防护层。负责多租户 UID 隔离校验与权限拦截。

### 2.3 协议转换层 (Transformer Layer)
- app/transformers/openai: 协议适配器。负责将 MonoLight 内部推理结果包装为标准的 OpenAI chat.completion 结构。

### 2.4 数据驱动层 (Provider Layer)
- app/providers/llm: LLM 抽象驱动，支持通过 API 密钥与端点原子化调用不同供应商。
- app/providers/database: 异步数据库驱动。

### 2.5 资源与执行层 (Resource Layer)
- app/core/tools: 工具库。包含物理执行组件（如 ShellExecutor）及其对应的业务逻辑。

## 3. 标准响应协议
系统全量采用 StandardResponse 结构：
- code: 状态码 (200, 4xx, 5xx)
- data: 业务负载
- message: 提示文案（由 core.constants 定义）

## 4. 业务规程
- 权限隔离：所有涉及 Message, Profile, Prompt 的操作必须强绑定 UID。
- 响应闭环：系统内所有响应必须经由 StandardResponse 包装，状态码与业务 code 必须保持一致。
