# MonoLight 架构文档

## 1. 核心设计理念
MonoLight 采用后端管控、前端透明的设计哲学。系统核心由 Profile 驱动架构与 PromptLibrary 资产库构成。通过解耦系统提示词与模型参数，并集成 Agent 自主执行能力，实现高度灵活的 AI 行为定义与资产复用。

## 2. 目录级分层架构

### 2.1 交互入口层 (Entry Layer)
- **app/api/v1/**: 标准化业务接口。集成了 `UnifiedResponse` 响应包装器。
- **dashboard/**: 前端工程。基于 Vue 3 组合式 API 开发，实现了高度互动的配置管理界面。

### 2.2 核心控制层 (Control Layer)
- **app/core/dispatcher.py**: 调度中心。支持 Agent 循环调用机制，负责 Profile 组装、工具分发及数据持久化。
- **app/core/messages.py**: 业务文案中心。收口全系统成功/失败/异常提示，确保信息的一致性。
- **app/core/security.py**: 安全层。负责 JWT 鉴权、CORS 策略及敏感操作拦截。

### 2.3 资源与执行层 (Provider Layer)
- **app/providers/database.py**: 异步数据库驱动。
- **app/providers/llm/client.py**: LLM 客户端。支持 `aiohttp` 异步请求及 `tools` 参数动态注入。
- **app/core/tools/**: 工具库。包含物理执行组件及其对应的业务逻辑。

### 2.4 数据定义层 (Data Layer)
- **app/models/**: ORM 实体。采用声明式定义，支持多表关联加载 (Joined Load)。
- **app/schemas/**: Pydantic 模型。不仅负责类型校验，更集成了对模型采样参数的物理边界约束。

## 3. 标准响应协议
系统全量采用 `UnifiedResponse` 结构：
- `code`: 状态码 (200, 4xx, 5xx)
- `data`: 业务负载
- `message`: 提示文案（由 core.messages 定义）