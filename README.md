# MonoLight

**English** | [中文](./README_ZH.md)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/honestDS/MonoLight)

**A self-hosted AI assistant platform that gets real work done while keeping you in control.**

<p align="center">
  <img src="./docs/banner.webp" alt="MonoLight Banner" width="100%" />
</p>

Most AI assistants stop at chat. Once you let one run commands or edit files, a simple mistake can delete something you need.

MonoLight separates **the AI that does the work from the AI that checks the risks**. An independent audit model reviews commands and file changes before they happen. The system blocks high-risk actions or pauses to show you an approval card. If it asks for approval, it only proceeds when you approve. The entire process is logged for later review.

Use MonoLight in the web dashboard, or scan a QR code to connect it to WeChat and assign tasks from anywhere. Model API settings, prompts, conversations, and files are managed on your own machine.

> [!IMPORTANT]
> **Pull requests and feedback**: The project is still in early development and its architecture changes frequently. **We're not accepting pull requests yet.** Please open an issue to report a problem or suggest an improvement. Every piece of feedback matters.

---

## What MonoLight Can Do for You

### 1. Safe and under your control
- **Separate models for execution and safety review (dual-model auditing)**: The primary model handles reasoning and tool calls. A separately configured audit model assesses risks before execution.
- **Approval for high-risk actions**: MonoLight can block sensitive actions such as deleting files or changing system settings, or ask for approval. If approval is required, the action only runs after you approve it in the web dashboard or WeChat.
- **Multi-user access and data isolation**: Role-based permissions keep users' platform-managed sessions, memories, and files separate. You control the deployment and data with SQLite or MySQL.

### 2. More than chat: get work done
- **File editing and delivery**: It can read, edit, and search local files, generate images, and send the resulting files or images directly to you.
- **Commands and interactive terminals**: Supports one-off commands and persistent interactive terminals on Linux and Windows, suitable for builds, debugging, or command-line programs that require continued input.
- **Web research**: Built-in web search and web page fetching let it check the latest information before doing the work.
- **Parallel, background, and scheduled work**: MonoLight makes a checklist for complex tasks, can delegate work to multiple subagents in parallel, and combines their results. Run long jobs in the background or set a recurring schedule. MonoLight sends you the results when the work is done.
- **Shared skill packages**: Administrators can add common workflows and scripts. For matching tasks, AI finds the relevant skill instructions and follows them.

### 3. Learns what matters to you, on the web and in WeChat
- **Cross-session long-term memory**: It remembers your preferences, habits, and project context so you do not need to repeat them. Background memory organization regularly consolidates memories and keeps them manageable.
- **Private document knowledge base**: Upload documents for retrieval and Q&A. Vector retrieval and reranking help AI work from your internal documents.
- **Web dashboard and WeChat access**: The Vue dashboard includes chat, approvals, model configuration, knowledge bases, memory management, scheduled tasks, and live and historical logs. API docs are available at `/docs`. Scan a QR code to connect WeChat via OpenClaw, then send text, images, or files to assign tasks, approve high-risk actions, and receive results.
- **Flexible multi-model setup**: Compatible with OpenAI and DeepSeek API formats, and can fetch model lists from OpenRouter. Assign separate models for main chat, summarization, memory organization, safety auditing, and image generation, then route by priority and weight instead of relying on one model.

---

## Quick Start in 3 Steps

The prebuilt frontend is included by default, so **Node.js and npm are not required**.

### Prepare the Environment

- **System configuration**: 2 CPU cores / 2 GB RAM minimum (2 CPU cores / 8 GB RAM recommended)
- **Python**: 3.13 or later
- **Model requirements**: Because MonoLight performs multi-turn tool calls, memory organization, and safety audits, the **main model** must support native Tool Calling and have a context window of **32K tokens or more** (**64K or more recommended**). Models with very small context windows are not recommended.

### Startup Steps

1. **Install backend dependencies** (run from the project root):

   ```bash
   python -m pip install -r requirements.txt
   ```

2. **Create and configure the root `.env` file** (choose either SQLite or MySQL):

   ```dotenv
   # Use SQLite by default (single-file database, ready to use)
   DATABASE_URL=sqlite+aiosqlite:///./data/monolight.db

   # To use MySQL instead, comment out the line above and enable the line below
   # DATABASE_URL=mysql+aiomysql://username:password@127.0.0.1:3306/monolight

   APP_HOST=0.0.0.0
   APP_PORT=8001
   APP_WORKERS=1
   ```

3. **Start with one command**:

   ```bash
   python start.py
   ```

After startup completes, the console prints `Dashboard access URL: ...`. Open that address in a browser and follow the **initialization wizard** to create an administrator account and enter the model API settings.

> [!TIP]
> `APP_HOST` must be an IP address (hostnames and `::` are not supported). `0.0.0.0` listens on all network interfaces; other devices should use the server's actual IP address. The printed loopback URL (`127.0.0.1`) is local to the server. When using a reverse proxy, `127.0.0.1` is recommended.

---

## Advanced Setup and Reference

<details>
<summary>Detailed context and output metrics for each model role</summary>

The backend lets you configure separate chat models for main chat, context summarization, memory organization, and security auditing; image, audio, and video understanding plus image generation only require model support when the corresponding features are used.

The table below uses tokens for context and maximum output lengths.

| Model role | Minimum | Recommended |
| --- | --- | --- |
| Main chat model | 32K context / 16K max output, native tool calling | 64K+ context (128K+ for complex tasks) / 20K+ max output, multi-turn and parallel calls |
| Context summarization model | 32K context / 8K max output | 64K+ context / 16K+ max output |
| Memory organization model | 32K context / 16K max output | 64K+ context / 20K+ max output (45-item trigger requires >= 11,520 tokens; 50-item full capacity requires 12,800 tokens) |
| Security audit model | 32K context / 4K max output | 64K+ context / 8K+ max output, strong instruction following |

> The system controls context size by automatically summarizing old history, truncating only overly long tool results from the current turn, and checking the budget before each request; saved historical tool results are not rewritten by the sliding window. If the current interaction still does not fit after old history is compacted, the selected model's context window is too small.

</details>

<details>
<summary>How MonoLight runs</summary>

Running `python start.py` checks for the prebuilt frontend assets, initializes the system keys and database, and starts **one Web service plus three background Worker processes**:
- **Web service**: Hosts the dashboard, HTTP/WebSocket endpoints, and conversation integrations.
- **`general` Worker**: Handles messaging and scheduled tasks, general background jobs, and session replies. Each of these three roles uses a separate database lease.
- **`memory` Worker**: Runs background jobs that organize and publish long-term memories and knowledge-base entries, including generating their embeddings.
- **`terminal` Worker**: Hosts persistent interactive terminal sessions in a separate process for fault isolation.

If a Worker exits unexpectedly, the launcher cleans up the remaining processes. For multi-instance deployments, all instances must share the same database. Workers coordinate through leases, so another instance can take over after the previous node goes offline or its lease expires.

</details>

<details>
<summary>Separate deployment, local development, and tests</summary>

#### 1. Separate Frontend and Backend Deployment (requires Node.js and npm)

The backend must still be started with `python start.py`. The current launcher still requires the prebuilt files under `app/static/dashboard/`, even if an external frontend does not use them, so they must not be removed.

Building the frontend requires Node.js and npm. Keeping the browser same-origin is recommended: host the frontend with an independent static server and reverse-proxy `/api` (including WebSocket upgrades) to the backend. In this mode, leave the following root `.env` variables commented out so the frontend uses `/api/v1` on the current browser origin:

```dotenv
# VUE_APP_API_BASE_URL=https://api.example.com/api/v1
# VUE_APP_WS_BASE_URL=wss://api.example.com
```

Only uncomment and configure these variables when the browser must access the backend directly across origins. `VUE_APP_API_BASE_URL` must include `/api/v1`. `VUE_APP_WS_BASE_URL` should contain only the WebSocket service root address or reverse-proxy prefix, not the final `/api/v1/.../ws` endpoint. These are frontend build-time variables, so the frontend must be rebuilt after changing them:

```bash
cd dashboard
npm ci
npm run build
```

Build output is always written to `app/static/dashboard/`. Copy or publish those assets to the independent static server described above afterward.

The initialization flow uses `SameSite=Strict` cookies. For direct cross-origin access, the frontend and backend should at minimum be HTTPS subdomains under the same site, such as `console.example.com` and `api.example.com`. Completely unrelated sites will cause the initialization session to fail. HTTPS and WSS are recommended for production deployments.

#### 2. Local Development

In terminal 1, run `python start.py` from the project root. In terminal 2, enter `dashboard` and start the hot-reload server. It automatically reads `APP_HOST` and `APP_PORT` from the root `.env` and proxies `/api` HTTP and WebSocket requests. Restart the development server after changing the address or port:

```bash
cd dashboard
npm ci
npm run serve
```

For individual process debugging, run initialization once, then start each process separately:

```bash
python -c "import asyncio; from start import initialize_system; asyncio.run(initialize_system())"
python main.py
python -m app.workers.general
python -m app.workers.memory
python -m app.workers.terminal
```

The independent entry points `message_platform`, `background_task`, and `session_reply` are retained for targeted debugging only. Use `general` for routine deployment.

#### 3. Run Automated Tests

From the project root:

```bash
PYTHONPATH=. pytest tests/
```

</details>

<details>
<summary>FAQ: Why does MonoLight quit without an error when creating long-term memories on Windows?</summary>

**Symptoms**: With long-term memory enabled, MonoLight may shut down while saving a memory, without a Python exception in the logs. The dashboard may show an empty memory marked "Processing." When the job resumes after a restart, it can trigger the same crash.

**Likely cause**: In **Windows Event Viewer → Windows Logs → Application**, look for an error involving `python.exe`. If it lists `MSVCP140.dll` and exception code `0xc0000005` (access violation), an older Microsoft Visual C++ runtime such as `14.36.32532.0` may be incompatible with a native dependency such as Chroma.

**Fix**:

1. Stop all MonoLight processes and back up the database and vector data.
2. Install or repair the [latest official Microsoft Visual C++ v14 x64 Redistributable](https://aka.ms/vc14/vc_redist.x64.exe). Visual Studio is not required; see [Microsoft's documentation](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).
3. Restart Windows, then start MonoLight again and verify that resumed jobs and newly created memories work correctly.

> Do not reinstall pip dependencies or delete the database or vector data to fix this issue. Never overwrite the system's `MSVCP140.dll` with a copy from a third-party site.

</details>

---

## Interface Preview

Clear chat interface and task progress display:

<p align="center">
  <img src="./docs/screenshot.png" alt="MonoLight Chat Interface" width="100%" />
</p>

Comprehensive backend management and configuration pages (model channels, knowledge bases, prompts, scheduled tasks, messaging platforms, and more):

<p align="center">
  <img src="./docs/screenshot2.png" alt="MonoLight Configuration 1" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot3.png" alt="MonoLight Configuration 2" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot4.png" alt="MonoLight Configuration 3" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot5.png" alt="MonoLight Configuration 4" width="100%" />
</p>
<p align="center">
  <img src="./docs/screenshot6.png" alt="MonoLight Configuration 5" width="100%" />
</p>

Real-time logs for step-by-step troubleshooting:

<p align="center">
  <img src="./docs/screenshot7.png" alt="MonoLight Live Logs" width="100%" />
</p>

---

## Current Progress and Roadmap

**Near-term focus**: Fix remaining issues and keep improving subagent collaboration tools.

**What's next**:
- **Core capabilities**
  - [ ] **Full multimodal support**: Add audio and video uploads and interactions alongside text, images, and files.
  - [ ] **Conversational system configuration**: Change and manage system settings by chatting with the AI.
  - [ ] **Web UI analytics dashboard**: Show system status, session data, and model usage statistics.
  - [ ] **Long-term memory standard and import/export**: Define a common storage format and support importing and exporting memories in that format.
- **Integrations and extensions**
  - [ ] **Enterprise audit reports**: Use the existing dual-model audit to automatically generate and send reports, then immediately delete temporary audit data from the server to reduce the risk of tampering or exposure.
  - [ ] **QQ integration**: Add a QQ messaging adapter.

---

## Documentation and AI Agent Development Rules

- Architecture: [ARCHITECTURE.md](./ARCHITECTURE.md)
- Development guide: [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md)

> [!IMPORTANT]
> **AI Agent development rules**: All AI Agents contributing to this project must strictly follow these development standards and architectural principles:
> 1. Read and follow the naming conventions, code style requirements (Ruff), and testing requirements in [DEVELOPMENT_GUIDE.md](./DEVELOPMENT_GUIDE.md).
> 2. Refer to [ARCHITECTURE.md](./ARCHITECTURE.md) to ensure changes comply with the system design and module dependency rules.
> 3. Before submitting any code, ensure that both `ruff check` and `ruff format` pass.
> 4. Every added or modified test must be based strictly on the actual implementation of the target code. Before writing tests, an AI Agent must fully read and understand the relevant source code so that mocks and business flows match the real implementation. Tests based on assumptions or experience rather than the actual code are prohibited.

---

## Acknowledgements

MonoLight draws on the work of the open-source community. The following projects and communities helped shape its design:

- **[LinuxDo](https://linux.do)** - Provides cutting-edge AI knowledge and inspiration.
- **[New API](https://github.com/QuantumNous/new-api)** - Inspired MonoLight's **model channel routing and multi-model scheduling**.
- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** - Inspired MonoLight's approach to **using the knowledge base as a tool and integrating messaging platforms**.

Thanks to the open-source community for exploring and sharing.

## License

This project is open source under the AGPL-3.0 license.
