# MonoLight

[English](./README.md) | **中文**

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/honestDS/MonoLight)

**一个真正能替你动手干活、同时守住安全底线的自托管 AI 助手平台。**

<p align="center">
  <img src="./docs/banner.webp" alt="MonoLight Banner" width="100%" />
</p>

普通 AI 只能陪你聊天；真让它去电脑上跑命令、改文件，又容易担心它一不小心删错东西。

MonoLight 的做法是**把“干活的 AI”和“检查风险的 AI”拆开**：AI 准备执行命令或修改文件前，先由独立的审计模型做一遍安全评估；遇到高风险操作，系统会停下来发一张确认卡片给你，你点同意它才继续，做完之后全程留痕可查。

你可以直接在网页工作台里用它，也可以扫码连进微信里随时派活；所有模型接口、提示词、对话和文件都跑在你自己的机器上。

> [!IMPORTANT]
> **关于 PR 与反馈**：项目目前处于早期快速开发阶段，架构调整较频繁，**暂不接受 Pull Request**。欢迎通过 Issue 反馈使用问题或提出建议，每一条反馈都很重要。

---

## MonoLight 能帮你做什么

### 1. 安全可控，不怕 AI 乱来
- **干活与安检分离（双模型审计）**：主模型负责思考和调用工具，另一个独立配置的“审计模型”负责在执行前评估风险。
- **高风险操作先问你**：碰到删文件、改系统等敏感动作，系统会自动拦截或弹出确认卡片（网页和微信里都能确认），经你同意才会真正执行。
- **多人可用，数据不串**：支持多用户与角色权限管理，不同用户的会话、记忆和文件彼此隔离；支持 SQLite 或 MySQL，部署和数据完全由你掌控。

### 2. 不只是聊天，真能把活干完
- **文件读写与交付**：会读、改、搜本地文件，能生成图片，做完还能直接把文件或图片发给你。
- **不仅能跑命令，还能盯交互终端**：支持一次性命令和不断线的交互式终端（兼容 Linux 和 Windows），适合运行编译、调试或需要持续输入指令的命令行程序。
- **联网查资料**：内置网页搜索与网页内容抓取，能先查最新信息再帮你干活。
- **拆任务并行做 + 后台/定时跑**：复杂任务会自动列计划清单，还能派多个“子助手”同时分工处理再汇总；耗时长的任务可以丢到后台慢慢跑，也支持设好周期定时自动执行，做完主动给你发结果。
- **共享技能包**：管理员可以预先放入常用流程和脚本，AI 遇到对应任务时会自动查找技能说明并按规范执行。

### 3. 越用越懂你，网页微信都能用
- **跨会话长期记忆**：自动记住你的偏好、习惯和项目背景，下次对话不用重复解释；后台会定期整理合并记忆，避免越记越乱。
- **专属文档知识库**：上传资料就能做检索问答，结合向量检索与结果重排，让 AI 依据你的内部文档干活。
- **网页工作台 + 微信扫码接入**：自带完整的 Vue 管理后台（聊天、审批确认、模型配置、知识库、记忆管理、定时任务、实时与历史日志）及 `/docs` API 文档；同时支持微信扫码接入（基于 OpenClaw），直接在微信里发文字、图片、文件派活，确认危险操作并接收结果推送。
- **多模型自由搭配**：兼容 OpenAI、DeepSeek 等接口格式（支持拉取 OpenRouter 模型列表）。你可以给“主对话、总结、记忆整理、安全审计、生图”分别配最合适的模型，并按优先级和权重路由，不必绑死单一模型。

---

## 3 步快速跑起来

默认打包已包含编译好的网页前端，**不需要安装 Node.js 或 npm**。

### 准备环境
- **系统配置**：最低 2 核 CPU / 2 GB 内存（推荐 2 核 CPU / 8 GB 内存）
- **Python**：3.13 及以上
- **模型要求**：因为要多轮调用工具、整理记忆和安全审计，需使用支持原生工具调用（Tool Calling）且上下文窗口 **32K 以上**（推荐 64K 以上）的模型，不建议用极小上下文的模型。

### 启动步骤

1. **安装后端依赖**（项目根目录执行）：

```bash
python -m pip install -r requirements.txt
```

2. **创建并配置 `.env` 文件**（数据库 SQLite 与 MySQL 二选一）：

```dotenv
# 默认使用 SQLite（单文件数据库，开箱即用）
DATABASE_URL=sqlite+aiosqlite:///./data/monolight.db

# 如需改用 MySQL，注释上面一行并启用下面这行
# DATABASE_URL=mysql+aiomysql://username:password@127.0.0.1:3306/monolight

APP_HOST=0.0.0.0
APP_PORT=8001
APP_WORKERS=1
```

3. **一键启动**：

```bash
python start.py
```

启动完成后，控制台会打印 `Dashboard access URL: ...`。在浏览器打开该地址，跟着**初始化向导**创建管理员账号并填写模型接口，就可以直接使用了。

> [!TIP]
> `APP_HOST` 必须填写 IP 地址（不支持域名或 `::`）。填 `0.0.0.0` 时表示允许局域网或外网通过服务器实际 IP 访问；如果前面套了 Nginx 等反向代理，建议改为 `127.0.0.1`。

---

## 进阶说明（按需展开）

<details>
<summary>各角色模型的详细上下文与输出指标</summary>

主聊天、上下文总结、记忆整理、安全审计可在后台分别指定不同的对话模型；图像、音频、视频理解与生图仅在使用对应功能时需要模型支持。

| 角色 | 最低要求 | 推荐配置 |
| --- | --- | --- |
| 主聊天模型 | 上下文 32K / 最大输出 16K，支持原生 Tool Calling | 上下文 64K+（复杂任务 128K+）/ 最大输出 20K+，支持多轮并行调用 |
| 上下文总结模型 | 上下文 32K / 最大输出 8K | 上下文 64K+ / 最大输出 16K+ |
| 记忆整理模型 | 上下文 32K / 最大输出 16K | 上下文 64K+ / 最大输出 20K+（45 条触发需 ≥11,520 Tokens，50 条满容量需 12,800 Tokens） |
| 安全审计模型 | 上下文 32K / 最大输出 4K | 上下文 64K+ / 最大输出 8K+，指令遵循能力强 |

> 系统会通过自动总结旧历史、截断当轮过长工具结果以及请求前预算检查来控制上下文大小；已保存的历史工具结果不会在后续请求中被滑动窗口改写。若压缩旧历史后仍放不下当前交互，说明所选模型的上下文窗口不够大。

</details>

<details>
<summary>架构简述：MonoLight 是怎么跑起来的？</summary>

运行 `python start.py` 后，启动器会自动检查前端资源、初始化系统密钥与数据库，并拉起 **1 个 Web 服务 + 3 个后台 Worker 进程**：
- **Web 服务**：同时托管网页工作台、HTTP/WebSocket 接口与对话接入入口。
- **`general` Worker**：负责消息平台收发、定时/后台任务执行和会话回复协调（三个角色通过数据库租约各自抢占）。
- **`memory` Worker**：专门负责长期记忆与知识库的异步整理、向量化和发布。
- **`terminal` Worker**：专门托管持久化的交互式终端会话，与主服务做故障隔离。

任一后台进程异常退出时，启动器会自动清理其余进程；多台机器连接同一个数据库时，各 Worker 会自动竞争租约并在节点下线后自动接管。

</details>

<details>
<summary>前后端分离部署 / 本地开发调试 / 运行测试</summary>

#### 1. 前后端分离部署（需要 Node.js 与 npm）
- 后端仍需通过 `python start.py` 启动，且**不要删除** `app/static/dashboard/` 里的预构建资源。
- 推荐用独立静态服务器托管前端，并将 `/api`（含 WebSocket Upgrade）反向代理到后端，保持浏览器同源；此时根 `.env` 保持下列变量注释即可。
- 仅当需要浏览器跨域直连后端时，才在根 `.env` 取消注释并配置构建期变量（注意：初始化登录使用 `SameSite=Strict` Cookie，跨域直连要求前后端为同一主域下的 HTTPS 子域；`VUE_APP_API_BASE_URL` 需带 `/api/v1`，`VUE_APP_WS_BASE_URL` 只填根地址）：

```dotenv
# VUE_APP_API_BASE_URL=https://api.example.com/api/v1
# VUE_APP_WS_BASE_URL=wss://api.example.com
```

在 `dashboard` 目录安装依赖并构建（产物固定输出到 `app/static/dashboard/`）：

```bash
cd dashboard
npm ci
npm run build
```

#### 2. 本地开发模式
终端 1 在根目录运行 `python start.py`，终端 2 进入 `dashboard` 启动热更新服务（会自动读取根 `.env` 的 `APP_HOST` 和 `APP_PORT` 代理 `/api`；改了地址端口后重启即可）：

```bash
cd dashboard
npm ci
npm run serve
```

如需单独调试各进程，先执行一次初始化，再分别启动：

```bash
python -c "import asyncio; from start import initialize_system; asyncio.run(initialize_system())"
python main.py
python -m app.workers.general
python -m app.workers.memory
python -m app.workers.terminal
```

（`message_platform`、`background_task`、`session_reply` 保留有独立调试入口，日常部署请统一用 `general`。）

#### 3. 运行自动化测试

```bash
PYTHONPATH=. pytest tests/
```

</details>

<details>
<summary>常见问题：Windows 上创建长期记忆时整个服务无报错退出怎么办？</summary>

**现象**：开启长期记忆后，创建记忆时服务突然退出且日志里没有 Python 报错，WebUI 里留下状态为“处理中”的空记录；重启后自动恢复该任务又会再次退出。

**原因**：通常是 Windows 系统的 Microsoft Visual C++ 运行库版本过旧（如 `14.36.32532.0`），与 Chroma 等原生依赖发生访问冲突（可在 Windows“事件查看器 → Windows 日志 → 应用程序”中看到 `python.exe` 报错，故障模块 `MSVCP140.dll`，异常代码 `0xc0000005`）。

**处理方法**：
1. 停止 MonoLight 所有进程，备份数据库和向量数据目录。
2. 安装或修复[微软官方最新 x64 Visual C++ v14 运行库](https://aka.ms/vc14/vc_redist.x64.exe)（无需安装 Visual Studio，详见[微软说明](https://learn.microsoft.com/zh-cn/cpp/windows/latest-supported-vc-redist)）。
3. 重启 Windows 后重新启动 MonoLight。

> 请勿通过重装 pip 依赖、删库或从第三方下载单个 `MSVCP140.dll` 覆盖系统文件来解决。

</details>

---

## 界面预览

清晰的对话页面与任务执行过程展示：

<p align="center">
  <img src="./docs/screenshot.png" alt="MonoLight 对话界面" width="100%" />
</p>

完整的后台管理与配置页面（模型渠道、知识库、提示词、定时任务、消息平台等）：

<p align="center">
  <img src="./docs/screenshot2.png" alt="MonoLight 配置界面 1" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot3.png" alt="MonoLight 配置界面 2" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot4.png" alt="MonoLight 配置界面 3" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot5.png" alt="MonoLight 配置界面 4" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot6.png" alt="MonoLight 配置界面 5" width="100%" />
</p>

实时日志排查，每一步动作清晰可见：

<p align="center">
  <img src="./docs/screenshot7.png" alt="MonoLight 实时日志" width="100%" />
</p>

---

## 当前进度与未来规划

**近期重点**：修复遗留问题，持续优化子代理协作工具。

**接下来的计划**：
- **核心能力**
  - [ ] **全模态支持**：在文本、图片、文件之外，支持音频、视频等多媒体上传与交互。
  - [ ] **对话式系统配置**：支持通过和 AI 对话直接调整与管理系统配置。
  - [ ] **WebUI 数据看板**：提供系统运行状态、会话数据与模型调用统计看板。
  - [ ] **长期记忆标准与导入导出**：定义通用存储标准，支持记忆按标准格式导出与导入。
- **生态扩展**
  - [ ] **企业级审计报告**：基于现有的双模型审计机制，自动生成并发送审计报告，并在发送后立即清除服务端暂存的审计数据，降低篡改与泄露风险。
  - [ ] **QQ 平台接入**：新增 QQ 消息平台适配器。

---

## 文档与 AI Agent 开发规范

- 架构说明：[ARCHITECTURE.md](./ARCHITECTURE.md)
- 开发指南：[DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)

> [!IMPORTANT]
> **AI Agent 开发规范守则**：所有参与本项目贡献的 AI Agent 必须严格遵守以下开发标准与架构原则：
> 1. 阅读并执行 [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md) 中的命名规范、代码风格（Ruff）及测试要求。
> 2. 参考 [ARCHITECTURE.md](./ARCHITECTURE.md) 以确保符合系统设计与模块依赖关系。
> 3. 在提交任何代码前，必须确保通过 `ruff check` 与 `ruff format` 检查。
> 4. 所有测试用例的编写或修改必须严格基于目标代码的实际实现。在编写测试前，AI Agent 必须完整阅读并解析目标源码，确保 Mock 逻辑与业务流转完全对齐，严禁凭经验或假设编写测试代码。

---

## 致谢

MonoLight 的诞生离不开开源社区的滋养。以下项目为我们提供了重要的设计灵感与思路参考：

- **[LinuxDo](https://linux.do)** —— 提供了大量前沿 AI 相关的知识与灵感。
- **[New API](https://github.com/QuantumNous/new-api)** —— MonoLight 的**模型渠道路由与多模型调度**设计深受其启发。
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** —— MonoLight 的**知识库工具化与 IM 平台接入**思路借鉴了该项目的优秀实践。

感谢开源社区中每一位先行者的探索与分享。

## 开源协议

本项目采用 AGPL-3.0 协议开源。
