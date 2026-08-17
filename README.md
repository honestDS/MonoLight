# MonoLight

MonoLight 是一个专注于安全执行与人机协同的通用自主智能体（General Autonomous Agent）运行时。

<p align="center">
  <img src="./docs/banner.webp" alt="MonoLight Banner" width="100%" />
</p>

## 我们的愿景

当智能体从回答问题逐渐走向执行命令、修改文件、访问网络和管理长期任务时，一次看似微小的判断错误，都可能对真实系统造成不可逆的影响。MonoLight 关注的核心问题是：当我们赋予 AI 更强自主执行能力时，如何同时保证这种能力是可控、可审计且值得信任的？

MonoLight 不追求让智能体彻底脱离人类，而是希望在人类监督与 AI 自主执行之间建立清晰的安全边界。通过执行前安全审计、人工确认和完整的执行记录，让关键操作在真正发生之前得到审查，并在发生之后能够被追溯。

从日常办公任务，到服务器运维和复杂的自动化操作，MonoLight 致力于将大模型的决策能力转化为现实世界的执行能力，同时尽可能降低自主执行带来的风险。我们希望通过持续的工程实践与安全实验，探索一种更安全、更可控的人机协同 Agent 运行模式。

---

## PR/Issue

> [!IMPORTANT]
> 项目当前处于早期活跃开发阶段，架构仍在快速演进中，暂不接受 Pull Request。欢迎通过 Issue 报告使用中遇到的问题或提出改进建议，你的反馈对我们非常重要。

## 1. 核心特性

MonoLight 不只是一个对话界面或单次工具调用封装，而是一套让 AI 能够持续执行任务、接受人工监督并留下完整记录的自主智能体系统。

- **双模型安全审计**：将任务执行与安全审查分开，主模型负责规划和调用工具，独立配置的审计模型在执行前逐项评估风险；高风险操作会被阻断或生成可读的人工确认卡片，审计结论、确认过程和执行结果均可追溯。
- **长期记忆与用户偏好管理**：支持跨会话长期记忆与用户级个性化偏好管理，结合关系型数据库与 RAG 检索，实现对历史信息与用户偏好的持久化存储、混合检索与上下文关联。
- **全功能 Shell**：除普通的一次性 Shell 命令外，还支持 Windows ConPTY 与 Linux PTY 交互终端。AI 可以持续读取输出、写入后续输入、查询状态、调整终端尺寸并主动关闭会话，能够操作需要 TTY、持续交互或长时间运行的命令行程序。
- **自主与持续任务执行**：支持多轮工具调用、并行工具调用、后台任务和定时任务；耗时工作可以转入后台继续执行，用户可查看或取消任务，并在完成后收到总结回复。
- **知识、网络与文件能力**：支持文档知识库、向量检索与结果重排，并可让 AI 查询知识库、搜索和抓取网页、生成图片、写入文件以及向用户发送文件。
- **多模型协同配置**：可接入并统一管理多个模型渠道，为聊天、上下文总结、知识库重排和图片生成分别选择模型，并通过优先级与权重配置模型路由，不必将所有工作绑定到单一模型。
- **IM 消息平台接入**：无需停留在浏览器中，即可从日常聊天软件使用完整的智能体能力。当前已支持微信 OpenClaw 扫码接入、文本/图片/文件双向收发、连续消息自动合并以及聊天内工具调用和安全确认；后台任务与定时任务完成后可主动回推结果，投递失败会自动重试。
- **完整的可视化工作台**：提供聊天与会话历史、工具执行结果、审计确认卡片、知识库、模型渠道、提示词、定时任务、消息平台以及实时和历史日志管理。
- **多用户与数据隔离**：支持用户与角色管理，并隔离不同用户的会话数据；每个 IM 接入账号还可绑定指定用户与 Profile。
- **自托管与数据掌控**：支持 SQLite、MySQL，可从个人本地部署扩展到多用户部署，模型渠道、提示词和运行数据均由部署者自行管理。

## 已知限制
- 连续对话以上一轮 API 返回的真实 input token 数为基线，增量估算下一轮；运行时提示仅附加到每轮最新用户消息，跨轮时会从旧消息移到新消息，因此下一轮发送前的增量估算会保留上一轮提示的 token，可能略微高估并提前触发上下文总结。真实模型响应返回 usage 后，界面展示会由实际值校正；当前暂不通过额外探测请求校准，以避免额外 token 消耗。
- 审计系统只审查本次直接运行的脚本，不会继续追踪并审查该脚本间接调用、导入或启动的其他脚本。链式审查没有明确边界；当入口脚本涉及大型项目时，可能导致巨量 Token 消耗，因此当前不支持链式审查。

## 2. 交互入口
- **仪表盘 (Dashboard)**: 基于 Vue 3 + Element Plus 的现代管理后台，提供极致流畅的配置与交互体验。
- **API 文档**: 内置 Swagger (/docs)，支持标准的鉴权与业务接口调用。

## 3. 未来规划
- 核心类
  - [ ] **Skill 动态加载**: 实现技能库的热插拔与在线热更新机制。
  - [ ] **全模态支持**: 除了图片、文本、文件的数据传输，还支持视频、音频等多媒体数据的上传与交互。
  - [ ] **SETUP 机制**: 重构并简化安全部署流程，为不熟悉 Agent 相关配置的用户提供更友好的部署体验。
  - [ ] **Agent 自行管理的系统配置工具**： 通过与LLM对话方式，实现对系统配置文件的动态调整与管理。
  - [ ] **WEBUI 数据看板**： 提供实时的系统运行状态、用户会话数据、模型调用统计等信息，帮助管理员监控与管理系统运行。
  - [ ] **思考等级与思维链支持**： 支持用户自定义思考等级并展示思维链。
  - [ ] **定义长期记忆存储标准**： 定义通用的长期记忆存储标准，用于与外部系统进行数据交换。
  - [ ] **支持长期记忆导入/导出**： 支持用户将长期记忆导出为标准格式，或从标准格式导入长期记忆。
- 扩展类
  - [ ] **双主题**： 支持经典/现代主题切换。
  - [ ] **企业级审计系统**: 使用平台自带的双模型审计机制(已实现)，实现企业级的审计报告自动生成与发送，且在发送后立即删除暂存在服务器上的审计数据，避免被恶意篡改或泄露。
  - [ ] **接入 QQ平台 消息适配器**: 实现与 QQ平台 的消息交互功能。
  - [ ] **合并知识库与长期记忆**： 
  这是一个探索性功能，需要进一步研究与实现。 
  主要思想是“知识”也属于“长期记忆”的一种，合并后可减少工具数量，避免相似工具对LLM产生误导。
  - [ ] **主代理的子代理派生**：
  这是一个探索性功能，需要进一步研究与实现。
  主要思想是通过主代理生成临时子代理来执行任务，用于完成可并发的重型任务，子代理在父代理的指导下执行任务。
  需要考虑该功能在当前系统架构下是否有确必要。

## 4. 技术架构
架构文档 [ARCHITECTURE.md](./ARCHITECTURE.md)
开发规范 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)

## 运行服务

> [!IMPORTANT]
新部署首次启动时会自动生成 `data/system_secrets.json`。已有部署在该文件不存在时，会从有效的 `JWT_SECRET_KEY` 与 `MONOLIGH_ENCRYPTION_KEY` 一次性迁移系统密钥；首次成功启动后，请从部署配置中移除这两个旧变量。JWT 算法已在代码中固定为 HS256，无需也不要配置 `JWT_ALGORITHM`。

`data/system_secrets.json` 和 `data/system_secrets.lock` 都必须位于持久化卷中，并限制为仅服务账户可读写。日志和 Setup 流程不会暴露系统密钥。

项目包含 Web 服务和四个独立 Worker：消息平台 Worker、后台任务 Worker、终端 Worker 和会话最终回复 Worker。Web 服务支持多个 Worker，四个后台 Worker 通过数据库租约保证同一数据库范围内各自只有一个有效实例。

在 `.env` 中配置监听地址、端口和 Web Worker 数量：

```dotenv
APP_HOST=0.0.0.0
APP_PORT=8001
APP_WORKERS=1
```

推荐在 Windows、Linux 和 macOS 上使用统一启动器：

```bash
python start.py
```

启动器会在启动 Web 服务及四个后台 Worker 前完成系统密钥与锁文件完整性检查、数据库建表与迁移和系统数据初始化；任一步骤失败都不会启动任何子进程。任一子进程异常退出或收到终止信号时，启动器会关闭其余子进程。

需要分别调试各进程时，先执行一次全局初始化：

```bash
python -c "import asyncio; from start import initialize_system; asyncio.run(initialize_system())"
```

然后分别启动：

```bash
python main.py
python -m app.workers.message_platform
python -m app.workers.background_task
python -m app.workers.terminal
python -m app.workers.session_reply
```

多实例部署时，所有实例必须连接同一个数据库。未取得租约的后台 Worker 会保持待命，并在当前持有者退出或租约过期后自动接管。

## 自动化测试
项目已接入自动化测试体系，涵盖单元测试、初始化逻辑测试以及 API 集成测试。

### 执行测试命令
在项目根目录下执行以下命令运行全量测试：

```bash
PYTHONPATH=. pytest tests/
```

## 项目预览

- 清晰的WEBUI对话页面
<p align="center">
  <img src="./docs/screenshot.png" alt="MonoLight Dialog Page" width="100%" />
</p>

- 丰富的配置
<p align="center">
  <img src="./docs/screenshot2.png" alt="MonoLight Config Page" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot3.png" alt="MonoLight Config Page 1" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot4.png" alt="MonoLight Config Page 2" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot5.png" alt="MonoLight Config Page 3" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot6.png" alt="MonoLight Config Page 4" width="100%" />
</p>

- 详细的实时日志
<p align="center">
  <img src="./docs/screenshot7.png" alt="MonoLight Log Page" width="100%" />
</p>

## AI Agent 开发规范守则

> [!IMPORTANT]
> 所有参与本项目贡献的 AI Agent 必须严格遵守以下开发标准与架构原则：
> 1. 阅读并执行 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 中的命名规范、代码风格（Ruff）及测试要求。
> 2. 参考 [ARCHITECTURE.md](./ARCHITECTURE.md) 以确保符合系统设计与模块依赖关系。
> 2. 在提交任何代码前，必须确保通过 `ruff check` 与 `ruff format` 检查。
> 3. 所有测试用例的编写或修改必须严格基于目标代码的实际实现。在编写测试前，AI Agent 必须完整阅读并解析目标源码，确保 Mock 逻辑与业务流转完全对齐，严禁凭经验或假设编写测试代码。

## 致谢

MonoLight 的诞生离不开开源社区的滋养。在开发过程中，以下项目为我们提供了重要的设计灵感与思路参考：

- **[LinuxDo](https://linux.do)** —— 新的理想型社区。作者本人从该社区获取了大量 AI 相关的知识与灵感。
- **[New API](https://github.com/QuantumNous/new-api)** —— 新一代大模型网关与 AI 资产管理系统。MonoLight 的**模型渠道路由与多模型调度**设计深受其启发。
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** —— 开源一体化 Agentic 聊天机器人平台。MonoLight 的**知识库工具化与 IM 平台接入**思路借鉴了该项目的优秀实践。

感谢开源社区中每一位先行者的探索与分享。

## 5. 开源协议
本项目采用 AGPL-3.0 协议开源。
