# MonoLight 架构说明

## 1. 核心设计理念
MonoLight 是一个异步驱动、模块化、分层设计的 AI 交互框架。其目标是实现高性能的消息调度与高度可扩展的 Agent 能力。

## 2. 技术栈
* **Web 框架:** [FastAPI](https://fastapi.tiangolo.com/)
* **运行环境:** Python 3.10+
* **数据库:** SQLAlchemy + SQLite
* **异步 I/O:** 全流程异步处理链

## 3. 模块职责

### 📂 `main.py`
程序入口。初始化 FastAPI 应用，挂载路由，并管理数据库引擎生命周期。

### 📂 `app/core/`
框架大脑。包含 **Dispatcher**（调度器），负责将接收到的消息路由到具体的处理器。

### 📂 `app/adapters/`
多平台统一接口。确保核心逻辑与具体的平台 API（如 QQ, 微信等）解耦。

### 📂 `app/transformers/`
数据转换中心。处理平台特定负载与内部标准化消息格式之间的互转。

### 📂 `app/models/` & `app/providers/`
* **Models:** SQLAlchemy ORM 实体定义。
* **Providers:** 基础设施逻辑，如数据库连接池和外部 API 客户端封装。

### 📂 `app/api/` & `app/schemas/`
* **API:** 外部交互的 RESTful 接口。
* **Schemas:** 用于请求/响应验证的 Pydantic 模型。

## 4. 数据流向
`外部消息` -> `适配器 (Adapters)` -> `转换器 (Transformers)` -> `调度中心 (Dispatcher)` -> `业务逻辑 / Agents` -> `响应输出`
