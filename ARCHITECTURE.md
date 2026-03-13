# MonoLight 架构文档

## 1. 核心设计理念
MonoLight 采用后端管控、前端透明的设计哲学。系统的核心是 Profile 驱动架构：通过在数据库中定义并激活具体的 Profile，系统会自动补全所有推理参数，将前端调用从繁琐的 API 细节中解放出来。

## 2. 目录级分层架构

### 2.1 交互入口层 (Entry Layer)
该层负责接收所有外部原始信号，并将其转化为系统内部可识别的请求对象。
- **app/api/**: 处理标准的 HTTP RESTful 请求。如 Chat 接口、Profile 管理接口、鉴权接口。
- **app/adapters/**: 处理第三方通讯平台（如 QQ、微信、Webhook）的异步接入。负责解析平台特定负载并调用调度器。

### 2.2 核心控制层 (Control Layer)
该层是系统的大脑，负责业务逻辑的流转与配置的注入。
- **app/core/**: 包含 Dispatcher（调度中心）。它根据当前激活的 Profile 决定如何处理请求，并负责不同层级间的能力协调。

### 2.3 资源与执行层 (Provider Layer)
该层负责与所有外部服务及基础设施进行实际交互。
- **app/providers/**: 包含数据库连接驱动 (database.py) 以及 LLM 客户端驱动 (llm.py)。它封装了所有底层的网络 IO 与数据持久化操作。

### 2.4 数据定义层 (Data Layer)
该层定义了系统中流转的所有数据结构与约束。
- **app/models/**: ORM 实体模型定义，映射数据库表结构。
- **app/schemas/**: Pydantic 验证模型定义，负责 API 输入输出的数据校验与过滤。

### 2.5 格式转换层 (Transformer Layer)
- **app/transformers/**: 负责在不同协议间进行数据映射。如将内部响应转化为标准的 OpenAI 格式响应。