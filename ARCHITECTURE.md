# MonoLight 系统架构设计说明

## 1. 设计哲学
MonoLight 采用“管控分离、协议标准、安全优先”的设计理念。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成，通过解耦系统提示词与模型参数，并集成 Agent 自主执行能力，实现高度灵活的 AI 行为定义。

## 2. 核心分层架构

### 2.1 API 层 (API Layer)
本层负责处理所有外部请求的鉴权、路由分发与响应包装。详细接口定义请参考 [API.md](./API.md)。

### 2.2 逻辑调度层 (Control Layer)
- app/core/dispatcher: 核心调度器。负责 Profile 加载、上下文组装及 Agent 反思循环。
- app/core/log: 统一日志系统，支持自动路径初始化与分级记录。
- app/core/context: 上下文管理器，负责消息链的截断与窗口控制。
- app/core/security: 安全防护层。负责多租户 UID 隔离校验与权限拦截。

### 2.3 协议转换层 (Transformer Layer)
- app/transformers: 协议转换基座，支持不同模型供应商的输入输出适配。
- app/transformers/openai: 协议适配器。负责将内部推理结果包装为标准的 OpenAI chat.completion 结构。

### 2.4 数据驱动层 (Provider Layer)
- app/providers/llm: LLM 抽象驱动，负责原子化调用不同供应商 API。
- app/providers/database: 异步数据库驱动，集成测试环境隔离机制。

### 2.5 资源与执行层 (Resource Layer)
- app/core/tools: 工具库。包含物理执行组件（如 ShellExecutor）及其对应的业务逻辑。
- app/models: 数据持久化模型定义。
- app/schemas: Pydantic 数据验证与响应 Schema。

## 3. 标准响应协议
系统全量采用 StandardResponse 结构：
- code: 状态码 (200, 4xx, 5xx)
- data: 业务负载
- message: 提示文案（由 app/schemas/response.py 定义）

## 4. 业务规程
- 权限隔离：所有涉及 Message, Profile, Prompt 的操作必须强绑定 UID。
- 响应闭环：系统内所有响应必须经由 StandardResponse 包装。
- 测试优先：核心逻辑必须在 tests 目录下具备对应的单元或集成测试。
