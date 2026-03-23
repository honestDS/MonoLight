# MonoLight (原 MonoBot)

MonoLight 不仅仅是一个 AI 框架，它是通往通用人工智能（AGI）交互形态的一次实验。

## 我们的愿景

我们致力于构建一个极简且强大的自主智能体（Autonomous Agent）核心基座。在集成大模型接口、循环反思执行、安全指令审计、RAG 知识检索、Skill 技能插件及定时任务等基础 Agentic 功能之上，MonoLight 的核心目标是打破数字世界与物理世界的边界。

通过内置的多协议适配器，MonoLight 未来将支持从 Web 端、移动端到各类物联网终端的广泛连接。我们最终的图景是让 MonoLight 成为具身智能（Embodied AI）的云端大脑节点，赋予机器逻辑思考与现实任务执行的统一能力。

---

## AI Agent 开发规范守则

> [!IMPORTANT]
> 所有参与本项目贡献的 AI Agent 必须严格遵守以下开发标准与架构原则：
> 1. 阅读并执行 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 中的命名规范、代码风格（Ruff）及测试要求。
> 2. 参考 [ARCHITECTURE.md](./ARCHITECTURE.md) 以确保符合系统设计与模块依赖关系。
> 3. 参考 [API.json](./API.json) 以确保编写集成测试时接口访问的准确性。
> 4. 提交前必须通过 `ruff check` 与 `ruff format` 检查。
> 5. 严禁凭经验编写测试，必须基于源码事实进行 Mock 逻辑对齐。

## 1. 核心特性
- **自主 Agent 架构**: 内置 Dispatcher 循环机制，支持标准协议下的工具调用与决策反思。
- **协议标准化**: 引入 Transformer 层，实现 LLM 协议与内部标准对象（InternalMessage）的解耦转换。
- **安全审计体系**: 支持基于 LLM 的敏感指令实时审计，构建 Agent 执行的物理安全红线。
- **配置驱动设计**: 通过 Profile 系统实现模型参数、采样策略及提供商映射的结构化热管理。
- **物理工具集成**: 支持 ShellExecutor 等核心组件，允许 Agent 在受控环境下执行系统指令。
- **资源高效利用**: 基于 FastAPI、SQLAlchemy 异步模式与 aiohttp 构建的全异步高性能架构。

## 2. 交互入口
- **仪表盘 (Dashboard)**: 基于 Vue 3 + Element Plus 的管理后台，提供可视化配置与实时交互。
- **API 文档**: 内置标准 Swagger 接口 (/docs)，支持多租户鉴权。

## 3. 演进路线
- **具身智能适配器**: 开发支持硬件通信协议的专用适配层，对接机器人与传感器节点。
- **RAG & Rerank**: 集成向量数据库与重排序模型，增强 Agent 的长效知识索引与上下文精炼能力。
- **计划任务系统**: 支持 Cron 驱动的 Agent 自主巡检与循环触发逻辑。
- **Skill 动态加载**: 实现技能库的热插拔与在线插件化生态。
- **多用户隔离环境**: 推进 SSH 级联隔离与容器化执行环境，保障多租户资源安全。

## 4. 技术架构
详细文档请参考 [ARCHITECTURE.md](./ARCHITECTURE.md)

## 自动化测试
项目已接入全量自动化测试体系，包含单元测试、初始化测试及 API 集成测试。
执行命令：`PYTHONPATH=. pytest tests/`

## 开源协议
本项目采用 AGPL-3.0 协议开源。
