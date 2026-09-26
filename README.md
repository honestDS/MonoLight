# MonoLight

**English** | [中文](./README_ZH.md)

MonoLight is a general-purpose autonomous agent runtime focused on safe execution and human-AI collaboration.

<p align="center">
  <img src="./docs/banner.webp" alt="MonoLight Banner" width="100%" />
</p>

## Our Vision

As agents evolve from answering questions to executing commands, modifying files, accessing networks, and managing long-running tasks, even a seemingly small judgment error can cause irreversible effects on real systems. MonoLight focuses on a central question: as we give AI greater autonomy to act, how can we keep that autonomy controllable, auditable, and trustworthy at the same time?

MonoLight does not aim to remove humans from the loop entirely. Instead, it seeks to establish a clear safety boundary between human oversight and autonomous AI execution. Through pre-execution safety audits, explicit human confirmation, and complete execution records, critical actions can be reviewed before they happen and traced afterward.

From everyday office tasks to server operations and complex automation workflows, MonoLight aims to turn the decision-making ability of large language models into real-world execution capability while reducing the risks introduced by autonomous actions as much as possible. Through continuous engineering practice and safety experimentation, we hope to explore a safer and more controllable operating model for human-AI collaborative agents.

---

## PR / Issue Policy

> [!IMPORTANT]
> The project is currently in an early stage of active development, and its architecture is still evolving rapidly. Pull Requests are not accepted for now. You are welcome to report issues or suggest improvements through Issues. Your feedback is highly valuable to us.

## 0. Current Progress
- Performance optimization
- Skill support

## 1. Core Features

MonoLight is more than a chat interface or a thin wrapper around one-off tool calls. It is an autonomous agent system designed to execute tasks continuously, accept human oversight, and preserve complete execution records.

- **Dual-model safety auditing**: Separates task execution from safety review. The primary model plans tasks and invokes tools, while an independently configured audit model evaluates risks before execution. High-risk operations can be blocked or converted into readable human-confirmation cards. Audit decisions, confirmation flows, and execution results are all traceable.
- **Long-term memory and user preference management**: Supports cross-session long-term memory and user-level personalization. By combining relational storage with RAG retrieval, MonoLight can persist historical information and user preferences, perform hybrid retrieval, and relate retrieved information to the current context.
- **Full-featured Shell**: In addition to ordinary one-shot shell commands, MonoLight supports interactive terminals through Windows ConPTY and Linux PTY. The AI can continuously read output, send follow-up input, query terminal status, resize the terminal, and close sessions proactively, enabling operation of TTY-dependent, interactive, or long-running command-line programs.
- **Autonomous and continuous task execution**: Supports multi-turn tool use, parallel tool calls, background tasks, and scheduled tasks. Long-running work can continue in the background, users can inspect or cancel it, and a summary reply can be sent when the work completes.
- **Knowledge, web, and file capabilities**: Supports document knowledge bases, vector retrieval, and reranking. The AI can query knowledge bases, search and crawl the web, generate images, write files, and send files back to users.
- **Multi-model orchestration**: Connect and centrally manage multiple model channels. Different models can be selected for chat, context summarization, knowledge-base reranking, and image generation, with priority- and weight-based routing so that all workloads do not need to depend on a single model.
- **IM platform integration**: Use the full agent experience directly from everyday messaging applications without staying in the browser. MonoLight currently supports WeChat OpenClaw QR-code integration, bidirectional text/image/file messaging, automatic merging of consecutive messages, in-chat tool calls, and safety confirmations. Background and scheduled tasks can proactively push results back to the platform, with automatic retry on delivery failure.
- **Complete visual workspace**: Includes chat and session history, tool execution results, audit confirmation cards, knowledge bases, model channels, prompts, scheduled tasks, messaging platforms, and real-time/historical log management.
- **Multi-user support and data isolation**: Supports users and roles while isolating session data between users. Each IM integration account can also be bound to a specific user and Profile.
- **Self-hosting and data ownership**: Supports SQLite and MySQL, scaling from local personal deployment to multi-user environments. Model channels, prompts, and runtime data remain under the deployer's control.

## Model Runtime Requirements

MonoLight is designed for multi-turn tool use, context summarization, and continuous task execution. Its model requirements are therefore higher than those of a typical chat application. The "minimum requirements" below indicate the baseline for using the core Agent capabilities with reasonable completeness, not merely for basic conversation.

| Item | Minimum Requirement | Recommended Configuration |
| --- | --- | --- |
| Main chat model context window | 32K tokens | 64K tokens or more; 128K+ recommended for complex tool tasks |
| Main chat model max output per request | 16K tokens | 20K tokens or more |
| Summarization model context window | 32K tokens | 64K tokens or more |
| Summarization model max output per request | 8K tokens | 16K tokens or more |
| Memory-organization model context window | 32K tokens | 64K tokens or more |
| Memory-organization model max output per request | 16K tokens | 20K tokens or more; the default 45-memory organization trigger requires at least 11,520 tokens, while organizing the full 50-memory capacity requires 12,800 tokens |
| Audit model context window | 32K tokens | 64K tokens or more |
| Audit model max output per request | 4K tokens | 8K tokens or more |
| Tool calling | The main chat model must support native Tool/Function Calling, reliably produce valid arguments, and correctly process tool results | Stable multi-turn and parallel tool calling support |
| Instruction following | All text-generation models must reliably distinguish System, User, Assistant, and Tool messages and continue following system constraints | Prefer models optimized for agents/tool use with strong instruction-following ability |

> [!IMPORTANT]
> Models below these requirements may still work for simple chat or a subset of features, but they are not recommended for the complete MonoLight Agent workflow. The system controls context size through history summarization, one-time truncation of the current tool round, and a final request-budget check. Persisted historical tool results are not re-truncated or rewritten through a sliding window on later requests. If recent required interactions and tool results still cannot fit after older history has already been compressed, the model's context capacity is generally insufficient for that task. The main chat, context summarization, long-term memory organization, and safety audit workloads can use different models. At present, the main chat, summarization, memory-organization, and audit workloads all use model entries configured for chat usage.

Image, audio, and video understanding, as well as image generation, are optional capabilities. They are only required when the corresponding features are used.

## Known Issues

- For continuous conversations, the real input token count returned by the previous API call is used as the baseline for estimating the next turn incrementally. Runtime instructions are attached only to the latest user message in each turn and are moved from the previous user message to the newest one across turns. As a result, the next-turn incremental estimate may retain the previous runtime-instruction tokens, slightly overestimating usage and triggering context summarization earlier than necessary. Once the model returns actual usage, the UI is corrected using the real value. MonoLight currently avoids extra probing requests for calibration in order to prevent additional token usage.
- The audit system only reviews the script directly executed in the current operation. It does not recursively track and audit other scripts that the entry script indirectly calls, imports, or launches. Recursive auditing has no clear natural boundary and can cause very large token consumption for large projects, so chained auditing is currently unsupported.

## 2. Interaction Entry Points

- **Dashboard**: A modern management interface built with Vue 3 and Element Plus for smooth configuration and interaction.
- **API documentation**: Built-in Swagger documentation at `/docs`, supporting authenticated access and business API calls.

## 3. Roadmap

- Core
  - [ ] **Dynamic Skill loading**: Add hot-pluggable skills and online hot updates for the skill library.
  - [ ] **Full multimodal support**: Extend beyond image, text, and file transfer to support video, audio, and other multimedia upload and interaction workflows.
  - [ ] **Agent-managed system configuration tools**: Allow system configuration files to be dynamically adjusted and managed through conversations with the LLM.
  - [ ] **WebUI analytics dashboard**: Provide real-time system health, user session data, model-call statistics, and other operational information for administrators.
  - [ ] **Define a long-term memory storage standard**: Establish a general long-term memory storage format for data exchange with external systems.
  - [ ] **Long-term memory import/export**: Allow users to export long-term memory in a standard format and import it back from that format.
- Extensions
  - [ ] **Enterprise-grade audit system**: Build on the platform's existing dual-model audit mechanism to automatically generate and deliver enterprise-grade audit reports, then immediately delete temporary audit data from the server after delivery to reduce the risk of tampering or leakage.
  - [ ] **QQ messaging adapter**: Add messaging integration for the QQ platform.
  - [ ] **Sub-agent spawning by the primary agent**:
    This is an exploratory feature that requires further research and implementation.
    The main idea is for the primary agent to create temporary sub-agents for heavy workloads that can run concurrently, with the sub-agents operating under the primary agent's guidance.
    The necessity of this feature within the current system architecture still needs to be evaluated.

## 4. Technical Architecture

Architecture documentation: [ARCHITECTURE.md](./ARCHITECTURE.md)

Development guide: [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)

## Running the Services

MonoLight consists of one Web service and five independent workers: the messaging-platform worker, background-task worker, long-term-memory worker, terminal worker, and session final-reply worker. The Web service can start multiple Web workers through `APP_WORKERS`. The five background workers use database leases to ensure that only one active instance of each worker type exists within the same database scope.

### Common Backend Setup

All three deployment modes start by installing backend dependencies from the project root:

```bash
python -m pip install -r requirements.txt
```

Configure the database, listen address, port, and number of Web workers in the root `.env` file. Choose either SQLite or MySQL for `DATABASE_URL`:

```dotenv
# SQLite
DATABASE_URL=sqlite+aiosqlite:///./data/monolight.db

# MySQL (replace the SQLite configuration above)
# DATABASE_URL=mysql+aiomysql://username:password@127.0.0.1:3306/monolight

APP_HOST=0.0.0.0
APP_PORT=8001
APP_WORKERS=1
```

`APP_HOST` defaults to `0.0.0.0` and accepts only IP address literals. Hostnames and the unspecified IPv6 address `::` are rejected. `0.0.0.0` means listening on all network interfaces. When configured this way, the `127.0.0.1` URL printed in the console is only reachable from the server itself; other devices must use the server's actual IP address or a reverse-proxy domain name. When exposing the service through a reverse proxy, `127.0.0.1` is recommended. The example port is `8001` and can be changed for your deployment environment.

### Option 1: Integrated Deployment (Recommended)

This is the default and recommended deployment mode. Release packages include the prebuilt Dashboard assets under `app/static/dashboard/`, so Node.js and npm are not required on the deployment machine. Run:

```bash
python start.py
```

The Dashboard, API, and WebSocket endpoints are served by the same Web service and therefore remain same-origin in the browser. `start.py` does not build the frontend; it only validates that the prebuilt assets exist. After startup completes, the console prints the English `Dashboard access URL: ...` address.

Before creating any child process, the launcher validates the prebuilt assets and system-key integrity, creates/migrates the database schema, and completes system initialization. Web services and all five workers are started only after every prerequisite succeeds. If a prerequisite fails, no child process is launched. After child processes are running, an abnormal exit from any process or a termination signal causes the launcher to clean up the remaining processes.

### Option 2: Separate Frontend and Backend Deployment

The backend must still be started with `python start.py`. The current launcher still requires the prebuilt files under `app/static/dashboard/`, even if an external frontend does not use them, so they must not be removed.

Building the frontend requires Node.js and npm. Keeping the browser same-origin is recommended: host the frontend with an independent static server and reverse-proxy `/api` (including WebSocket Upgrade requests) to the backend. In this mode, leave the following root `.env` variables commented out so the frontend uses `/api/v1` on the current browser origin:

```dotenv
# VUE_APP_API_BASE_URL=https://api.example.com/api/v1
# VUE_APP_WS_BASE_URL=wss://api.example.com
```

Only uncomment and configure these variables when the browser must access the backend directly across origins. `VUE_APP_API_BASE_URL` must include `/api/v1`. `VUE_APP_WS_BASE_URL` should contain only the WebSocket service root address or reverse-proxy prefix, not the final `/api/v1/.../ws` endpoint. These are frontend build-time variables, so the frontend must be rebuilt after changing them:

```bash
cd dashboard
npm install
npm run build
```

Build output is always written to `app/static/dashboard/`. Copy or publish those assets to the independent static server afterward.

The initialization flow uses `SameSite=Strict` cookies. For direct cross-origin access, the frontend and backend should at minimum be HTTPS subdomains under the same site, such as `console.example.com` and `api.example.com`. Completely unrelated sites will cause the initialization session to fail. HTTPS and WSS are recommended for production deployments.

### Option 3: Development Mode

Normally leave `VUE_APP_API_BASE_URL` and `VUE_APP_WS_BASE_URL` commented out in the root `.env` file. Start the backend from the project root in the first terminal:

```bash
python start.py
```

In a second terminal, enter `dashboard`. Install dependencies on the first run, then start the Vue development server:

```bash
cd dashboard
npm install
npm run serve
```

The Vue development server reads `APP_HOST` and `APP_PORT` from the root `.env`, proxies `/api` HTTP and WebSocket requests to the backend, and provides hot reload. Restart `npm run serve` after changing `APP_HOST` or `APP_PORT`. You do not need to run `npm run build` after every frontend change during development.

When debugging individual processes separately, first run global initialization once:

```bash
python -c "import asyncio; from start import initialize_system; asyncio.run(initialize_system())"
```

Then start the processes independently:

```bash
python main.py
python -m app.workers.message_platform
python -m app.workers.background_task
python -m app.workers.memory
python -m app.workers.terminal
python -m app.workers.session_reply
```

### Multi-instance Deployment

All instances must connect to the same database. Background workers that do not acquire the lease remain on standby and automatically take over after the current lease holder exits or the lease expires.

## Automated Tests

The project includes automated tests covering unit tests, initialization logic, and API integration tests.

### Running the Test Suite

Run the full test suite from the project root:

```bash
PYTHONPATH=. pytest tests/
```

## Project Preview

- Clean WebUI chat experience
<p align="center">
  <img src="./docs/screenshot.png" alt="MonoLight Dialog Page" width="100%" />
</p>

- Rich configuration options
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

- Detailed real-time logs
<p align="center">
  <img src="./docs/screenshot7.png" alt="MonoLight Log Page" width="100%" />
</p>

## AI Agent Development Rules

> [!IMPORTANT]
> All AI Agents contributing to this project must strictly follow these development standards and architectural principles:
> 1. Read and follow the naming conventions, code style requirements (Ruff), and testing requirements in [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md).
> 2. Refer to [ARCHITECTURE.md](./ARCHITECTURE.md) to ensure changes comply with the system design and module dependency rules.
> 3. Before submitting any code, ensure that both `ruff check` and `ruff format` pass.
> 4. Every added or modified test must be based strictly on the actual implementation of the target code. Before writing tests, an AI Agent must fully read and understand the relevant source code so that mocks and business flows match the real implementation. Tests based on assumptions or experience rather than the actual code are prohibited.

## Acknowledgements

MonoLight is built on the knowledge and inspiration of the open-source community. The following projects and communities provided important design inspiration during development:

- **[LinuxDo](https://linux.do)** - A new ideal-style community. The author has gained a large amount of AI-related knowledge and inspiration from this community.
- **[New API](https://github.com/QuantumNous/new-api)** - A next-generation LLM gateway and AI asset management system. MonoLight's **model channel routing and multi-model scheduling** design was strongly inspired by this project.
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** - An open-source all-in-one Agentic chatbot platform. MonoLight's approach to **knowledge-base tooling and IM platform integration** draws on its excellent practices.

Thanks to every pioneer in the open-source community for exploring, building, and sharing.

## 5. License

This project is open source under the AGPL-3.0 license.
