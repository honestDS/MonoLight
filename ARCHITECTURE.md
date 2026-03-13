MonoLight 架构说明

一 核心设计理念
MonoLight 采用异步驱动、插件化与层次化设计的 AI 交互框架。其核心目标是实现高性能的消息分发与高度可扩展的 Agent 能力。

二 技术栈
1 Web 框架: FastAPI (基于 main.py 确认)
2 数据库: SQLAlchemy + SQLite (基于 app/providers/database.py)
3 异步 I/O: 核心处理链条全面异步化

三 模块详细职责
1 入口层 (main.py): 初始化 FastAPI 应用，挂载路由，启动数据库连接池。
2 接口层 (app/api): 暴露外部 RESTful 接口，处理 API 级别的数据交互。
3 核心层 (app/core): 包含消息调度器 (dispatcher.py)，负责将接收到的消息分发给对应的处理器。
4 适配层 (app/adapters): 定义消息输入/输出的统一接口，使框架能兼容不同平台。
5 模型层 (app/models): 使用 SQLAlchemy 定义实体，管理数据持久化。
6 转换层 (app/transformers): 处理不同平台消息格式与内部统一格式之间的互转。
7 模式层 (app/schemas): 使用 Pydantic 定义 API 响应与请求的数据契约。
8 服务提供 (app/providers): 封装数据库连接、外部 API 调用等基础设施。

四 数据流向
外部消息 -> 适配器 (Adapters) -> 转换器 (Transformers) -> 调度中心 (Dispatcher) -> 业务处理/Agent -> 响应输出
