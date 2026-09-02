# MonoLight 项目架构说明

本文只描述项目的目录分层、模块职责、持久化边界和依赖方向，不记录具体接口格式、执行流程或实现规则。

## 顶层目录

```text
Monoligh/
├── app/                    # FastAPI 后端源码
├── dashboard/              # Vue 管理与聊天前端源码
├── data/                   # 运行期持久化数据
├── scripts/                # 数据库迁移与维护脚本
├── temp/                   # 运行期临时文件
├── tests/                  # 后端自动化测试
├── .env                    # 本地运行配置
├── .gitattributes          # Git 属性配置
├── .gitignore              # Git 忽略配置
├── ARCHITECTURE.md         # 项目架构说明
├── DEVELOPMENT_GUIDE.md    # 开发规范
├── LICENSE                 # 项目许可证
├── README.md               # 项目说明
├── logo.jpg                # 项目 Logo
├── main.py                 # Web 应用入口
├── start.py                # 多进程启动协调
├── pytest.ini              # Pytest 配置
├── requirements.txt        # 后端依赖
└── ruff.toml               # Ruff 配置
```

版本控制目录、虚拟环境、缓存目录和本地工具配置不属于产品模块，不在本文展开。

## 后端分层：`app/`

```text
app/
├── static/
│   └── dashboard/           # 随发布包分发并由 FastAPI 托管的 Vue 生产构建产物
├── adapters/               # 外部对话与消息平台适配
├── api/                    # HTTP 与 WebSocket 接口
├── core/                   # 应用服务、领域能力与通用组件
├── models/                 # 持久化模型
├── providers/              # 数据库、模型和外部服务封装
├── schemas/                # 接口数据结构
├── transformers/           # 外部模型协议转换
├── workers/                # 独立后台进程
├── handler.py              # 应用中间件、路由和异常处理装配
├── tasks.py                # 后台任务定义
└── warning_filters.py      # 运行时告警过滤
```

### 应用入口与 Worker

`main.py` 提供 Web 应用入口，`start.py` 负责 Web 进程和独立后台进程的启动协调。

```text
app/workers/
├── __init__.py             # Worker 包标识
├── background_task.py      # 通用后台任务 Worker
├── lease.py                # Worker 协调支持
├── memory.py               # 长期记忆与知识作业 Worker
├── message_platform.py     # 消息平台与定时任务 Worker
├── session_reply.py        # 会话回复 Worker
├── terminal.py             # 交互终端 Worker
└── signals.py              # Worker 进程信号支持
```

### 对话适配层：`app/adapters/`

```text
app/adapters/
├── base.py                 # 对话适配器抽象
├── chat_web.py             # Web 对话适配
├── chat_ws.py              # WebSocket 对话适配
└── weixin_openclaw/
    ├── __init__.py         # 微信适配包标识
    ├── adapter.py          # 微信对话适配
    ├── client.py           # OpenClaw 客户端封装
    ├── config.py           # 平台配置结构
    ├── constants.py        # 平台常量
    ├── crypto.py           # 平台敏感配置处理
    ├── media.py            # 媒体与文件适配
    ├── message.py          # 入站消息结构
    ├── outbound.py         # 出站消息适配
    ├── response.py         # 出站响应结构
    └── schemas.py          # 平台内部数据结构
```

### 接口层：`app/api/v1/`

```text
app/api/v1/
├── auth.py                 # 认证与令牌接口
├── channels.py             # 模型渠道与模型条目接口
├── chat.py                 # 对话、会话、消息与任务接口
├── files.py                # 文件接口
├── knowledge_base.py       # 知识库接口
├── memories.py             # 长期记忆管理接口
├── message_platforms.py    # 消息平台接口
├── profile.py              # Profile 配置接口
├── prompts.py              # Prompt 管理接口
├── scheduled_tasks.py      # 定时任务接口
├── setup.py                # 系统初始化接口
├── system.py               # 系统设置、日志与语言接口
└── users.py                # 用户管理接口
```

接口层负责对外协议和访问入口，业务能力由 `app/core/` 提供，接口数据结构由 `app/schemas/` 描述。

Profile 删除采用影响预览和确认 token。确认后删除当前绑定会话、定时任务和托管知识库；消息平台与用户知识库仅解除绑定。Profile 行锁阻止删除期间新增运行引用，会话清理由 `session_cleanup.py` 统一执行。

## 核心层：`app/core/`

```text
app/core/
├── audit/                  # 审计领域与审计持久化
├── background_tasks/       # 通用后台任务应用服务
├── crud/                   # 持久化数据访问
├── dispatchers/            # 对话分发应用服务
├── embedding/              # 知识库嵌入能力
├── i18n/                   # 后端多语言
├── knowledge/              # 托管知识关系主数据领域服务
├── knowledge_jobs/         # 知识异步发布与清理作业组件
├── memory/                 # 长期记忆领域与管理服务
├── memory_jobs/            # 长期记忆作业组件
├── message_platforms/      # 消息平台应用服务
├── rerank/                 # 知识库重排能力
├── retrieval/              # 知识库检索能力
├── session_reply_queue/    # 会话回复队列
├── terminal/               # 交互终端与 PTY 能力
├── tools/                  # 模型可调用工具
├── utils/                  # 通用辅助组件
├── channel_model_protection.py # 渠道与模型引用管理
├── channel_router.py       # 模型渠道路由
├── constants.py            # 核心常量
├── context.py              # 对话上下文
├── crypto.py               # 通用敏感数据处理
├── dispatch_context.py     # 工具分发上下文
├── dispatcher.py           # 对话分发入口
├── event_loop.py           # 平台事件循环支持
├── exceptions.py           # 应用异常类型
├── log.py                  # 日志服务
├── log_broadcaster.py      # 日志广播
├── knowledge_base_collection_cleanup.py # 知识库 collection 持久化清理队列处理服务
├── paths.py                # 数据和临时目录定义
├── profile_deletion.py     # Profile 删除影响预览、确认与级联清理
├── profile_selection.py    # Profile 选择服务
├── profile_validation.py   # Profile 配置检查
├── prompts.py              # 内置提示内容
├── security.py             # 认证与安全辅助
├── session_cleanup.py      # 会话清理服务
├── session_notifier.py     # 会话事件通知
├── session_source.py       # 会话来源信息
├── setup.py                # 系统初始化应用服务
├── system_secrets.py       # 系统密钥管理
└── validation.py           # 通用输入校验
```

### 审计：`app/core/audit/`

```text
app/core/audit/
├── __init__.py             # 审计能力导出
├── confirmation.py         # 审计确认数据
├── integrity.py            # 审计完整性数据
├── persistence.py          # 审计持久化协调
├── service.py              # 审计应用服务
├── startup.py              # 审计启动支持
└── storage.py              # 审计文件存储
```

### 通用后台任务：`app/core/background_tasks/`

```text
app/core/background_tasks/
├── __init__.py             # 后台任务能力导出
├── manager.py              # 后台任务管理
├── recovery.py             # 后台任务恢复支持
├── reply_trigger.py        # 任务回复协调
├── runner.py               # 后台任务执行组件
├── scheduler.py            # 定时任务调度
└── schemas.py              # 后台任务内部数据结构
```

### 托管知识领域：`app/core/knowledge/`

```text
app/core/knowledge/
├── __init__.py             # 托管知识领域能力导出
├── bindings.py             # Profile 的用户知识库绑定业务边界
├── embedding_migration.py  # 用户知识库手动迁移与托管知识随长期记忆修订自动迁移
├── errors.py               # 托管知识领域异常
├── managed.py              # 托管知识条目、版本与维护边界领域服务
├── managed_container.py    # 按 Profile 懒创建/复用托管知识库并复制长期记忆当前嵌入运行态
├── migration.py            # 知识库迁移期间的增量变更记录边界
├── recall.py               # 托管知识候选校验、去重与最终主数据还原
└── results.py              # 托管知识领域操作结果与状态
```

该目录承载 LLM 托管知识主数据。正文和版本历史以关系库为事实源，向量仅作派生索引。首次写入按 Profile 懒创建唯一托管知识库并绑定 Profile；容器、绑定、首条知识和发布作业在同一短事务提交。召回最终由关系库版本、墓碑和 `is_recallable` 判定。

Profile 的手工知识库绑定只管理 `USER` 类型；`LLM_MANAGED` 绑定由托管知识生命周期维护，保存 Profile 或用户知识库绑定时不得删除或改绑托管关系。

知识库运行时嵌入配置由 `knowledge_base_runtime.py` 统一解析。`active_*` 是当前生效配置，legacy 字段仅作旧数据回退。查询、写入、发布和清理必须使用同一解析结果；渠道与模型删除保护同时覆盖当前和目标迁移配置。

用户知识库和托管知识库共用在线嵌入迁移引擎。迁移以关系数据库完整正文为事实源，先锁定快照边界并分页构建目标集合，迁移期间的关系数据变化写入单调增量日志；目标集合通过数量、标识、元数据、维度和抽样查询校验后，复用锁外最终计划并以增量水位作写入栅栏，在短事务内批量更新必要向量引用并切换 `active_*`，事务内不再全量读取或重新切分正文。旧集合清理成功前禁止开始下一次嵌入迁移，避免覆盖清理上下文；取消或失败不会改变当前生效集合。

用户知识库可由管理页面独立选择目标嵌入渠道和模型并提交迁移，与长期记忆开关无关；托管知识库不接受独立模型选择。长期记忆嵌入修订原子切换时，先持有该用户长期记忆状态锁，再以锁定读取获取最新已提交的全部托管知识库，并在同一事务分别提交迁移作业；托管知识库首次创建也必须先取得同一长期记忆状态锁，因此切换前已创建的容器不会因 MySQL 可重复读快照遗漏，切换期间的新建请求则会在切换提交后读取新的生效配置。托管知识库跟随迁移的可重试失败最多连续尝试 3 次，重试不退避；达到上限后保留原生效嵌入配置并进入失败状态。作业状态按知识库隔离。

### 知识作业：`app/core/knowledge_jobs/`

```text
app/core/knowledge_jobs/
├── __init__.py             # 知识作业能力导出
├── manager.py              # 短事务提交、幂等与活动变更占用
├── consumer.py             # 作业消费、租约续期、恢复与重试
├── executor.py             # 租约栅栏与处理器执行器
├── handlers.py             # 托管知识异步嵌入、发布和删除清理
├── migration.py            # 通用知识库嵌入迁移、校验、原子切换与旧集合清理
└── vector_cleanup.py       # staged/superseded 向量的持久化独立清理作业
```

知识作业与长期记忆作业模型独立，但复用租约、重试和旧 Worker 栅栏原则。数据库短事务只负责预留、校验和发布；嵌入与向量调用在事务外执行。staged/superseded 清理均持久化为可恢复作业，Consumer 启动时恢复过期租约。删除先墓碑化停止召回，再异步清理向量与条目。

### 长期记忆作业：`app/core/memory_jobs/`

```text
app/core/memory_jobs/
├── __init__.py             # 记忆作业能力导出
├── manager.py              # 作业管理
├── consumer.py             # 作业消费与租约协调
├── executor.py             # 作业执行器
├── handlers.py             # 记忆作业处理器
├── organization_handler.py # 记忆整理模型调用作业处理器
├── maintenance_handlers.py # 记忆维护作业处理器
├── maintenance_lifecycle.py # 维护作业生命周期组件
├── maintenance_state.py    # 维护作业状态数据
├── maintenance_vector.py   # 维护作业向量组件
├── vector_cleanup.py       # 记忆向量清理作业组件
├── migration_handler.py    # 嵌入模型迁移处理器
└── reindex_handler.py      # 向量索引处理器
```

该目录承载长期记忆的独立作业能力，与长期记忆领域服务和 Worker 进程协作。

### 长期记忆领域：`app/core/memory/`

```text
app/core/memory/
├── __init__.py             # 长期记忆公开入口
├── errors.py               # 记忆领域异常
├── capacity.py             # 长期记忆容量状态
├── results.py              # 记忆领域结果类型
├── normalization.py        # 记忆数据规范化
├── identifiers.py          # 记忆持久化标识
├── service.py              # 长期记忆领域服务
├── management.py           # 长期记忆管理应用服务
├── management_helpers.py   # 记忆管理辅助组件
├── embedding_config.py     # 记忆嵌入配置服务
├── maintenance.py          # 记忆维护服务
├── organization_types.py   # 记忆整理内部契约
├── organization.py         # 记忆整理领域策略
└── channel_protection.py   # 记忆渠道与模型引用管理
```

长期记忆领域由 `service.py` 提供核心领域服务，由 `management.py` 面向管理接口组织管理能力；`management_helpers.py` 提供管理层共用的数据辅助能力。

长期记忆召回由已发布主动记忆检索和用户级历史聊天稀疏检索两个协作部分组成。已发布主动记忆检索提供优先的主结果及其管理标识；历史聊天稀疏检索只提供次要上下文，不进入记忆变更链路，也不能挤占主动记忆结果。

### 会话回复队列：`app/core/session_reply_queue/`

```text
app/core/session_reply_queue/
├── __init__.py             # 队列能力导出
├── consumer.py             # 回复工作消费
├── executor.py             # 回复工作执行
└── manager.py              # 回复工作管理
```

### 交互终端：`app/core/terminal/`

```text
app/core/terminal/
├── __init__.py             # 终端能力导出
├── schemas.py              # 终端协议数据结构
├── manager.py              # 终端会话管理
├── process_config.py       # 终端进程配置
├── pty_base.py             # PTY 抽象
├── pty_factory.py           # PTY 驱动创建
├── pty_unix.py              # Unix PTY 驱动
├── pty_windows.py           # Windows PTY 驱动
└── recovery.py              # 终端资源恢复支持
```

### 消息平台：`app/core/message_platforms/`

```text
app/core/message_platforms/
├── __init__.py             # 消息平台能力导出
├── base.py                 # 平台处理器抽象
├── inbound_collector.py    # 入站消息收集
├── manager.py              # 消息平台管理
├── notifier.py             # 平台事件通知
├── outbound_text.py        # 出站文本适配
├── tool_output.py          # 工具输出适配
└── weixin_openclaw.py      # 微信平台处理器
```

### 数据访问：`app/core/crud/`

```text
app/core/crud/
├── base.py                     # 通用数据访问抽象
├── account/
│   └── user.py                 # 用户数据访问
├── audit/
│   ├── audit.py                # 审计数据访问
│   └── tool_result_version.py  # 审计结果版本数据访问
├── channel/
│   ├── channel.py              # 渠道和模型数据访问
│   └── cursor.py               # 渠道路由数据访问
├── context_summary/
│   ├── fragment.py             # 上下文总结片段数据访问
│   └── stage.py                # 上下文总结阶段数据访问
├── knowledge/
│   ├── base.py                 # 知识库数据访问
│   ├── embedding_transition.py # 知识库嵌入模型切换数据访问
│   ├── job.py                  # 知识作业数据访问
│   └── managed.py              # 托管知识数据访问
├── memory/
│   ├── store.py                # 长期记忆数据访问
│   ├── job.py                  # 长期记忆作业数据访问
│   └── maintenance.py          # 长期记忆维护数据访问
├── message_platform/
│   ├── platform.py             # 消息平台数据访问
│   └── outbox.py               # 消息发件箱数据访问
├── profile/
│   ├── profile.py              # Profile 数据访问
│   └── prompt.py               # Prompt 数据访问
├── session/
│   ├── message.py              # 消息数据访问
│   ├── session.py              # 会话数据访问
│   ├── event.py                # 会话事件数据访问
│   ├── reply_provider_usage.py # 回复 Provider Usage 持久化
│   ├── reply_stream_event.py   # 回复流事件数据访问
│   └── reply_work_item.py      # 回复工作数据访问
├── system/
│   ├── log.py                  # 系统日志数据访问
│   └── setting.py              # 系统设置数据访问
├── task/
│   ├── background.py           # 后台任务数据访问
│   └── scheduled.py            # 定时任务数据访问
├── terminal/
│   └── session.py              # 终端会话数据访问
└── worker/
    └── lease.py                # Worker 协调数据访问
```

### 对话分发：`app/core/dispatchers/`

```text
app/core/dispatchers/
├── __init__.py             # 分发器导出
├── background.py           # 后台对话分发
├── interactive.py          # 交互式对话分发
├── interactive_helpers.py  # 交互式分发辅助
├── memory/                 # 长期记忆召回组件
│   ├── __init__.py         # 记忆召回公开导出
│   ├── persistence.py      # 记忆召回持久化组件
│   ├── recall.py           # 对话记忆召回集成
│   ├── request.py          # 记忆召回请求组件
│   └── types.py            # 记忆召回数据结构
├── non_stream.py           # 非流式对话分发
├── shared.py               # 分发器共用组件
└── stream.py               # 流式对话分发
```

### 知识库能力

```text
app/core/embedding/
├── __init__.py             # 嵌入能力导出
├── common.py               # 嵌入共用组件
└── knowledge_base.py       # 知识库嵌入服务

app/core/rerank/
├── __init__.py             # 重排能力导出
├── knowledge_base.py       # 知识库重排服务
└── schemas.py              # 重排数据结构

app/core/retrieval/
├── __init__.py             # 检索能力导出
├── fusion.py               # 检索结果融合
├── hybrid.py               # 混合检索
├── schemas.py              # 检索数据结构
├── sparse.py               # 稀疏检索
└── tokenizer.py            # 检索分词
```

### 工具与通用组件

```text
app/core/tools/
├── __init__.py             # 工具注册入口
├── base.py                 # 工具抽象
├── cancel_background_task.py # 后台任务工具
├── file_writer.py          # 文件写入工具
├── firecrawl_scrape.py     # 网页抓取工具
├── firecrawl_search.py     # 网页搜索工具
├── image_generation.py     # 图像生成工具
├── knowledge_base_query.py # 知识库查询与托管知识可信元数据
├── list_background_tasks.py # 后台任务查询工具
├── longterm_memory.py      # 统一记忆工具：个人记忆与托管知识维护
├── read_text_file.py       # 文本文件读取工具
├── read_multimodal_file.py # 多模态文件读取工具
├── send_file_to_user.py    # 文件发送工具
├── terminal.py             # 交互终端工具
└── shell.py                # Shell 工具

app/core/utils/
├── dispatcher/             # 对话分发辅助
├── context_summary/        # 上下文总结辅助
├── assistant_files.py      # 助手文件辅助
├── background_task_result.py # 后台任务结果辅助
├── channel_profile_sync.py # 渠道与 Profile 同步辅助
├── config.py               # 配置读取辅助
├── context_budget.py       # 上下文预算辅助
├── context_messages.py     # 上下文消息辅助
├── http_proxy.py           # HTTP 代理辅助
├── message_assembler.py    # 消息组装辅助
├── message_parser.py       # 消息解析辅助
├── model_request_headers.py # 模型请求头辅助
├── operation_directories.py # 文件系统目录辅助
├── request_token_baseline.py # 请求令牌统计辅助
├── session.py              # 会话辅助
├── system.py               # 系统信息辅助
├── text_splitter.py        # 文本切分辅助
├── time.py                 # 时间辅助
└── tokenizer.py            # 令牌统计辅助
```

统一记忆工具保留个人长期记忆操作，并维护当前 Profile 的托管知识。托管知识更新/删除仅接受可信召回的知识标识与版本；用户知识库和聊天历史只读。托管向量分块命中经关系库校验后按知识去重并还原完整正文，工具日志不记录查询或知识正文。

### 多语言：`app/core/i18n/`

```text
app/core/i18n/
├── __init__.py             # 多语言能力导出
├── context.py              # 语言上下文
├── locale.py               # 语言结构
├── translator.py           # 翻译服务
└── locales/                # 后端语言资源
```

## 持久化模型：`app/models/`

```text
app/models/
├── __init__.py             # 模型导出
├── audit.py                # 审计模型
├── background_task.py      # 后台任务模型
├── channel.py              # 渠道与模型条目模型
├── channel_cursor.py       # 渠道路由模型
├── context_summary_stage.py # 上下文总结模型
├── knowledge_base.py       # 知识库、用户文档、托管知识、迁移增量、版本历史与知识作业模型
├── memory.py               # 长期记忆模型
├── message.py              # 消息模型
├── message_platform.py     # 消息平台模型
├── message_platform_outbox.py # 消息发件箱模型
├── profile.py              # Profile 模型
├── prompt.py               # Prompt 模型
├── scheduled_task.py       # 定时任务模型
├── session.py              # 会话模型
├── session_event.py        # 会话事件模型
├── session_reply_stream_event.py # 回复流事件模型
├── session_reply_provider_usage.py # 回复请求 Provider Usage 去重模型
├── session_reply_work_item.py # 回复工作模型
├── system_log.py           # 系统日志模型
├── system_setting.py       # 系统设置模型
├── terminal_session.py     # 终端会话模型
├── user.py                 # 用户模型
└── worker_lease.py         # Worker 协调模型
```

`app/models/` 定义关系型持久化对象，由 `app/core/crud/` 提供访问，由 `app/providers/database/` 提供数据库连接和初始化能力。

`app/models/knowledge_base.py` 定义知识库容器、用户文档、托管知识、版本历史和知识作业。托管条目保存正文、稳定标识、版本、来源和维护状态；版本历史独立保存快照。知识作业保存幂等、租约、重试和目标版本，不携带知识正文；向量分块仅作派生索引。

## 外部能力封装：`app/providers/`

```text
app/providers/
├── database/
│   ├── __init__.py         # 数据库能力导出
│   ├── bootstrap.py        # 数据库初始化
│   ├── client.py           # 异步数据库连接与会话
│   └── time.py             # 数据库时间能力
├── embedding/
│   ├── __init__.py         # 嵌入能力导出
│   └── client.py           # 嵌入模型客户端
├── image_generation/
│   ├── __init__.py         # 图像生成能力导出
│   └── client.py           # 图像生成客户端
├── llm/
│   ├── __init__.py         # 大模型能力导出
│   └── client.py           # 大模型客户端
├── rerank/
│   ├── __init__.py         # 重排能力导出
│   └── client.py           # 重排模型客户端
└── vector/
    ├── __init__.py         # 向量存储能力导出
    └── chroma.py           # Chroma 向量存储适配
```

Provider 层隔离数据库、向量存储、语言模型、嵌入、重排和图像生成等外部依赖。

## 接口数据与协议转换

```text
app/schemas/
├── auth.py                 # 认证接口数据结构
├── background_task.py      # 后台任务接口数据结构
├── memory.py               # 长期记忆接口数据结构
├── response.py             # 通用响应和分页结构
├── scheduled_task.py       # 定时任务接口数据结构
└── setup.py                # 系统初始化接口数据结构

app/transformers/
├── base.py                 # 模型协议转换抽象
├── cohere_rerank.py        # Cohere 重排协议转换
└── openai/
    ├── __init__.py         # OpenAI 转换器导出
    ├── base.py             # OpenAI 协议转换基础组件
    ├── chat_completions.py # Chat Completions 协议转换
    ├── embedding.py        # 嵌入协议转换
    ├── image_generation.py # 图像生成协议转换
    └── responses.py        # Responses 协议转换
```

`schemas` 描述应用对外的数据契约，`transformers` 隔离不同外部模型协议与内部模型表示。

## 前端分层：`dashboard/`

```text
dashboard/
├── devServerProxy.cjs       # 从项目根 .env 解析 Vue 开发代理目标
├── public/                 # 静态资源模板
├── src/                    # Vue 源码
├── tests/                  # 前端自动化测试
├── vue.config.js           # Vue 构建配置，npm run build 输出到 app/static/dashboard/
├── package-lock.json       # 前端依赖锁定
└── package.json            # 前端依赖与脚本配置
```

开发服务器将同源 `/api` 的 HTTP/WebSocket 请求代理到 `APP_HOST`/`APP_PORT` 指定的后端；生产环境仍由 FastAPI 托管预构建资源。

### 前端源码：`dashboard/src/`

```text
dashboard/src/
├── api/                    # HTTP 与 WebSocket 请求封装
├── assets/                 # 样式与图标
├── components/             # 通用界面组件
├── composables/            # 可复用组合逻辑
├── constants/              # 前端常量
├── i18n/                   # 前端多语言
├── router/                 # 页面路由
├── utils/                  # 前端通用辅助
├── views/                  # 页面组件
├── App.vue                 # 根组件
└── main.js                 # 前端入口
```

```text
dashboard/src/views/
├── BackendUnavailableView.vue # 后端服务不可用时展示的独立错误页面
├── ChannelsView.vue        # 渠道管理页面
├── ChatView.vue            # 聊天页面
├── HistoryLogs.vue         # 历史日志页面
├── KnowledgeBase.vue       # 知识库管理页面
├── LoginView.vue           # 登录页面
├── MemoriesView.vue        # 长期记忆数据、作业与运行状态页面，配置入口集中到 Profile 记忆设置
├── MessagePlatformsView.vue # 消息平台管理页面
├── ProfilesView.vue        # Profile 管理页面，协调 Profile 级记忆参数与用户级自动整理设置
├── PromptsView.vue         # Prompt 管理页面
├── RealTimeLogs.vue        # 实时日志页面
├── ScheduledTasksView.vue  # 定时任务管理页面
├── SetupView.vue           # 系统初始化页面
└── UsersView.vue           # 用户管理页面
```

`dashboard/src/components/ProfileFormDialog.vue` 的记忆设置页集中编辑 Profile 级召回参数、嵌入配置和用户级自动整理设置；自动整理数据仍由后端按用户唯一保存，避免同一用户的多个 Profile 产生冲突。

`dashboard/src/api/` 对应后端接口层，`components/` 提供共享界面组件，`composables/` 提供页面共用逻辑，`router/`、`i18n/` 和 `utils/` 分别承载路由、多语言和前端辅助能力。

## 测试与脚本

```text
tests/                      # 后端单元测试与集成测试
dashboard/tests/            # 前端自动化测试
scripts/                    # 数据库迁移和运行维护脚本
```

测试目录和脚本目录只作为工程支持层，不在架构文档中展开文件清单。

数据库一次性迁移由 `app/providers/database/bootstrap.py` 按 `migration_*.py` 文件名顺序逐个执行并在成功后登记。历史迁移不得依赖未来版本 ORM 的表结构；表重建类迁移应基于执行时数据库自描述结构或版本冻结的迁移 schema 工作，保证用户可以从较老版本直接跨多个版本升级而不被后续模型字段反向阻塞。

## 持久化目录：`data/` 与 `temp/`

```text
data/                       # SQLite、Chroma、日志与审计等持久化数据
├── audit/                  # 审计文件
├── system_secrets.json     # 系统密钥持久化文件
└── system_secrets.lock     # 系统密钥初始化锁文件

temp/                       # 上传文件、工具结果和其他临时数据
```

关系型数据由 `app/providers/database/` 管理，向量数据由 `app/providers/vector/` 管理，文件型持久化由核心服务和路径组件管理。

## 依赖方向

```text
dashboard / adapters
        ↓
api / application entry
        ↓
core application and domain services
        ↓
crud / models / providers / transformers
        ↓
database / vector storage / external model services
```

- `dashboard` 依赖后端公开接口，不依赖后端内部模块。
- `adapters` 和 `api` 是后端入口层，依赖 `core` 与 `schemas`。
- `core` 组织应用服务和领域能力，依赖数据访问、持久化模型、Provider 及协议转换组件。
- `crud` 面向 `models` 和数据库 Provider，负责持久化访问抽象。
- `providers` 隔离外部服务和持久化基础设施。
- `workers` 使用核心服务和持久化抽象，不依赖前端模块。

## 进程边界

- Web 进程同时承载 FastAPI 接口、WebSocket 接口、对话适配入口和预构建 Dashboard 静态资源。
- 后台 Worker 进程承载通用任务、长期记忆作业、消息平台、会话回复和交互终端等独立能力。
- Web 进程与 Worker 进程通过核心服务及持久化资源共享应用数据，不直接形成前端依赖。
- 数据库、向量存储、日志、审计文件和临时文件构成运行期资源边界。
