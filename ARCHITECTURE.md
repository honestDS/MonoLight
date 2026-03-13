# MonoLight 架构文档

## 1. 核心设计理念
MonoLight 采用后端管控、前端透明的设计哲学。系统的核心是 Profile 驱动架构：通过在数据库中定义并激活具体的 Profile，系统会自动补全所有推理参数，将前端调用从繁琐的 API 细节中解放出来。

## 2. 目录级分层架构

### 2.1 交互入口层 (Entry Layer)
该层负责接收所有外部原始信号，并将其转化为系统内部可识别的请求对象。
- **app/api/**: 处理标准的 HTTP RESTful 请求。如 Chat 接口、Profile 管理接口、鉴权接口。
- **app/adapters/**: 处理第三方通讯平台（如 QQ、微信、Webhook）的接入转换。

### 2.2 核心控制层 (Control Layer)
该层是系统的大脑，负责业务逻辑流转、配置注入与会话维持。
- **app/core/dispatcher.py**: 调度中心。协调 Profile 检索、上下文获取、LLM 请求与数据持久化。
- **app/core/context.py**: 上下文管理器。负责 Token 估算、动态回溯（支持 100 条深度）及语义对齐逻辑。
- **app/core/security.py**: 安全策略层。负责密码哈希、JWT 签发与鉴权拦截。

### 2.3 资源与执行层 (Provider Layer)
该层负责与所有外部服务及基础设施进行交互。
- **app/providers/database.py**: 异步数据库连接驱动。
- **app/providers/llm/client.py**: LLM 客户端驱动。支持自动容错与参数动态补全。

### 2.4 数据定义层 (Data Layer)
该层定义了系统中流转的所有数据结构与约束。
- **app/models/**: ORM 实体模型。包括 Profile（配置）、Provider（供应商）与 Message（历史消息）。
- **app/schemas/**: Pydantic 数据模型。负责 API 输入输出的校验。

### 2.5 格式转换层 (Transformer Layer)
- **app/transformers/**: 负责数据映射转换，确保内部数据结构能无缝转换为标准协议格式。