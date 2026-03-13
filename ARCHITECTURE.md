# MonoLight 架构文档

## 1. 核心设计理念
MonoLight 采用后端管控、前端透明的设计哲学。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成。通过解耦系统提示词与模型参数，实现高度灵活的 AI 行为定义与资产复用。

## 2. 目录级分层架构

### 2.1 交互入口层 (Entry Layer)
负责接收外部信号并转化为内部请求对象。
- **app/api/v1/**: 提供基于非 RESTful 风格（仅 GET/POST）的标准化接口。
- **app/adapters/**: 负责第三方通讯平台（如 QQ、微信）的接入与负载转换。

### 2.2 核心控制层 (Control Layer)
系统逻辑中心，处理配置注入与上下文流转。
- **app/core/dispatcher.py**: 调度中心。负责 Profile 与 Prompt 的动态组装、LLM 请求分发及数据持久化。
- **app/core/context.py**: 上下文管理器。负责 Token 估算、动态历史回溯与语义对齐。
- **app/core/security.py**: 安全层。负责 JWT 鉴权与敏感操作拦截。

### 2.3 资源与执行层 (Provider Layer)
负责物理基础设施的 IO 交互。
- **app/providers/database.py**: 异步数据库驱动。
- **app/providers/llm/client.py**: LLM 客户端。支持模型容错与虚提供商（provider_id=-1）拦截。

### 2.4 数据定义层 (Data Layer)
定义系统核心实体与约束。
- **app/models/**: ORM 实体。包含 Profile、PromptLibrary、Provider 与 Message。
- **app/schemas/**: Pydantic 模型。负责全链路数据的强类型校验。

### 2.5 格式转换层 (Transformer Layer)
- **app/transformers/**: 负责协议适配，将内部数据结构转换为 OpenAI 等标准格式。
