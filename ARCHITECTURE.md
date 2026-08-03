# MonoLight 项目架构说明

## 顶层目录

```text
Monoligh/
├── app/                    # FastAPI 后端源码
├── dashboard/              # Vue 管理与聊天前端
├── data/                   # 运行期持久化数据
├── scripts/                # 数据迁移与维护脚本
├── temp/                   # 临时文件与工具工作目录
├── tests/                  # 后端自动化测试
├── .env                    # 本地环境变量
├── .gitattributes          # Git 属性配置
├── .gitignore              # Git 忽略配置
├── ARCHITECTURE.md         # 项目架构说明
├── DEVELOPMENT_GUIDE.md    # 开发规范
├── LICENSE                 # 项目许可证
├── README.md               # 项目说明
├── logo.jpg                # 项目 Logo
├── main.py                 # FastAPI 应用入口
├── start.py                # 多进程启动器
├── pytest.ini              # Pytest 配置
├── requirements.txt        # 后端依赖
└── ruff.toml               # Ruff 配置
```

`.git/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 和 `node_modules/` 等版本控制、虚拟环境及缓存目录不在本文展开。`.clinerules/`、`.kilo/` 和 `参考项目/` 是本地忽略的工具配置或参考资料目录，不属于受版本控制的产品架构。

## 后端目录：`app/`

```text
app/
├── adapters/               # 对话入口与消息平台适配
├── api/                    # HTTP 与 WebSocket 接口
├── core/                   # 核心业务与通用能力
├── models/                 # 数据库与业务数据模型
├── providers/              # 数据库和模型服务封装
├── schemas/                # 接口数据结构
├── transformers/           # 模型协议转换
├── workers/                # 独立后台进程
├── __init__.py             # 后端包标识
├── handler.py              # 中间件、路由与异常处理注册
├── tasks.py                # 周期清理任务
└── warning_filters.py      # 运行时告警过滤
```

### 应用入口与后台进程

```text
main.py                     # 创建 FastAPI 应用
start.py                    # 启动 Web 与独立后台进程

app/workers/
├── __init__.py             # Worker 包标识
├── background_task.py      # 后台工具任务进程
├── lease.py                # Worker 数据库租约管理
├── message_platform.py     # 消息平台、定时任务与发件箱进程
├── session_reply.py        # 会话最终回复进程
├── terminal.py              # 持有终端会话租约与后续 PTY 生命周期
└── signals.py              # Worker 退出信号处理
```

## 接口层：`app/api/v1/`

```text
app/api/v1/
├── auth.py                 # 登录、令牌与管理员重置接口
├── channels.py             # 模型渠道与模型条目接口
├── chat.py                 # 对话、会话、消息与任务接口
├── files.py                # 文件上传与下载接口
├── knowledge_base.py       # 知识库、文档与检索接口
├── message_platforms.py    # 消息平台与微信登录接口
├── profile.py              # Profile 配置接口
├── prompts.py              # Prompt 管理接口
├── scheduled_tasks.py      # 定时任务接口
├── system.py               # 系统设置、日志与语言接口
└── users.py                # 用户管理接口
```

## 对话适配层：`app/adapters/`

```text
app/adapters/
├── base.py                 # 对话适配器基类
├── chat_web.py             # HTTP 对话适配
├── chat_ws.py              # WebSocket 对话适配
└── weixin_openclaw/
    ├── __init__.py         # 微信 OpenClaw 适配包标识
    ├── adapter.py          # 微信对话适配入口
    ├── client.py           # OpenClaw 请求客户端
    ├── config.py           # 平台配置解析
    ├── constants.py        # 平台常量
    ├── crypto.py           # 敏感配置加解密
    ├── media.py            # 媒体与文件处理
    ├── message.py          # 入站消息解析
    ├── outbound.py         # 微信出站文本约束策略
    ├── response.py         # 出站响应处理
    └── schemas.py          # 平台内部数据结构
```

## 核心层：`app/core/`

```text
app/core/
├── audit/                  # 整轮审计、确认判定、结果版本与文件存储
├── background_tasks/       # 后台任务与定时任务
├── crud/                   # 数据访问
├── dispatchers/            # 对话分发实现
├── embedding/              # 知识库向量化
├── i18n/                   # 后端多语言
├── message_platforms/      # 消息平台运行管理
├── rerank/                 # 知识库重排
├── retrieval/              # 知识库检索
├── session_reply_queue/    # 会话最终回复队列
├── terminal/               # 交互终端与 PTY 生命周期
├── tools/                  # 模型可调用工具
├── utils/                  # 通用与调度辅助函数
├── channel_model_protection.py # 渠道模型通用引用保护
├── channel_router.py       # 模型渠道选择
├── constants.py            # 常量与消息键
├── context.py              # 对话上下文构建
├── crypto.py               # API Key 加解密
├── dispatch_context.py     # 工具执行上下文
├── dispatcher.py           # 对话分发入口
├── event_loop.py           # Uvicorn 跨平台事件循环选择
├── exceptions.py           # 业务异常
├── log.py                  # 日志记录
├── log_broadcaster.py      # 实时日志广播
├── memory.py               # 只包含长期记忆 collection 名和版本化 vector item ID 的稳定生成基础
├── memory_channel_protection.py # 长期记忆嵌入渠道与模型引用保护
├── memory_embedding_config.py # 长期记忆嵌入配置预检、确认与迁移入队
├── paths.py                # 数据与临时目录路径
├── profile_selection.py    # 会话、平台与默认 Profile 选择
├── profile_validation.py   # Profile 渠道配置与归属校验
├── prompts.py              # 内置提示内容
├── security.py             # 认证与安全辅助
├── session_cleanup.py      # 会话关联数据清理
├── session_notifier.py     # 会话事件通知
└── session_source.py       # 会话来源与工具调用展示默认值
```

### 审计记录：`app/core/audit/`

```text
app/core/audit/
├── __init__.py             # 审计能力导出
├── confirmation.py         # 确认词识别与确认消息状态更新
├── integrity.py            # 整轮和逐工具参数摘要及文件摘要
├── persistence.py          # 审计文件与数据库明细保存编排
├── service.py              # 整轮评分、按需只读文件证据与确认摘要
├── startup.py              # 启动恢复及保留期清理
└── storage.py              # 审计文件原子写入与路径核对
```

### 后台任务：`app/core/background_tasks/`

```text
app/core/background_tasks/
├── __init__.py             # 后台任务包标识
├── manager.py              # 任务提交与状态管理
├── recovery.py             # 未完成任务恢复
├── reply_trigger.py        # 任务完成后的总结回复触发
├── runner.py               # 后台工具任务执行
├── scheduler.py            # 定时任务调度
└── schemas.py              # 后台任务内部数据结构
```

### 会话最终回复队列：`app/core/session_reply_queue/`

```text
app/core/session_reply_queue/
├── __init__.py             # 队列能力导出
├── consumer.py             # 工作认领、并发执行与恢复
├── executor.py             # 前台、任务总结及已确认工具执行
└── manager.py              # 统一用户消息提交、确认判定与工作入队
```

### 交互终端：`app/core/terminal/`

```text
app/core/terminal/
├── __init__.py             # 交互终端能力导出
├── schemas.py              # 终端协议、状态、权限与输出结果结构
├── manager.py              # 终端会话、控制命令、租约与 Worker 编排
├── process_config.py        # 交互 Shell 命令参数与子进程环境
├── pty_base.py             # PTY 抽象、资源快照与有界输出缓冲
├── pty_factory.py           # 按平台创建 PTY 驱动
├── pty_unix.py              # Linux PTY 驱动
├── pty_windows.py           # Windows ConPTY 驱动
└── recovery.py              # 进程身份校验与孤儿进程清理
```

交互终端由 `execute_shell` 统一创建。该工具必须提供 `execution_mode`，模式描述的是同一条原命令的输入输出和生命周期，不代表编程语言；两种模式都执行未修改的原命令，不因模式选择替换或包装命令。`non_interactive` 始终使用 PIPE 子进程路径，适合不需要后续输入且应在 `tool_timeout` 内结束的命令；`interactive` 从进程创建开始就使用 PTY，适合需要后续输入、TTY 行为或在本次工具调用结束后继续运行的命令。当前仅支持 Windows ConPTY 和 Linux PTY，其他平台不支持 `interactive`。

Web 进程和会话最终回复 Worker 不直接持有 PTY，只通过数据库中的 `terminal_session` 会话记录和 `terminal_control_command` 控制命令寻址；只有 terminal Worker 认领会话租约并持有 PTY。PTY 输出正文只保存在 terminal Worker 的有界内存缓冲中，数据库只保存输出容量、offset 和 sequence 等边界元数据；stdout 与 stderr 在 PTY 层合并为一个按字节 offset 读取的输出流。terminal Worker 重启后不会恢复旧输出正文，遗留会话按租约恢复规则处理。

`terminal_write` 在 Worker 实际写入 PTY 之前记录 `output_offset_before_write`，并将该位置作为本次读取起点。写入、等待命令完成和内部读取共用当前 Profile 的 `tool_timeout` 时间预算；超时只结束等待，不关闭终端，并返回 `read_timed_out=true`、`read_result=null` 和 `read_offset`，后续通过 `terminal_read` 从该 offset 继续读取。终端自然退出或显式关闭后分别记录 `EXITED` 或对应失败终态；审计配置完整并成功绑定时，终端终态会完成原始审计执行记录，未配置审计时允许使用空绑定。

终端会话租约失效时，持有者关闭 PTY，后续恢复流程清理孤儿进程并将无法继续持有的会话标记为 `LOST`，未完成的控制命令失败；terminal Worker 正常停止时会关闭其持有的 PTY、结束未完成命令，并将未完成会话标记为 `LOST`。删除聊天会话时，系统先按保存的进程身份清理终端进程，再处理活动会话的 `LOST` 终态和审计收尾，最后删除终端会话及控制命令记录。进程清理同时核对 PID、`create_time` 和系统 `boot_time`，避免 PID 复用导致误杀其他进程。

### 消息平台：`app/core/message_platforms/`

```text
app/core/message_platforms/
├── __init__.py             # 消息平台包标识
├── base.py                 # 平台处理器基类
├── inbound_collector.py    # 入站消息收集与合并
├── manager.py              # 平台轮询与发件箱投递管理
├── notifier.py             # 会话事件和平台消息入队
├── outbound_text.py        # 出站文本长度约束、精简与回退
├── tool_output.py          # 主动回复中的工具调用正文合并
└── weixin_openclaw.py      # 微信 OpenClaw 平台处理器
```

### 数据访问：`app/core/crud/`

```text
app/core/crud/
├── audit.py                # 审计整轮、工具明细、确认占用与执行记录访问
├── audit_tool_result_version.py # 工具结果不可变版本访问
├── background_task.py      # 后台任务数据访问
├── base.py                 # 通用数据访问基类
├── channel.py              # 渠道与模型数据访问
├── channel_cursor.py       # 渠道路由游标访问
├── context_summary_fragment.py # 上下文总结片段访问
├── context_summary_stage.py # 上下文总结阶段访问
├── knowledge_base.py       # 知识库数据访问
├── log.py                  # 系统日志访问
├── message.py              # 消息访问
├── message_platform.py     # 消息平台访问
├── message_platform_outbox.py # 消息平台发件箱访问
├── memory.py               # 长期记忆 Store、记录、历史、embedding revision/delta 的 uid 隔离读写
├── memory_job.py           # 专用记忆作业去重、目标占用和基础状态读写
├── profile.py              # Profile 访问
├── prompt.py               # Prompt 访问
├── scheduled_task.py       # 定时任务访问
├── session.py              # 会话访问
├── session_event.py        # 会话通知事件访问
├── session_reply_stream_event.py # 回复流事件访问
├── session_reply_work_item.py # 回复工作与顺序访问
├── system_setting.py       # 系统设置访问
├── terminal_session.py      # 终端会话与控制命令访问
├── user.py                 # 用户访问
└── worker_lease.py         # Worker 租约访问
```

### 对话分发：`app/core/dispatchers/`

```text
app/core/dispatchers/
├── __init__.py             # 分发器导出
├── background.py           # 后台对话分发
├── interactive.py          # HTTP/WebSocket/队列交互式流式与非流式对话共用执行流程
├── interactive_helpers.py   # 交互式分发的消息、工具与执行检查点辅助逻辑
├── non_stream.py           # 非流式对话分发
├── shared.py               # 分发器共用逻辑
└── stream.py               # 流式对话分发
```

### 调度辅助：`app/core/utils/dispatcher/`

```text
app/core/utils/dispatcher/
├── __init__.py
├── append_new_user_messages.py       # 追加用户消息
├── channel_call.py                   # 模型渠道调用
├── context_summary_checkpoint.py     # Agent 循环中的上下文总结检查点
├── fetch_and_merge_new_user_messages.py # 获取并合并新消息
├── handle_parallel_tool_limit.py     # 并行工具数量限制
├── helpers.py                        # 调度共用函数
├── inject_system_prompt.py           # 系统提示注入
├── mark_initial_message_processed.py # 初始消息状态更新
├── markdown_instruction.py           # Markdown 输出指令
├── prepare_messages.py               # 模型消息准备
├── process_markdown_response.py      # Markdown 响应处理
├── process_single_tool.py            # 单个工具调用处理
├── save_assistant_message.py         # 助手消息保存
├── save_initial_message.py           # 初始消息保存
├── save_message.py                   # 通用消息保存
├── save_tool_response.py             # 工具响应保存
├── truncate_tool_result.py           # 工具结果截断
├── user_input_batch.py               # 用户输入批次及来源消息标识
└── validate_profile_and_cfg.py       # Profile 与配置校验
```

### 上下文总结：`app/core/utils/context_summary/`

```text
app/core/utils/context_summary/
├── __init__.py             # 上下文总结能力导出
├── boundary.py              # 总结触发边界解析与校验
├── cleanup.py              # 过期总结任务与阶段清理
├── common.py               # 共用状态与令牌计算
├── history.py              # 历史消息测量与分页
├── merge.py                # 已完成总结片段归并
├── model_call.py           # 总结模型调用
├── pipeline.py             # 总结片段处理管线
├── reduction.py            # 多层总结归约与精炼
├── selection.py            # 总结模型选择
├── service.py              # 总结生成与保存入口
├── snapshot.py             # 消息快照构建
├── split.py                # 总结来源单元拆分
├── stage.py                # 总结阶段执行与状态更新
└── user_message_block.py    # 总结中已覆盖用户消息的编码与解析
```

### 其他通用函数：`app/core/utils/`

```text
app/core/utils/
├── assistant_files.py      # 助手文件信息提取
├── background_task_result.py # 后台任务结果整理
├── channel_profile_sync.py # 渠道与 Profile 配置同步
├── config.py               # 配置读取
├── context_budget.py       # 上下文令牌预算
├── context_messages.py     # 上下文消息处理
├── http_proxy.py           # 渠道 HTTP 代理校验与请求参数
├── message_assembler.py    # 消息组装
├── message_parser.py       # 消息解析
├── model_request_headers.py # 模型自定义请求头校验与构建
├── operation_directories.py # 工具允许操作目录校验
├── request_token_baseline.py # LLM 请求令牌基线与增量估算
├── session.py              # 会话辅助函数
├── system.py               # 系统信息
├── text_splitter.py        # 文本切分
├── time.py                 # 时间处理
└── tokenizer.py            # 令牌计算
```

### 工具系统：`app/core/tools/`

```text
app/core/tools/
├── __init__.py             # 工具注册与过滤
├── base.py                 # 工具基类
├── cancel_background_task.py # 取消后台任务
├── file_writer.py          # 写入文件
├── firecrawl_scrape.py     # Firecrawl 网页抓取
├── firecrawl_search.py     # Firecrawl 搜索
├── image_generation.py     # 图像生成
├── knowledge_base_query.py # 知识库查询
├── list_background_tasks.py # 查询后台任务
├── read_text_file.py       # 通用只读文本文件读取
├── read_multimodal_file.py # 读取并校验本地多模态文件
├── send_file_to_user.py    # 向用户发送文件
├── terminal.py             # 交互终端状态、读写、调整与关闭工具
└── shell.py                # Shell 命令执行
```

### 知识库能力

```text
app/core/embedding/
├── __init__.py
├── common.py               # 通用嵌入渠道/模型校验、调用参数和实际维度探测
└── knowledge_base.py       # 文档向量化与写入，复用共用嵌入代码并保持原有语义

app/core/rerank/
├── __init__.py
├── knowledge_base.py       # 检索结果重排
└── schemas.py              # 重排数据结构

app/core/retrieval/
├── __init__.py
├── fusion.py               # 检索结果融合
├── hybrid.py               # 混合检索
├── schemas.py              # 检索数据结构
├── sparse.py               # 稀疏检索
└── tokenizer.py            # 检索分词
```

### 多语言

```text
app/core/i18n/
├── __init__.py
├── context.py              # 当前语言上下文
├── locale.py               # 语言解析
├── translator.py           # 文本翻译入口
└── locales/                # 多语言消息
```

## 数据模型层：`app/models/`

```text
app/models/
├── __init__.py             # 模型导出
├── audit.py                # 审计整轮、工具明细、确认占用、执行记录与结果版本模型
├── background_task.py      # 后台任务模型
├── channel.py              # 渠道与模型条目模型
├── channel_cursor.py       # 渠道路由游标模型
├── context_summary_stage.py # 上下文总结阶段与片段模型
├── knowledge_base.py       # 知识库、文档与分块模型
├── memory.py               # 长期记忆基础表
├── message.py              # 消息与持久引导模型
├── message_platform.py     # 消息平台模型
├── message_platform_outbox.py # 消息平台发件箱模型
├── profile.py              # Profile 配置模型
├── prompt.py               # Prompt 模型
├── scheduled_task.py       # 定时任务模型
├── session.py              # 会话模型
├── session_event.py        # 会话通知事件模型
├── session_reply_stream_event.py # 回复流事件模型
├── session_reply_work_item.py # 回复工作与顺序模型
├── system_log.py           # 系统日志模型
├── system_setting.py       # 系统设置模型
├── terminal_session.py      # 终端会话与控制命令模型
├── user.py                 # 用户模型
└── worker_lease.py         # Worker 租约模型
```

## 外部能力封装：`app/providers/`

```text
app/providers/
├── __init__.py
├── database/
│   ├── __init__.py
│   ├── bootstrap.py        # 数据表与初始数据创建
│   ├── client.py           # 异步数据库连接与会话
│   └── time.py             # 数据库时间读取
├── embedding/
│   ├── __init__.py
│   └── client.py           # 向量模型客户端
├── image_generation/
│   ├── __init__.py
│   └── client.py           # 图像生成客户端
├── llm/
│   ├── __init__.py
│   └── client.py           # 大模型客户端
├── rerank/
│   ├── __init__.py
│   └── client.py           # 重排模型客户端
└── vector/
    ├── __init__.py
    └── chroma.py           # Chroma 向量库访问，支持异步批量写入/读取/删除、collection 校验和孤儿 item 清理，并保留同步兼容入口
```

## 接口结构与协议转换

```text
app/schemas/
├── auth.py                 # 认证接口数据结构
├── background_task.py      # 后台任务数据结构
├── response.py             # 通用响应与分页结构
└── scheduled_task.py       # 定时任务接口数据结构

app/transformers/
├── base.py                 # 聊天、嵌入、生图与重排转换基类
├── cohere_rerank.py        # Cohere 重排协议转换
└── openai/
    ├── __init__.py         # OpenAI 转换器导出
    ├── base.py             # OpenAI HTTP、SSE 与异常处理基类
    ├── chat_completions.py # OpenAI Chat Completions 协议转换
    ├── embedding.py        # OpenAI 嵌入协议转换
    ├── image_generation.py # OpenAI 生图协议转换
    └── responses.py        # OpenAI Responses 协议转换
```

## 前端目录：`dashboard/`

```text
dashboard/
├── dist/                   # 执行构建后生成的前端产物
├── public/                 # 静态页面模板
├── src/                    # Vue 源码
├── tests/                  # 前端聊天状态与事件跟踪测试
├── package-lock.json       # 依赖锁定文件
└── package.json            # 前端依赖与命令
```

`dashboard/dist/` 和 `dashboard/node_modules/` 均为按需生成且被忽略的目录，未执行构建或安装依赖时可以不存在。

### 前端源码：`dashboard/src/`

```text
dashboard/src/
├── api/                    # HTTP 与 WebSocket 请求封装
├── assets/                 # 样式与图标
├── components/             # 通用组件
├── composables/            # 页面共用逻辑
├── constants/              # 前端常量
├── i18n/                   # 前端多语言
├── router/                 # 页面路由与登录检查
├── utils/                  # 前端通用函数
├── views/                  # 页面组件
├── App.vue                 # 根组件
└── main.js                 # 前端入口
```

### 页面：`dashboard/src/views/`

```text
dashboard/src/views/
├── ChannelsView.vue        # 渠道管理
├── ChatView.vue            # 聊天
├── HistoryLogs.vue         # 历史日志
├── KnowledgeBase.vue       # 知识库管理
├── LoginView.vue           # 登录
├── MessagePlatformsView.vue # 消息平台管理
├── ProfilesView.vue        # Profile 管理
├── PromptsView.vue         # Prompt 管理
├── RealTimeLogs.vue        # 实时日志
├── ScheduledTasksView.vue  # 定时任务管理
└── UsersView.vue           # 用户管理
```

### 通用组件、组合逻辑与工具函数

```text
dashboard/src/components/
├── BaseDataTable.vue       # 通用数据表格
├── ChannelEditor.vue       # 渠道编辑器
├── ChannelFormDialog.vue   # 渠道表单、模型探测与测试
├── ChannelModelEntry.vue   # 渠道单模型配置项
├── ChatMessageList.vue     # 虚拟化聊天消息、引导、工具结果与审计卡片展示
├── LanguageSwitcher.vue    # 语言切换器
├── MessagePlatformFormDialog.vue # 消息平台表单
├── ProfileFormDialog.vue   # Profile 表单
├── StatusTag.vue           # 状态标签
├── VirtualizedCode.vue     # 大段代码展示
└── weixin_oc/              # 微信 OpenClaw 登录组件

dashboard/src/composables/
├── chat/
│   ├── auditConfirmationState.js # 审计确认状态与工具结果事件合并
│   ├── contextSummaryTracker.js # 上下文总结工作与事件顺序跟踪
│   ├── historyMergeTracker.js   # 异步历史加载失效与顺序控制
│   ├── thinkingTracker.js       # Thinking 占位生命周期与请求归属跟踪
│   ├── useChatSession.js         # 聊天会话模块编排
│   ├── useChatState.js           # 消息列表与滚动状态
│   ├── useChatTransport.js       # HTTP/WebSocket 通信与生命周期事件分发
│   ├── useMessageProcessor.js    # 流式消息、工具调用与去重处理
│   ├── useSessionManager.js      # 会话列表、历史分页与会话切换
│   └── workLifecycleTracker.js   # 用户输入、Agent 循环与工作结束状态跟踪
├── useDeleteConfirm.js     # 删除确认
├── useResizeObserver.js    # 尺寸监听
├── useToolParser.js        # 工具调用内容解析
└── useWebSocket.js         # WebSocket 连接管理

dashboard/src/utils/
├── assistantResponseIdentity.js # 助手回复身份匹配与幂等合并
├── auditConfirmation.js    # 审计确认是否仍可操作的判断
├── channelTestManager.js    # 渠道模型测试并发与取消生命周期
├── index.js                # 消息处理、格式化与通用函数
├── profileOptions.js       # Profile 筛选、展示与会话选择辅助
└── toolOutputVisibility.js # 工具消息识别与显示过滤
```

### 前端测试：`dashboard/tests/`

```text
dashboard/tests/
├── assistantResponseIdentity.test.js # 助手回复身份与幂等合并测试
├── auditConfirmation.test.js     # 审计确认可操作状态测试
├── auditConfirmationState.test.js # 审计确认事件合并测试
├── channelTestManager.test.js    # 渠道模型测试并发与取消生命周期测试
├── chatConcurrentEventFlow.test.js # 并发聊天事件收敛流程测试
├── contextSummaryTracker.test.js # 上下文总结工作与事件顺序测试
├── historyMergeTracker.test.js   # 异步历史加载顺序测试
├── profileOptions.test.js        # Profile 选项与归属测试
├── thinkingTracker.test.js       # Thinking 占位生命周期测试
└── workLifecycleTracker.test.js  # 聊天工作生命周期测试
```

## 测试目录：`tests/`

```text
tests/
├── integration/
│   ├── conftest.py                  # 接口测试数据库夹具
│   ├── test_chat_concurrent_input.py # 并发输入接口流程测试
│   ├── test_chat_guidance_api.py    # 外部会话引导接口请求测试
│   ├── test_profile_memory_embedding_api.py # Profile 长期记忆嵌入配置接口测试
│   └── test_terminal_shell_workflow.py # execute_shell 交互模式与伴随工具流程测试
└── unit/
    ├── context_summary_*_fixture.py # 上下文总结测试夹具
    ├── context_summary_*_support.py # 上下文总结测试辅助
    ├── session_reply_queue_fixture.py # 回复队列测试夹具
    ├── session_reply_queue_test_support.py # 回复队列测试辅助
    ├── test_audit_*.py              # 审计、确认与审计存储测试
    ├── test_background_*.py          # 后台任务测试
    ├── test_channel_model_protection.py # 渠道模型引用保护测试
    ├── test_context_*.py             # 上下文与总结测试
    ├── test_message_*.py             # 消息与消息平台测试
    ├── test_memory_channel_protection_stage2.py # 长期记忆渠道引用保护测试
    ├── test_memory_embedding_config_stage2.py # 长期记忆嵌入配置确认测试
    ├── test_memory_storage_foundation.py # 长期记忆存储基础测试
    ├── test_memory_embedding_vector_foundation.py # 长期记忆嵌入与向量基础测试
    ├── test_session_*.py             # 会话、通知与回复队列测试
    ├── test_terminal_*.py            # 终端单元、PTY 与平台测试
    ├── test_worker_*.py              # 独立进程与租约测试
    └── test_*.py                     # 其他后端单元测试
```

## 数据迁移脚本：`scripts/`

```text
scripts/
├── migration_20260629_add_scheduled_task_profile_id.py # 定时任务 Profile 字段
├── migration_20260703_add_message_platform.py          # 消息平台表
├── migration_20260709_add_chat_session_reply_target_source.py # 会话回复目标来源字段
├── migration_20260709_add_chat_session_source.py       # 会话来源字段
├── migration_20260710_add_message_platform_outbox.py   # 消息平台发件箱表
├── migration_20260710_add_session_event.py             # 会话事件表
├── migration_20260711_add_message_dedupe_key.py        # 消息去重键
├── migration_20260711_add_session_event_dedupe_key.py  # 会话事件去重键
├── migration_20260711_add_worker_lease.py              # Worker 租约表
├── migration_20260712_add_chat_session_context_summary.py # 会话上下文总结字段
├── migration_20260712_add_session_reply_queue.py       # 会话回复队列表
├── migration_20260712_add_session_reply_stream_event.py # 会话回复流事件表
├── migration_20260712_drop_active_session.py           # 删除旧活跃会话表
├── migration_20260714_add_context_summary_stages.py    # 上下文总结阶段与片段表
├── migration_20260715_add_message_system_prompt.py     # 旧消息系统提醒字段
├── migration_20260715_migrate_message_environment_prompt.py # 消息环境提示字段迁移
├── migration_20260717_add_audit_confirmation_records.py # 审计与确认记录表
├── migration_20260719_add_background_task_audit_binding.py # 后台任务审计绑定
├── migration_20260724_add_audit_tool_result_versions.py # 审计工具结果版本表
├── migration_20260725_add_chat_session_llm_request_metadata.py # 会话 LLM 请求元数据
├── migration_20260726_add_chat_session_llm_request_metadata_order.py # LLM 请求元数据顺序字段
├── migration_20260727_add_channel_http_proxy.py # 渠道 HTTP 代理字段及旧配置迁移
├── migration_20260727_add_message_guidance_prompt.py # 消息持久引导字段
├── migration_20260727_drop_channel_type.py       # 删除旧渠道类型字段
├── migration_20260728_add_profile_selection_priority.py # Profile 默认值、会话覆盖与平台绑定字段
├── migration_20260729_add_chat_session_show_tool_calls.py # 会话工具调用展示字段
├── migration_20260729_add_message_platform_language.py # 消息平台语言字段
├── migration_20260729_add_message_platform_use_stream_dispatch.py # 消息平台流式分发字段
├── migration_20260731_add_terminal_sessions.py       # 终端会话与控制命令表
├── migration_20260801_make_terminal_audit_optional.py # 终端审计绑定可选
├── migration_20260801_terminal_process_identity.py   # 终端进程身份字段
├── migration_20260803_add_longterm_memory.py         # 长期记忆基础表
└── migration_20260803_add_memory_embedding_selection_token.py # 长期记忆嵌入配置一次性确认凭证表
```

## 运行期目录

```text
data/                       # SQLite、Chroma、日志与独立审计文件
├── audit/                  # 按用户保存的审计文件，只在系统启动阶段清理
temp/                       # 上传文件、工具结果与其他临时文件
```
