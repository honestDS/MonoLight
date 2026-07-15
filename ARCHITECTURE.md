# MonoLight 项目架构说明

## 顶层目录

```text
Monoligh/
├── .agents/                # Agent 本地配置
├── .clinerules/            # 项目规则
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

`.git/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.ruff_cache/` 和 `node_modules/` 等版本控制、虚拟环境及缓存目录不在本文展开。

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
    ├── response.py         # 出站响应处理
    └── schemas.py          # 平台内部数据结构
```

## 核心层：`app/core/`

```text
app/core/
├── background_tasks/       # 后台任务与定时任务
├── crud/                   # 数据访问
├── dispatchers/            # 对话分发实现
├── embedding/              # 知识库向量化
├── i18n/                   # 后端多语言
├── message_platforms/      # 消息平台运行管理
├── middleware/             # 工具调用审计
├── rerank/                 # 知识库重排
├── retrieval/              # 知识库检索
├── session_reply_queue/    # 会话最终回复队列
├── tools/                  # 模型可调用工具
├── utils/                  # 通用与调度辅助函数
├── channel_router.py       # 模型渠道选择
├── constants.py            # 常量与消息键
├── context.py              # 对话上下文构建
├── crypto.py               # API Key 加解密与脱敏
├── dispatch_context.py     # 工具执行上下文
├── dispatcher.py           # 对话分发入口
├── exceptions.py           # 业务异常
├── log.py                  # 日志记录
├── log_broadcaster.py      # 实时日志广播
├── paths.py                # 数据与临时目录路径
├── prompts.py              # 内置提示内容
├── security.py             # 认证与安全辅助
├── session_cleanup.py      # 会话关联数据清理
└── session_notifier.py     # 会话事件通知
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
├── executor.py             # 前台及任务总结回复执行
└── manager.py              # 工作入队、消息合并与结果读取
```

### 消息平台：`app/core/message_platforms/`

```text
app/core/message_platforms/
├── __init__.py             # 消息平台包标识
├── base.py                 # 平台处理器基类
├── inbound_collector.py    # 入站消息收集与合并
├── manager.py              # 平台轮询与发件箱投递管理
├── notifier.py             # 会话事件和平台消息入队
└── weixin_openclaw.py      # 微信 OpenClaw 平台处理器
```

### 数据访问：`app/core/crud/`

```text
app/core/crud/
├── background_task.py      # 后台任务数据访问
├── base.py                 # 通用数据访问基类
├── channel.py              # 渠道与模型数据访问
├── channel_cursor.py       # 渠道路由游标访问
├── context_summary_fragment.py # 上下文总结片段访问
├── context_summary_stage.py # 上下文总结阶段访问
├── log.py                  # 系统日志访问
├── message.py              # 消息访问
├── message_platform.py     # 消息平台访问
├── message_platform_outbox.py # 消息平台发件箱访问
├── profile.py              # Profile 访问
├── prompt.py               # Prompt 访问
├── scheduled_task.py       # 定时任务访问
├── session.py              # 会话访问
├── session_event.py        # 会话通知事件访问
├── session_reply_stream_event.py # 回复流事件访问
├── session_reply_work_item.py # 回复工作与顺序访问
├── system_setting.py       # 系统设置访问
├── user.py                 # 用户访问
└── worker_lease.py         # Worker 租约访问
```

### 对话分发：`app/core/dispatchers/`

```text
app/core/dispatchers/
├── __init__.py             # 分发器导出
├── background.py           # 后台对话分发
├── non_stream.py           # 非流式对话分发
├── shared.py               # 分发器共用逻辑
└── stream.py               # 流式对话分发
```

### 调度辅助：`app/core/utils/dispatcher/`

```text
app/core/utils/dispatcher/
├── __init__.py
├── append_new_user_messages.py       # 追加用户消息
├── audit_tool_call.py                # 工具调用审计
├── channel_call.py                   # 模型渠道调用
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
└── validate_profile_and_cfg.py       # Profile 与配置校验
```

### 上下文总结：`app/core/utils/context_summary/`

```text
app/core/utils/context_summary/
├── __init__.py             # 上下文总结能力导出
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
└── stage.py                # 总结阶段执行与状态更新
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
├── message_assembler.py    # 消息组装
├── message_parser.py       # 消息解析
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
├── send_file_to_user.py    # 向用户发送文件
└── shell.py                # Shell 命令执行
```

### 知识库能力

```text
app/core/embedding/
├── __init__.py
└── knowledge_base.py       # 文档向量化与写入

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

### 多语言与审计

```text
app/core/i18n/
├── __init__.py
├── context.py              # 当前语言上下文
├── locale.py               # 语言解析
├── translator.py           # 文本翻译入口
└── locales/                # 多语言消息

app/core/middleware/
└── auditor.py              # 工具调用安全审计
```

## 数据模型层：`app/models/`

```text
app/models/
├── __init__.py             # 模型导出
├── background_task.py      # 后台任务模型
├── channel.py              # 渠道与模型条目模型
├── channel_cursor.py       # 渠道路由游标模型
├── context_summary_stage.py # 上下文总结阶段与片段模型
├── knowledge_base.py       # 知识库、文档与分块模型
├── message.py              # 消息模型
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
    └── chroma.py           # Chroma 向量库访问
```

## 接口结构与协议转换

```text
app/schemas/
├── auth.py                 # 认证接口数据结构
├── background_task.py      # 后台任务数据结构
├── response.py             # 通用响应与分页结构
└── scheduled_task.py       # 定时任务接口数据结构

app/transformers/
├── base.py                 # 协议转换基类
└── openai.py               # OpenAI 风格协议转换
```

## 前端目录：`dashboard/`

```text
dashboard/
├── dist/                   # 前端构建产物
├── public/                 # 静态页面模板
├── src/                    # Vue 源码
├── package-lock.json       # 依赖锁定文件
└── package.json            # 前端依赖与命令
```

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

### 通用组件与组合逻辑

```text
dashboard/src/components/
├── BaseDataTable.vue       # 通用数据表格
├── ChannelEditor.vue       # 渠道编辑器
├── LanguageSwitcher.vue    # 语言切换器
├── MessagePlatformFormDialog.vue # 消息平台表单
├── ProfileFormDialog.vue   # Profile 表单
├── StatusTag.vue           # 状态标签
├── VirtualizedCode.vue     # 大段代码展示
└── weixin_oc/              # 微信 OpenClaw 登录组件

dashboard/src/composables/
├── chat/                   # 聊天状态、传输与会话逻辑
├── useDeleteConfirm.js     # 删除确认
├── useResizeObserver.js    # 尺寸监听
├── useToolParser.js        # 工具调用内容解析
└── useWebSocket.js         # WebSocket 连接管理
```

## 测试目录：`tests/`

```text
tests/
└── unit/
    ├── context_summary_*_support.py  # 上下文总结测试辅助
    ├── session_reply_queue_*_support.py # 回复队列测试辅助
    ├── test_background_*.py          # 后台任务测试
    ├── test_context_*.py             # 上下文与总结测试
    ├── test_message_*.py             # 消息与消息平台测试
    ├── test_session_*.py             # 会话、通知与回复队列测试
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
└── migration_20260715_add_message_system_prompt.py     # 消息系统提醒字段
```

## 运行期目录

```text
data/                       # SQLite、Chroma 与日志数据
temp/                       # 上传文件、工具结果与其他临时文件
```
