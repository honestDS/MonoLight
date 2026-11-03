# MonoLight 项目架构说明

本文只描述项目的目录分层、模块职责、持久化边界和依赖方向，不记录具体接口格式、执行流程或实现规则。

## 顶层目录

```text
Monolight/
├── app/                    # FastAPI 后端源码
├── dashboard/              # Vue 管理与聊天前端源码
├── data/                   # 运行期持久化数据
├── docs/                   # 项目展示图片等文档资源
├── scripts/                # 数据库迁移与维护脚本
├── temp/                   # 运行期临时文件
├── tests/                  # 后端自动化测试
├── .github/                # GitHub Issue 模板与仓库协作配置
├── .env                    # 本地运行配置
├── .gitattributes          # Git 属性配置
├── .gitignore              # Git 忽略配置
├── ARCHITECTURE.md         # 项目架构说明
├── DEVELOPMENT_GUIDE.md    # 开发规范
├── LICENSE                 # 项目许可证
├── README.md               # 英文项目说明
├── README_ZH.md            # 中文项目说明
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
├── __init__.py             # 应用包标识
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
├── handler.py              # HTTP 中间件、异常处理与图标路由装配
├── https_certificates.py   # 网络通信安全凭证与根证书加载
├── tasks.py                # 临时文件、用户文件与日志清理任务
└── warning_filters.py      # 运行时告警过滤
```

### 应用入口与 Worker

`main.py` 提供 Web 应用入口；`start.py` 负责启动协调，先通过短生命周期初始化子进程完成系统初始化，再启动 Web 进程和长期后台进程。后台服务启动时具备异常中断任务清理协调机制，确保历史未完成任务状态一致后再开展业务处理。

```text
app/workers/
├── __init__.py             # Worker 包标识
├── general.py              # 消息平台、通用后台任务和会话回复三个独立租约角色的合并进程入口
├── background_task.py      # 通用后台任务角色实现与独立调试入口（默认由 general 合并运行）
├── lease.py                # Worker 协调支持
├── memory.py               # 长期记忆与知识作业 Worker
├── message_platform.py     # 消息平台与定时任务角色实现与独立调试入口（默认由 general 合并运行）
├── session_reply.py        # 会话回复角色实现与独立调试入口（默认由 general 合并运行）
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

### 接口层共享组件：`app/api/`

```text
app/api/
└── knowledge_input.py       # 知识录入请求体限制与上传文件的有界流式读取
```

该层只承载跨知识库接口复用的输入边界处理；具体业务校验和持久化仍由 `app/core/` 与 `app/models/` 负责。

### 接口层：`app/api/v1/`

```text
app/api/v1/
├── auth.py                 # 认证与令牌接口
├── channels.py             # 模型渠道与模型条目接口
├── chat.py                 # 对话、会话、消息与任务接口
├── files.py                # 文件上传、下载与发送文件访问接口
├── knowledge_base.py       # 知识库接口
├── memories.py             # 长期记忆管理接口
├── message_platforms.py    # 消息平台接口
├── profile.py              # Profile 配置接口
├── prompts.py              # Prompt 管理接口
├── scheduled_tasks.py      # 定时任务管理与执行时间预览接口
├── setup.py                # 系统初始化接口
├── system.py               # 系统设置、日志、语言与共享技能接口
└── users.py                # 用户管理接口
```

接口层负责对外协议和访问入口，业务能力由 `app/core/` 提供，接口数据结构由 `app/schemas/` 描述。

## 核心层：`app/core/`

```text
app/core/
├── audit/                  # 审计领域与审计持久化
├── background_tasks/       # 后台工具执行、外部观察与结果回传
├── channel/                # 渠道与模型引用管理
├── crud/                   # 持久化数据访问
├── dispatchers/            # 对话分发应用服务
├── embedding/              # 知识库嵌入能力
├── i18n/                   # 后端多语言
├── knowledge/              # 知识主数据、绑定、迁移与统一召回领域服务
├── knowledge_jobs/         # 知识异步发布与清理作业组件
├── log/                    # 日志服务与日志处理
├── memory/                 # 长期记忆领域与管理服务
├── memory_jobs/            # 长期记忆作业组件
├── message_platforms/      # 消息平台应用服务
├── profile/                # Profile 配置与生命周期服务
├── rerank/                 # 知识库重排能力
├── retrieval/              # 知识库检索能力
├── session/                # 会话支撑服务
├── session_reply_queue/    # 前台回复与独立任务工作队列
├── setup/                  # 系统初始化服务
├── terminal/               # 交互终端与 PTY 能力
├── tools/                  # 模型可调用工具
├── user/                   # 用户生命周期服务
├── utils/                  # 通用辅助组件与内部任务模型请求策略
├── constants.py            # 核心常量
├── context.py              # 主会话历史、工具链对齐与模型可见内容处理
├── crypto.py               # 通用敏感数据处理
├── event_loop.py           # 平台事件循环支持
├── exceptions.py           # 应用异常类型
├── paths.py                # 运行数据、临时文件与发布资源路径
├── prompts.py              # 对话、审计与任务授权提示内容
├── security.py             # 认证与安全辅助
├── skills.py               # 共享技能发现、目录浏览与管理员删除
├── system_secrets.py       # 系统密钥管理
└── validation.py           # 通用输入校验
```

### 核心领域包

```text
app/core/channel/
├── __init__.py             # 渠道领域包标识
├── router.py               # 模型渠道路由
└── model_protection.py     # 模型引用保护、整理配置同步与延迟删除

app/core/log/
├── __init__.py             # 日志服务主实现
├── broadcaster.py          # 日志广播
└── redaction.py            # 日志敏感信息脱敏

app/core/profile/
├── __init__.py             # Profile 领域包标识
├── deletion.py             # Profile 删除影响预览、确认与级联清理
├── permissions.py          # Profile 配置权限与受限字段合并
├── selection.py            # 会话覆盖、平台绑定与默认 Profile 选择
└── validation.py           # Profile 配置检查

app/core/session/
├── __init__.py             # 会话领域包标识
├── cleanup.py              # 会话清理服务
├── notifier.py             # 会话事件通知
└── source.py               # 会话来源信息

app/core/setup/
├── __init__.py             # 系统初始化包标识
├── service.py              # 系统初始化应用服务
└── session.py              # 初始化会话令牌签发与校验

app/core/user/
├── __init__.py             # 用户领域包标识
├── deletion.py             # 用户删除服务
└── file_cleanup.py         # 用户文件清理
```

### 审计：`app/core/audit/`

```text
app/core/audit/
├── __init__.py             # 审计能力导出
├── confirmation.py         # 审计确认兼容导出入口
├── confirmation_common.py  # 确认枚举、状态更新与投影数据结构
├── confirmation_decision.py # 确认候选识别与决定解析
├── confirmation_events.py  # 确认状态及工具结果事件通知
├── confirmation_lifecycle.py # 确认过期、取消与会话清理
├── confirmation_persistence.py # 待确认数据包及取消结果持久化
├── confirmation_projection.py # 确认状态的消息投影同步
├── confirmation_queries.py # 确认消息与结构化结果内部查询
├── confirmation_results.py # 待确认工具结果读取、替换与终态更新
├── context_budget.py       # 审计模型请求预算与证据裁剪
├── integrity.py            # 审计完整性数据
├── persistence.py          # 审计持久化协调
├── service.py              # 工具风险审计、任务授权与人工确认
├── startup.py              # 审计启动支持
├── storage.py              # 审计文件存储
└── visibility.py           # 面向主模型对审计工具结果做安全可见性裁剪/隐藏
```

### 通用后台任务：`app/core/background_tasks/`

```text
app/core/background_tasks/
├── __init__.py             # 后台任务能力导出
├── external_work.py        # 外部异步操作挂起、恢复与结果收集
├── manager.py              # 后台任务管理
├── policy.py               # 图像生成、终端观察与非交互 Shell 后台策略
├── recovery.py             # 后台任务恢复支持与启动检查
├── reply_trigger.py        # 任务回复协调
├── result_delivery.py      # 外部与后台执行结果交付
├── runner.py               # 后台任务执行组件
├── scheduler.py            # 间隔/Cron 调度、会话配置解析与独立任务入队
├── schemas.py              # 后台任务内部数据结构
├── terminal_watch_coordinator.py # 终端自动观察、上下文冻结与结果协调
└── terminal_watch_reply.py # 终端观察通知回传与回复路由
```

### 知识领域：`app/core/knowledge/`

```text
app/core/knowledge/
├── __init__.py             # 知识领域能力导出
├── bindings.py             # Profile 的用户知识库绑定业务边界
├── deletion.py             # 用户知识库与托管知识库删除权限及生命周期边界
├── documents.py            # 用户知识库文档导入与分块向量化处理
├── embedding_migration.py  # 用户知识库手动迁移与托管知识随长期记忆修订自动迁移
├── errors.py               # 托管知识领域异常
├── input_limits.py         # 知识正文字符、字节与输入边界校验
├── managed.py              # 托管知识条目、版本与维护边界领域服务
├── managed_container.py    # 按 Profile 懒创建/复用托管知识库并复制长期记忆当前嵌入运行态
├── migration.py            # 知识库迁移期间的增量变更记录边界
├── organization.py         # 托管知识整理快照冻结、分页读取与稳定工作身份
├── organization_analysis.py # 整理分析阶段及上下文压缩
├── organization_executor.py # 整理执行能力兼容导出入口
├── organization_pipeline.py # 有界并发整理管线
├── organization_plan.py    # 整理计划阶段执行
├── organization_publication.py # 整理计划发布与子作业编排
├── organization_reduction.py # 多轮整理归约与收敛校验
├── organization_run.py     # 整理作业运行、租约与终态协调
├── organization_lifecycle.py # 整理父子作业终态、取消与锁释放协调
├── organization_runtime.py # 模型配置、调用与语义邻居运行支持
├── organization_scope.py   # 整理范围构建、分组与暂存
├── organization_stages.py  # 整理阶段、片段与本任务连续前缀持久化
├── organization_types.py   # 整理计划、范围与执行结果契约
├── recall.py               # 托管知识候选校验、去重与最终主数据还原
├── results.py              # 托管知识操作及统一知识召回结果模型
└── unified_recall.py       # 托管知识与用户知识库候选合并、全局重排和结果组装
```

知识正文的录入边界由 `app/core/constants.py` 统一定义，入口层负责限制实际接收量，知识领域负责校验解码后的正文，文档切分负责限制分块资源消耗。统一召回保留命中知识的完整正文；模型上下文容量与超限恢复由 Dispatcher 和上下文总结组件负责，不由知识领域截断正文。

### 知识作业：`app/core/knowledge_jobs/`

```text
app/core/knowledge_jobs/
├── __init__.py             # 知识作业能力导出
├── manager.py              # 短事务提交、幂等与活动变更占用
├── consumer.py             # 作业消费、租约续期、恢复与重试
├── executor.py             # 租约栅栏与处理器执行器
├── handlers.py             # 托管知识异步嵌入、发布和删除清理
├── maintenance.py          # 启动一致性巡检、过期整理历史和孤儿向量维护
├── collection_cleanup.py   # 已删除知识库 collection owner 持久化清理处理服务
├── migration.py            # 知识库嵌入迁移兼容导出入口
├── migration_build.py      # 目标集合构建与增量追平
├── migration_common.py     # 迁移契约、载荷校验与向量计划
├── migration_handlers.py   # 迁移、取消、终态、失败目标与旧集合清理处理器
├── migration_prepare.py    # 迁移准备、配置锁定与作业提交
├── migration_switch.py     # 目标校验、增量栅栏与原子切换
└── vector_cleanup.py       # staged/superseded 向量的持久化独立清理作业
```

### 长期记忆作业：`app/core/memory_jobs/`

```text
app/core/memory_jobs/
├── __init__.py             # 记忆作业能力导出
├── consumer.py             # 作业消费与租约协调
├── executor.py             # 作业执行器
├── handlers.py             # 记忆作业处理器兼容导出入口
├── handler_cleanup.py      # 终态向量与占位记录清理
├── handler_contracts.py    # 处理器上下文、快照与错误契约
├── handler_execution.py    # 作业类型分派与执行协调
├── handler_factory.py      # 处理器集合及默认执行器装配
├── handler_organization.py # 整理合并发布
├── handler_organization_validation.py # 整理与删除载荷校验
├── handler_prepare.py      # 发布、替换、整理及删除执行前准备
├── handler_publication.py  # 记忆版本发布与替换
├── handler_publication_validation.py # 发布状态、容量与唯一性校验
├── handler_replacement_validation.py # 替换载荷及快照校验
├── handler_vector.py       # 向量生成、元数据与补偿删除
├── organization_handler.py # 记忆整理模型调用作业处理器
├── maintenance_handlers.py # 记忆维护作业处理器
├── maintenance_lifecycle.py # 维护作业生命周期组件
├── maintenance_state.py    # 维护作业状态数据
├── maintenance_vector.py   # 维护作业向量组件
├── vector_cleanup.py       # 记忆向量清理作业组件
├── migration_handler.py    # 嵌入模型迁移处理器
├── reindex_handler.py      # 向量索引处理器
├── manager.py              # 作业管理兼容导出与组合入口
├── manager_cleanup.py      # 终态作业清理提交
├── manager_common.py       # 提交结果、异常与幂等校验
├── manager_control.py      # 取消及控制操作
├── manager_organization.py # 自动与手动整理提交
├── manager_organization_child.py # 整理子作业提交
├── manager_query.py        # 作业状态查询
└── manager_submission.py   # 普通变更作业提交
```

该目录承载长期记忆的独立作业能力，与长期记忆领域服务和 Worker 进程协作。

### 长期记忆领域：`app/core/memory/`

```text
app/core/memory/
├── __init__.py             # 长期记忆公开入口
├── errors.py               # 记忆领域异常
├── capacity.py             # 长期记忆容量状态
├── chat_history.py         # 历史聊天稀疏召回
├── results.py              # 记忆领域结果类型
├── normalization.py        # 记忆数据规范化
├── identifiers.py          # 记忆持久化标识
├── service.py              # 长期记忆领域服务组合入口
├── service_common.py       # 领域服务共用加载、校验与提交支持
├── service_create.py       # 记忆创建
├── service_delete.py       # 记忆删除
├── service_embedding.py    # 嵌入变更增量记录
├── service_pin.py          # 记忆固定状态更新
├── service_recall.py       # 主动记忆召回
├── service_resume.py       # 暂停变更恢复
├── service_update.py       # 记忆更新
├── management.py           # 长期记忆管理应用服务
├── management_helpers.py   # 记忆管理辅助组件
├── embedding_config.py     # 记忆嵌入配置服务
├── maintenance.py          # 记忆维护服务
├── organization.py         # 记忆整理领域能力兼容导出入口
├── organization_config.py  # 整理设置与模型配置
├── organization_contracts.py # 整理执行载荷、预算与快照契约
├── organization_execution.py # 整理模型请求、调用与恢复
├── organization_payload.py # 整理执行载荷序列化
├── organization_pins.py    # 整理合并的固定记忆策略
├── organization_snapshot.py # 整理快照、摘要与作业载荷构建
├── organization_types.py   # 整理计划与来源类型
├── organization_validation.py # 整理输出、令牌预算与提交校验
└── channel_protection.py   # 记忆渠道与模型引用管理
```

### 会话回复队列：`app/core/session_reply_queue/`

```text
app/core/session_reply_queue/
├── __init__.py             # 队列能力导出
├── consumer.py             # 回复工作消费
├── executor.py             # 回复工作执行兼容导出入口
├── executor_agent_work.py  # 独立 AgentWork 指令执行与自身上下文恢复
├── executor_audit.py       # 审计执行绑定、未知终态与文件快照校验
├── executor_common.py      # 工作身份、去重键与响应事件辅助
├── executor_confirmed.py   # 已确认工具执行与原工作上下文继承
├── executor_interactive.py # 交互回复、独立工作隔离与流事件持久化
├── executor_lifecycle.py   # 工作执行、失败、重试与租约生命周期
├── executor_metadata.py    # 请求用量记录、跨工作汇总与主对话信息隔离
├── executor_replies.py     # 前台回复与冻结上下文的后台续接
├── executor_resume.py      # 交互工作 checkpoint 工具结果恢复
├── manager.py              # 回复工作管理组合入口
├── manager_common.py       # 请求标识、事件与失败处理辅助
├── manager_enqueue.py      # 回复工作入队
├── manager_freeze.py       # 用户输入冻结与工作边界确定
├── manager_result.py       # 工作结果等待与读取
└── manager_submission.py   # 用户消息提交、确认判定与工作上下文继承
```

`AgentWork` 是独立指令执行的共用底层：输入必须自包含，不读取所属主会话的实时历史；执行过程复用会话回复队列、交互式工具循环、审计、外部工作恢复与自身 checkpoint。结果交付按目标区分：`SESSION` 面向主会话直接展示最终结果，计划任务使用这一途径，内部工具过程保留在自身工作上下文；`PARENT_WORK` 面向父工作独立上报，为未来子代理机制预留，当前不开放执行。定时任务通过 `AgentWork` 执行，未来其他独立 Agent 调度场景可复用同一执行边界。

### 交互终端：`app/core/terminal/`

```text
app/core/terminal/
├── __init__.py             # 终端能力导出
├── schemas.py              # 终端协议数据结构
├── manager.py              # 终端管理兼容导出与组合入口
├── manager_common.py       # 管理常量与变更请求契约
├── audit_lifecycle.py      # 终端审计终态与会话级清理
├── session_manager.py      # 终端会话创建、读取、写入与关闭
├── session_runtime.py      # 单会话运行时内部组合类型
├── session_runtime_commands.py # 运行时写入、调整与关闭命令
├── session_runtime_common.py # 输出缓冲与租约异常辅助
├── session_runtime_core.py # PTY 启动、读取与状态转换
├── session_runtime_state.py # 会话运行状态、租约与进程身份同步
├── worker_coordinator.py   # Worker 认领、续租与资源回收协调
├── watcher.py              # 终端输出变化轮询观察器
├── watch_schema.py         # 终端观察请求数据结构
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
│   ├── file_cleanup.py         # 用户文件清理记录数据访问
│   └── user.py                 # 用户数据访问
├── audit/
│   ├── audit.py                # 审计数据访问组合入口
│   ├── claims.py               # 确认与执行占用
│   ├── common.py               # 状态更新、占用与文件快照辅助
│   ├── execution.py            # 审计执行记录及终态
│   ├── preparation.py          # 审计轮次、明细与待确认准备
│   ├── recovery.py             # 过期确认与执行恢复
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
│   ├── managed.py              # 托管知识数据访问
│   └── organization.py         # 知识整理快照、阶段与片段持久化
├── memory/
│   ├── store.py                # 长期记忆数据访问组合入口
│   ├── store_common.py         # 记录条件、排序与事务辅助
│   ├── store_history.py        # 修订、嵌入增量与引用访问
│   ├── store_record.py         # 记忆记录访问组合类型
│   ├── store_record_lifecycle.py # 记录墓碑、恢复与清理
│   ├── store_record_mutation.py # 记录创建与状态变更
│   ├── store_record_query.py   # 记录查询与容量候选选择
│   ├── store_state.py          # 用户记忆仓库与嵌入选择令牌
│   ├── job.py                  # 长期记忆作业数据访问组合入口
│   ├── job_claim.py            # 作业认领与租约续期
│   ├── job_common.py           # 作业条件、结果契约与参数校验
│   ├── job_control.py          # 作业取消与释放
│   ├── job_query.py            # 作业查询
│   ├── job_terminal.py         # 作业成功、失败与恢复终态
│   └── maintenance.py          # 长期记忆维护数据访问
├── message_platform/
│   ├── platform.py             # 消息平台数据访问
│   └── outbox.py               # 消息发件箱数据访问
├── profile/
│   ├── profile.py              # Profile 数据访问
│   └── prompt.py               # Prompt 数据访问
├── session/
│   ├── message.py              # 主会话与独立工作消息读写及历史隔离
│   ├── session.py              # 会话设置、上下文状态与累计用量访问
│   ├── event.py                # 会话事件数据访问
│   ├── external_work.py        # 外部异步操作关联数据访问
│   ├── todo.py                 # 会话 Todo 计划数据访问
│   ├── reply_provider_usage.py # 模型请求用量去重与用途记录
│   ├── reply_stream_event.py   # 回复流事件数据访问
│   ├── reply_work_item.py      # 工作认领、独立消息归属与恢复状态访问
│   └── terminal_watch.py       # 终端观察通知数据访问
├── system/
│   ├── log.py                  # 系统日志数据访问
│   └── setting.py              # 系统设置数据访问
├── task/
│   ├── background.py           # 后台任务数据访问
│   ├── interrupted.py          # 异常中断任务数据访问
│   └── scheduled.py            # 定时任务数据访问
├── terminal/
│   └── session.py              # 终端会话数据访问
└── worker/
    └── lease.py                # Worker 协调数据访问
```

### 对话分发：`app/core/dispatchers/`

```text
app/core/dispatchers/
├── __init__.py             # 分发包标识
├── context.py              # 工具分发上下文
├── dispatcher.py           # 对话分发入口
├── background.py           # 后台对话分发
├── interactive.py          # 交互式对话分发兼容导出入口
├── interactive_generation.py # 单轮模型生成与流输出
├── interactive_helpers.py  # 交互式分发辅助
├── interactive_runtime.py  # 交互式代理循环协调
├── interactive_state.py    # 分发运行状态构建
├── interactive_tools.py    # 工具轮次执行与审计状态持久化
├── memory/                 # 长期记忆召回组件
│   ├── __init__.py         # 记忆召回公开导出
│   ├── persistence.py      # 记忆召回持久化组件
│   ├── recall.py           # 对话记忆召回集成
│   ├── request.py          # 前置记忆召回的独立辅助请求与用量
│   └── types.py            # 记忆召回数据结构与隔离的辅助运行态
├── non_stream.py           # 非流式对话分发
├── shared.py               # 分发器共用组件
└── stream.py               # 流式对话分发
```

### 知识库能力

```text
app/core/embedding/
├── __init__.py             # 嵌入能力导出
├── common.py               # 嵌入共用组件
├── knowledge_base.py       # 知识库嵌入服务
└── knowledge_base_runtime.py # 知识库当前生效嵌入与集合快照解析

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
├── __init__.py             # 工具注册、可用范围与审计豁免判定
├── base.py                 # 工具抽象
├── background_tasks.py      # 后台任务查询与取消工具
├── end_session.py          # 会话结束与回复控制工具
├── file_tool/              # 通用文本文件操作工具
│   ├── __init__.py         # 工具能力导出
│   ├── executor.py         # 单一 file_tool 执行入口
│   ├── schema.py           # read/write/edit/patch/grep 工具协议
│   ├── paths.py            # 工作区与允许目录路径边界
│   ├── text.py             # UTF-8、BOM、换行与行号公共能力
│   ├── read.py             # 有界分页读取
│   ├── write.py            # 完整文件写入
│   ├── edit.py             # 字面文本精确编辑
│   ├── patch.py            # 多 hunk 补丁解析与原子应用
│   └── grep.py             # 文件或目录正则搜索
├── image_generation.py     # 图像生成工具
├── knowledge_base_query.py # 知识库查询与托管知识可信元数据
├── longterm_memory.py      # 统一记忆工具：个人记忆与托管知识维护
├── read_text_file.py       # 审计取证用文本读取
├── read_multimodal_file.py # 多模态文件读取工具
├── scheduled_tasks.py      # 间隔与 Cron 定时任务管理工具
├── send_file_to_user.py    # 文件投递与下载凭证管理
├── shell.py                # 一次性命令执行与交互终端创建
├── skill.py                # 全局共享技能包只读目录访问工具
├── terminal.py             # 交互终端工具（terminal_tool，集成 read/write/resize/close 操作）
├── terminal_watch.py       # 终端输出观察工具
├── todo.py                 # 当前会话 Todo 计划工具
├── timeout.py              # 工具执行与占用等待超时策略
└── web_tool.py             # 统一网页搜索与抓取工具

app/core/utils/
├── dispatcher/             # 对话分发辅助
│   ├── __init__.py         # 分发辅助包标识
│   ├── append_new_user_messages.py # 追加当前轮新增用户消息
│   ├── channel_call.py     # 模型渠道调用与回退
│   ├── context_summary_checkpoint.py # 上下文总结检查点应用
│   ├── external_work.py    # 外部工作挂起状态构建
│   ├── fetch_and_merge_new_user_messages.py # 拉取并合并新增用户消息
│   ├── frozen_context.py    # 工作上下文快照捕获、恢复与独立压缩
│   ├── handle_parallel_tool_limit.py # 并行工具调用上限处理
│   ├── helpers.py          # 分发共用辅助
│   ├── inject_system_prompt.py # 系统提示构建与注入
│   ├── mark_initial_message_processed.py # 初始消息处理状态更新
│   ├── markdown_instruction.py # 运行时文本与输出指令组装
│   ├── prepare_messages.py # 模型请求消息准备
│   ├── process_markdown_response.py # Markdown 响应处理
│   ├── process_single_tool.py # 单工具校验与执行
│   ├── save_assistant_message.py # 助手消息持久化
│   ├── save_initial_message.py # 初始消息持久化
│   ├── save_message.py     # 通用消息持久化
│   ├── save_tool_response.py # 工具结果持久化
│   ├── session_todo_snapshot.py # Todo 快照持久化与模型上下文恢复
│   ├── terminal_watch_snapshot.py # 终端观察状态快照构建
│   ├── tool_call_correction.py # 虚拟工具反馈消息构建
│   ├── truncate_tool_result.py # 工具结果预算截断
│   ├── user_input_batch.py # 用户输入批次结构
│   └── validate_profile_and_cfg.py # Profile 配置校验
├── context_summary/        # 上下文总结辅助
│   ├── __init__.py         # 上下文总结能力导出
│   ├── boundary.py         # 总结触发边界解析
│   ├── cleanup.py          # 总结阶段与过期数据清理
│   ├── common.py           # 总结共用状态、统计与选择辅助
│   ├── history.py          # 持久化历史与摘要片段读取统计
│   ├── merge.py            # 已完成摘要片段分组与合并
│   ├── model_call.py       # 上下文总结模型调用
│   ├── pipeline.py         # 有界分片总结管线
│   ├── reduction.py        # 分层归约与摘要精炼
│   ├── selection.py        # 上下文总结模型选择
│   ├── service.py          # 总结生命周期与持久化协调
│   ├── snapshot.py         # 总结快照构建与历史轮次读取
│   ├── split.py            # 超长消息与轮次分片
│   ├── stage.py            # 总结阶段生成与状态管理
│   └── user_message_block.py # 被总结覆盖的用户消息块处理
├── assistant_files.py      # 助手文件组装、生成图像落盘与模型正文隔离
├── auxiliary_request.py    # 辅助请求输出完整性预算校验与业务异常
├── background_task_result.py # 后台任务结果辅助
├── channel_profile_sync.py # 渠道与 Profile 同步辅助
├── config.py               # 配置读取辅助
├── context_budget.py       # 上下文预算辅助
├── context_messages.py     # 上下文消息辅助
├── database_integrity.py   # 数据库完整性错误识别辅助
├── file_access.py          # 用户临时文件安全访问辅助
├── http_proxy.py           # HTTP 代理辅助
├── message_assembler.py    # 消息组装辅助
├── message_parser.py       # 消息解析辅助
├── model_request_headers.py # 模型请求头辅助
├── llm_request_params.py   # 内部任务模型请求参数策略
├── llm_request_policy.py   # 模型请求大小限制与超限拦截策略
├── operation_directories.py # 操作目录白名单与共享技能目录保护
├── request_token_baseline.py # 请求用量识别、累计用量与缓存统计
├── scheduled_time.py       # 间隔、Cron 与时区校验及执行时间计算
├── session.py              # 会话辅助
├── system.py               # 系统信息辅助
├── text_splitter.py        # 文本切分辅助
├── time.py                 # 时间辅助
└── tokenizer.py            # 按模型标识选择编码的令牌统计辅助
```

标题生成与前置记忆召回复用辅助请求配置边界，相关运行态与主对话分离。

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
├── knowledge_base/
│   ├── __init__.py         # 知识库模型及接口数据结构的稳定公开导入入口
│   ├── base.py             # 知识库主体、嵌入运行状态与 Profile 绑定
│   ├── collection_owner.py # 向量集合所有权模型与数据库触发器
│   ├── document.py         # 用户知识库文档与分块元数据
│   ├── managed.py          # 托管知识主记录与修订历史
│   ├── migration.py        # 嵌入迁移增量记录
│   ├── job.py              # 知识异步作业模型
│   ├── organization.py     # 知识整理快照、阶段与片段
│   └── schemas.py          # 知识库与托管知识请求响应结构
├── memory/
│   ├── __init__.py         # 长期记忆模型稳定公开导入入口
│   ├── store.py            # 用户记忆仓库、容量和嵌入运行状态
│   ├── embedding.py        # 嵌入选择凭证、版本及迁移增量
│   ├── record.py           # 记忆主记录与修订历史
│   └── job.py              # 长期记忆变更作业模型
├── message.py              # 消息持久化、工作归属与内部消息结构
├── message_platform.py     # 消息平台模型
├── message_platform_outbox.py # 消息发件箱模型
├── agent_work.py           # 独立 AgentWork 执行契约与结果目标
├── profile.py              # Profile 模型及独立的对话、总结与辅助渠道配置
├── prompt.py               # Prompt 模型
├── scheduled_task.py       # 会话绑定、间隔/Cron 规则与执行状态
├── session.py              # 会话设置、上下文摘要与累计用量状态
├── session_event.py        # 会话事件模型
├── session_reply_stream_event.py # 回复流事件模型
├── session_reply_provider_usage.py # 模型请求用途与用量去重模型
├── session_reply_work_item.py # 回复工作、执行快照与队列序号模型
├── session_todo.py             # 会话级 Todo 计划持久化模型
├── system_log.py           # 系统日志模型
├── system_setting.py       # 系统设置模型
├── terminal_session.py     # 终端会话模型
├── user.py                 # 用户模型
└── worker_lease.py         # Worker 协调模型
```

`app/models/` 定义关系型持久化对象，由 `app/core/crud/` 提供访问，由 `app/providers/database/` 提供数据库连接和初始化能力。
知识库接口数据结构位于 `app/models/knowledge_base/schemas.py`，仍由 `app.models.knowledge_base` 对外导出。

## 外部能力封装：`app/providers/`

```text
app/providers/
├── __init__.py             # Provider 包标识
├── database/
│   ├── __init__.py         # 数据库能力导出
│   ├── bootstrap.py        # 数据库建表、初始数据与迁移调度
│   ├── client.py           # 异步数据库会话与取消时的事务收尾
│   └── time.py             # 数据库时间能力
├── embedding/
│   ├── __init__.py         # 嵌入能力导出
│   └── client.py           # 嵌入模型客户端
├── image_generation/
│   ├── __init__.py         # 图像生成能力导出
│   └── client.py           # 图像生成客户端
├── llm/
│   ├── __init__.py         # 大模型能力导出
│   ├── client.py           # 大模型客户端
│   └── token_estimation.py # Provider 请求载荷的本地 Token 估算
├── rerank/
│   ├── __init__.py         # 重排能力导出
│   └── client.py           # 重排模型客户端
├── vector/
│   ├── __init__.py         # 向量存储能力导出
│   └── chroma.py           # Chroma 向量存储适配
└── web.py                  # 网页服务选择与 Firecrawl 适配
```

Provider 层隔离数据库、向量存储、语言模型、嵌入、重排、图像生成和网页服务等外部依赖。

### 模型输入 Token 计量

模型输入 Token 明确区分“Provider 确认值”和“本地估算值”，两者不得互相冒充：

- Provider 成功响应中的输入 Token 用量是主会话上下文占用、前端展示和主动总结阈值的权威来源。主动总结只复用同一 `channel_id + model_id + protocol` 且上下文版本未变化的最近一次 Provider 确认值；旧 metadata 没有渠道身份时不得复用，避免不同 NewAPI/OpenAI-compatible 渠道的同名 alias 串用。
- 主对话模型请求不会因为本地 Token 估算超过配置窗口而被拒绝或触发总结。若 Provider 明确返回上下文超限，则在当前渠道强制总结后重试；仅 `context_window - max_output - safety_margin <= 0` 这类与 Tokenizer 无关的静态配置错误会在本地直接拒绝。
- 本地 Token 估算只用于发送前必须确定大小的内部链路，例如工具结果截断、审计载荷拟合、总结分片和压缩结果校验，不作为整个会话是否超限的事实来源。
- `core/utils/dispatcher/frozen_context.py`：冻结工作上下文独立压缩，不改写主会话摘要。
- `transformers` 负责把内部消息转换为实际 Provider 请求形态；`providers/llm/token_estimation.py` 对该 Provider 载荷做本地估算。审计完整请求拟合、工具结果预算和总结后的最终请求校验都复用这一口径；回传的推理文本或原生推理项按实际请求形态计入，未发送的内部元数据不计入，避免重复。
- `core/utils/tokenizer.py` 只根据 `model_id` 选择 tiktoken 已知编码；协议只表示请求格式，不能用于推断真实模型厂商。对于 `gpt-5.6-*` 这类 tiktoken 前缀表已能表达、但点号版本名无法直接命中的名称，仅做名称归一化后再次交给 tiktoken 决策；无法确认的普通 alias 使用通用回退。
- 不通过 OpenAI-compatible 协议猜测并调用厂商专有的预请求 Token 计数接口，也不维护“本地估算 / Provider usage”的历史校准系数；聚合网关后的真实 Provider 由对端响应负责给出最终 usage 和上下文超限结果。

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
├── deepseek/                # DeepSeek 协议转换
│   ├── chat_completions.py  # DeepSeek 对话协议与推理内容转换
│   └── responses.py         # DeepSeek Responses 协议与推理条目转换
├── http_transport.py        # 模型列表、JSON 请求与事件流 HTTP 传输
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
├── public/                 # HTML 入口模板
│   └── index.html           # 前端页面入口模板
├── src/                    # Vue 源码
├── tests/                  # 前端自动化测试
├── vue.config.js           # Vue 开发代理与浏览器错误提示配置
├── package-lock.json       # 前端依赖锁定
└── package.json            # 前端依赖与测试、发布构建脚本
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

### 前端基础模块

```text
dashboard/src/api/
└── index.js                # 后端鉴权请求与 OpenRouter 模型目录访问

dashboard/src/assets/
├── css/                    # 页面、组件与主题 SCSS
└── svg/                    # 界面图标

dashboard/src/constants/
├── channelModel.js         # 上下文容量与输出预算常量
└── index.js                # 界面常量与默认配置

dashboard/src/i18n/
├── index.js                # 语言注册与切换
└── locales/                # 中英文界面文案

dashboard/src/router/
├── index.js                # 页面路由与权限检查装配
├── setupGuard.js           # 初始化、登录与服务可用性导航检查
└── userManagementGuard.js  # 管理员页面访问检查
```

### 前端共享组件

```text
dashboard/src/components/
├── BaseDataTable.vue            # 通用数据表格与分页
├── ChannelEditor.vue            # 渠道配置与模型条目编辑
├── ChannelFormDialog.vue        # 渠道新增与编辑弹窗
├── ChannelModelEntry.vue        # 模型能力、协议与请求参数编辑
├── ChatMessageList.vue          # 消息、工具结果与请求用量展示
├── CollapsibleMessageText.vue    # 长消息折叠展示
├── ConfirmDialogContent.vue      # 确认弹窗正文
├── HelpTooltip.vue               # 帮助提示
├── KnowledgeBaseRepairDialog.vue # 用户知识库修复确认弹窗
├── KnowledgeEntryEditor.vue     # 托管知识条目编辑
├── KnowledgeOrganizationDialog.vue # 知识整理作业提交、进度与历史
├── LanguageSwitcher.vue         # 界面语言切换
├── MemoryEmbeddingDialog.vue    # 记忆嵌入配置与迁移确认
├── MessagePlatformFormDialog.vue # 消息平台配置弹窗
├── ModelTestResultDialog.vue    # 模型调用测试结果展示
├── ProfileFormDialog.vue        # Profile 与记忆参数配置
├── SessionTaskCenter.vue        # 全局会话任务通知与跳转
├── SessionTodoPanel.vue         # 会话 Todo 计划展示
├── SetupProfileGuide.vue         # 初始化后的 Profile 配置引导
├── StatusTag.vue                # 业务状态标签
├── ThinkingBlock.vue            # 模型推理内容展示
├── UnderConstruction.vue        # 建设中占位内容
├── VirtualizedCode.vue           # 长代码分段展开与复制
└── weixin_oc/                   # 微信接入界面组件
    └── WeixinOcLoginDialog.vue  # 微信扫码登录弹窗
```

### 前端组合逻辑

```text
dashboard/src/composables/
├── useConfirmDialog.js           # 通用确认弹窗调用
├── useDeleteConfirm.js           # 删除确认与反馈
├── useToolParser.js              # 工具消息解析与折叠状态
├── useUserPermissions.js         # 用户身份与管理员权限状态
├── useWebSocket.js               # WebSocket 连接生命周期封装
├── webSocketConnection.js        # 连接、重连、心跳与消息订阅
├── chat/                         # 聊天状态、消息同步与任务协调
│   ├── auditConfirmationState.js # 审计确认与工具结果状态合并
│   ├── chatMessageHelpers.js     # 消息分类与身份匹配辅助
│   ├── chatTransportRuntime.js   # HTTP 与 WebSocket 传输执行
│   ├── contextSummaryTracker.js  # 上下文总结状态与事件顺序跟踪
│   ├── historyIncrementalSync.js # 历史消息增量同步
│   ├── historyMergeTracker.js    # 历史合并版本跟踪
│   ├── historyPagination.js      # 历史分页参数换算
│   ├── httpHistorySync.js        # HTTP 历史同步调度
│   ├── httpReplyPolling.js       # HTTP 回复工作状态轮询
│   ├── llmRequestMetadata.js     # 主对话请求状态与累计用量合并
│   ├── reasoningTracker.js       # 流式推理文本追加与完成处理
│   ├── replyControl.js           # 回复停止与在途提交协调
│   ├── sessionActivity.js        # 会话活跃时间与顺序维护
│   ├── sessionAgentSettings.js   # 目标模式与轮次设置
│   ├── sessionListLoading.js     # 会话列表工作状态轮询
│   ├── sessionTasks.js           # 会话任务列表与状态协调
│   ├── sessionTodoOverlay.js     # Todo 抽屉展开状态与偏好保存
│   ├── sessionTransportMode.js   # 会话传输方式切换与恢复
│   ├── streamEventIdentity.js    # 流事件身份提取
│   ├── streamResume.js           # 断线续传与轮次历史恢复
│   ├── terminalHistory.js        # 工具轮次与异常事件历史合并
│   ├── thinkingTracker.js        # 回复处理中占位消息管理
│   ├── transportNotifications.js # 传输状态提示
│   ├── useChatAuditConfirmation.js # 审计确认交互
│   ├── useChatComposer.js        # 消息输入、附件上传与外部会话指引
│   ├── useChatDrafts.js          # 按用户和会话保存草稿
│   ├── useChatHistory.js         # 历史加载、分页与增量合并
│   ├── useChatHttpSend.js        # HTTP 消息发送
│   ├── useChatMessageFiles.js    # 消息文件下载与预览
│   ├── useChatMessagePresentation.js # 消息渲染、工具分组与用量展示
│   ├── useChatSession.js         # 聊天会话与回复流程装配
│   ├── useChatSessionEvents.js   # 会话通知、总结与工作事件处理
│   ├── useChatState.js           # 聊天响应式状态
│   ├── useChatStreamResume.js    # 当前会话流恢复
│   ├── useChatTransport.js       # 聊天传输方式封装
│   ├── useChatViewLayout.js      # 聊天布局与会话分组
│   ├── useChatViewNavigation.js  # 会话切换与页面导航
│   ├── useChatViewSettings.js    # Profile、显示与执行设置
│   ├── useChatWebSocketSend.js   # WebSocket 消息发送
│   ├── useMessageProcessor.js    # 回复、推理与工具消息合并
│   ├── useSessionManager.js      # 会话列表、历史读取与删除
│   ├── useSessionTasks.js        # 全局任务通知与会话跳转
│   └── workLifecycleTracker.js   # 输入排队与工作运行状态跟踪
├── knowledgeBase/                # 知识库管理与作业交互
│   ├── useKnowledgeBaseEntries.js # 知识文档、托管条目与作业轮询
│   ├── useKnowledgeBaseQueryTest.js # 知识库召回测试
│   ├── useKnowledgeBaseView.js   # 知识库列表、嵌入迁移与修复
│   ├── useKnowledgeEntryEditor.js # 托管知识条目编辑提交
│   └── useKnowledgeOrganization.js # 知识整理提交、进度与历史
├── memories/                     # 长期记忆管理交互
│   ├── useMemoriesView.js        # 记忆页面与用户范围协调
│   ├── useMemoryRecords.js       # 记忆条目查询与编辑
│   └── useMemoryRuntime.js       # 记忆运行状态、整理与索引维护
├── profiles/                     # Profile 配置交互
│   ├── useProfileEditor.js       # Profile 表单编辑与知识库绑定
│   ├── useProfileMemory.js       # 记忆嵌入选择、迁移确认与整理配置
│   └── useProfilesView.js        # Profile 列表与编辑协调
└── setup/                        # 初始化渠道交互
    └── useSetupChannelConfiguration.js # 渠道发现、参数校验与调用测试
```

### 前端辅助模块

```text
dashboard/src/utils/
├── assistantResponseIdentity.js # 助手回复身份匹配与历史合并
├── auditConfirmation.js         # 审计确认可操作性判断
├── channelAdvancedSettings.js   # 模型高级设置与请求头编辑
├── channelHttpProxy.js          # 渠道代理地址校验
├── channelModelMetadata.js      # OpenRouter 模型信息匹配与参数补全
├── channelTestManager.js        # 渠道调用测试任务协调
├── chatContentReveal.js         # 聊天欢迎过渡与内容显示判定
├── chatPresentation.js          # 消息滚动、活动提示与会话分组
├── clientSettings.js            # 浏览器本地偏好持久化
├── errorMessage.js              # 错误提示长度控制
├── fileDownloads.js             # 鉴权下载地址与预览缓存
├── index.js                     # 消息解析、合并与时间格式化
├── knowledgeBaseManagement.js   # 知识库类型、操作权限与反馈
├── knowledgeBaseMigration.js    # 嵌入迁移状态与进度展示
├── knowledgeBaseScope.js       # 用户与知识库切换范围隔离
├── memoryManagement.js          # 记忆容量、整理设置与作业展示
├── messagePlatformForm.js       # 消息平台表单校验与请求组装
├── profileOptions.js             # Profile 归属筛选与配置权限过滤
├── requestTaskManager.js        # 可取消请求与过期响应隔离
├── runtimeEndpoints.js           # API 与 WebSocket 运行地址解析
├── scheduledTaskSchedule.js      # 日历规则、Cron 转换与时区表单校验
├── setupForm.js                  # 初始化表单校验与请求组装
├── todoPresentation.js           # Todo 计划状态合并与摘要
├── toolOutputVisibility.js       # 工具调用与结果显示过滤
└── toolResultState.js             # 工具结果状态合并与展示转换
```

### 前端页面

```text
dashboard/src/views/
├── BackendUnavailableView.vue # 后端服务不可用时展示的独立错误页面
├── ChannelsView.vue        # 渠道管理页面
├── ChatView.vue            # 聊天页面
├── HistoryLogs.vue         # 历史日志页面
├── KnowledgeBase.vue       # 知识库管理页面
├── KnowledgeEntriesView.vue # 知识文档与托管知识条目管理页面
├── LoginView.vue           # 登录页面
├── MemoriesView.vue        # 长期记忆条目、作业与运行状态页面
├── MessagePlatformsView.vue # 消息平台管理页面
├── ProfilesView.vue        # Profile、记忆参数与自动整理配置页面
├── PromptsView.vue         # Prompt 管理页面
├── RealTimeLogs.vue        # 实时日志页面
├── ScheduledTasksView.vue  # 间隔、日历规则与 Cron 定时任务管理及预览
├── SetupView.vue           # 系统初始化页面
├── SkillsView.vue          # 管理员共享技能列表与删除页面
├── UnderConstructionView.vue # 通用建设中占位页面
└── UsersView.vue           # 用户管理页面
```

## 测试与脚本

```text
tests/                      # 后端单元测试与集成测试
dashboard/tests/            # 前端自动化测试
scripts/                    # 数据库迁移和运行维护脚本
```

测试目录和脚本目录只作为工程支持层，不在架构文档中展开文件清单。

已有部署的数据库一次性迁移由 `app/providers/database/bootstrap.py` 按 `migration_*.py` 文件名顺序逐个执行并在成功后登记。历史迁移不得依赖未来版本 ORM 的表结构；表重建类迁移应基于执行时数据库自描述结构或版本冻结的迁移 schema 工作，保证用户可以从较老版本直接跨多个版本升级而不被后续模型字段反向阻塞。

## 持久化目录：`data/` 与 `temp/`

```text
data/                       # SQLite、Chroma、日志与审计等持久化数据
├── audit/                  # 审计文件
├── chromadb/               # Chroma 向量数据
├── file_download_tokens/   # 发送文件下载凭证
├── logs/                   # 文件日志
├── monolight.db            # 默认 SQLite 数据库
├── skills/                 # 全局共享技能包
├── system_secrets.json     # 系统密钥持久化文件
└── system_secrets.lock     # 系统密钥初始化锁文件

temp/                       # 上传文件、工具结果和其他临时数据
└── temp_<uid>/              # 按用户隔离的临时文件
    └── generated_images/    # 模型生成的图片文件
```

关系型数据由 `app/providers/database/` 管理，向量数据由 `app/providers/vector/` 管理，文件型持久化由核心服务和路径组件管理。
共享技能资产访问与 Shell 执行职责分离。

## 依赖方向

```text
dashboard → api → adapters / core
dashboard → OpenRouter model catalog
workers → core
core/message_platforms → adapters
adapters → core
core → crud / models / providers / transformers
crud → models / database provider
providers / transformers → external services
```

- `dashboard` 通过公开接口访问后端，另可直连 OpenRouter 模型目录。
- `api` 调用适配器与核心服务；`adapters` 适配对话平台并调用核心服务。
- `core/message_platforms` 负责选择与调用平台适配器。
- `core` 组织应用服务和领域能力，依赖数据访问、持久化模型、Provider 及协议转换组件。
- `crud` 面向 `models` 和数据库 Provider，负责持久化访问抽象。
- `providers` 隔离外部服务和持久化基础设施。
- `workers` 使用核心服务和持久化抽象，不依赖前端模块。
- 模型与基础设施复用 `core` 的常量、路径与基础辅助。

## 进程边界

- Web 进程同时承载 FastAPI 接口、WebSocket 接口、对话适配入口和预构建 Dashboard 静态资源。
- 默认长期后台进程为 `general`、`memory` 和 `terminal` 三个；`general` 合并承载消息平台、通用后台任务和会话回复三个角色，三个角色仍分别使用原数据库租约，因此多实例互斥语义不变。
- `memory` 与 `terminal` 继续作为独立长期进程运行，以保持资源和故障隔离。
- Web 进程与 Worker 进程通过核心服务及持久化资源共享应用数据，不直接形成前端依赖。
- 数据库、向量存储、日志、审计文件和临时文件构成运行期资源边界。
- 非交互 Shell、图像生成和终端观察由后台任务执行，结果通过回复队列交付。
