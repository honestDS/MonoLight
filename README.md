MonoLight

一 项目简介
MonoLight 是一个基于 Python 构建的现代化 AI 框架重构版本。它继承了 AstrBot 的核心理念，并专注于更高性能、更低耦合的架构设计。

二 核心架构
本项目采用模块化分层设计，主要目录结构如下：
1 app/core: 框架核心逻辑，负责调度与生命周期管理。
2 app/agents: 自主 AI Agent 实现逻辑。
3 app/adapters: 多平台消息适配器，支持扩展不同通讯渠道。
4 app/providers: 外部服务提供商集成。
5 app/models: 数据库模型与数据持久化。
6 app/api: RESTful 接口层，支持外部调用。
7 app/schemas & app/transformers: 数据结构定义与格式转换。

三 环境要求
1 Python 3.10+
2 SQLite (默认数据库)

四 快速开始
1 安装依赖: pip install -r requirements.txt
2 配置文件: 拷贝 .env.example 并修改为 .env
3 启动项目: python main.py

五 协作开发
本项目当前由沉及其邀请的协作者共同维护。

六 许可证
Private Property - All Rights Reserved.
