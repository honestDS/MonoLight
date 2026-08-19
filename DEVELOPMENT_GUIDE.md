# Monolight 开发规范 (v1.1)

## 一、 项目概况
Monolight 是一个基于 **FastAPI** 与 **SQLAlchemy** 的轻量级 AI 转发框架。本项目遵循严格的异步编程范式，所有 IO 操作必须使用 `await` 关键字。

## 二、 命名规范
- **类名**：使用大驼峰命名法（PascalCase），例如 `ChatDispatcher`、`AsyncSessionLocal`。
- **函数与方法**：使用小写加下划线（snake_case），例如 `get_current_user`、`chat_completions`。异步函数必须以 `async def` 定义。
- **变量与参数**：使用小写加下划线（snake_case），例如 `current_user`、`db_session`。
- **常量**：在 `app/core/constants.py` 中定义，统一使用大写加下划线（UPPER_CASE），例如 `DEFAULT_TOKEN_LIMIT`。
- **数据库模型**：表名使用单数小写（snake_case），例如 `profile`、`user`；模型类名为单数大驼峰。

## 三、 代码风格与检查 (RUFF)
本项目强制使用 **Ruff** 作为静态代码检查与格式化工具。
1. 禁止使用未使用的导入。
2. 单行长度限制为 **320** 字符。
3. 必须在文件头部保留清晰的导入层级：标准库 -> 第三方库 -> 项目内模块。
4. 提交代码前，必须执行：
   ```bash
   ruff check . --fix
   ruff format .
   ```
5. ruff时禁止使用 --unsafe-fixes 选项。
6. 禁止在任何时候使用全局变量存储状态,项目应该考虑在多WORKER环境下运行。
7. 禁止在代码或规划中使用任何emoji表情。
8. 开发/修复BUG的过程中禁止使用补丁式代码，尽可能彻底解决问题，如有必要可以重构。

## 四、 核心模型与协议
1. **标准消息对象**：所有模块间的消息传递必须使用 `app.schemas.message` 中的 `InternalMessage`。
2. **配置管理**：Profile 配置更新必须通过 `ProfileConfig` 模型进行校验，确保 JSON 数据结构的正确性。
3. **协议扩展**：新增模型供应商支持时，必须继承 `BaseTransformer` 并实现双向转换逻辑。

## 五、 数据交互规范
1. 严禁在 API 或 Dispatcher 层直接编写 SQL/select 语句，所有数据交互必须通过 CRUD层 实例进行。
2. CRUD 层应保持纯粹，仅负责数据的存取与基础过滤，不应包含 LLM 调用或复杂的跨领域业务逻辑。
3. 所有针对特定 LLM 厂商的请求参数拼接与响应解析逻辑，必须封装在对应的 Transformer 实现类中，严禁在 `LLMClient` 或 `Dispatcher` 中出现特定厂商（如 OpenAI、Anthropic）的字段名称。
4. 数据库关联约束实行“默认不使用、关键关系按风险评估启用”的原则，禁止一律拒绝或无差别使用外键。
   - 同一数据库内、生命周期一致且子记录脱离父记录后没有独立业务意义的强所有权关系，应优先使用外键，并明确删除策略。
   - `ON DELETE RESTRICT` 用于仍被有效业务数据引用时禁止删除的关系；`ON DELETE CASCADE` 仅用于随父记录共同销毁的强所有权数据；`ON DELETE SET NULL` 仅用于允许解绑且字段可空的可选关系。
   - 历史审计、事件快照、来源追溯、多态引用、跨数据库或跨外部存储关系，以及需要保留子记录历史的场景，可以不使用外键。
   - 不使用外键时，必须通过单一事务、条件写入或比较更新、明确的生命周期状态、幂等键、租约栅栏令牌和一致性巡检保证关联完整性；禁止仅依赖“先查询父记录、再写入子记录”的 Python 检查。
   - 新增或调整外键前必须检查现有孤儿数据、明确历史数据修复方式，并通过 `scripts/migration_*.py` 迁移脚本兼容项目支持的数据库方言，不得只修改模型定义。
5. 若修改需要做老旧数据迁移(如涉及数据库结构变更等)，应在 `scripts/` 目录下编写一次性迁移脚本，不允许只依赖 `SQLModel.metadata.create_all` 处理已存在表结构。
   - 迁移脚本文件名必须使用 `migration_*.py` 格式，例如 `scripts/migration_001_add_profile_id_to_scheduled_task.py`。
   - 迁移脚本必须定义全局唯一的 `MIGRATION_ID`，并提供 `async def migrate(session): ...` 入口函数。
   - 应用启动时会自动扫描并按文件名排序执行 `scripts/migration_*.py`，执行成功后写入 `migration_record` 表；同一个 `MIGRATION_ID` 后续启动会自动跳过。
   - 已执行过的迁移脚本禁止修改语义；如需补充或修复迁移逻辑，必须新增下一个迁移脚本。
   - 迁移脚本中应使用 `sqlalchemy.text` 或现有 CRUD/模型完成数据修正，并自行处理 SQLite/Mysql 等数据库方言差异。

## 六、 新增功能与测试要求
- 代码变更后必须按功能分批运行全量测试，确保新增功能与已存在功能无冲突。
- **后端目录结构**：单元测试存放在 `tests/unit`，集成测试存放在 `tests/integration`。
- **后端测试框架**：统一使用 `pytest` 与 `pytest-asyncio`。
- **后端编写指南**：
  - 每个新增的 API 路由必须在 `tests/integration` 中拥有对应的异步请求测试。
  - 使用 `conftest.py` 中定义的 `db_session` fixture 进行数据库隔离测试。
  - 测试中的 Mock 逻辑必须基于 `InternalResponse` 等标准对象，严禁使用旧版字典结构。
- **前端目录结构**：前端单元测试统一存放在 `dashboard/tests`，测试文件使用 `*.test.js` 命名。
- **前端测试框架**：使用 Node.js 内置的 `node:test` 与 `node:assert/strict`，测试代码使用 ES Module 导入方式；未经项目统一调整，不得自行引入其他前端测试框架。
- **前端编写指南**：
  - 优先测试可脱离浏览器运行的纯 JavaScript 逻辑，例如消息状态流转、事件顺序、重复事件处理和清理逻辑。
  - 修改已有受测模块或新增同类状态逻辑时，必须补充正常流程、边界输入及幂等性测试。
  - 测试必须基于目标源码的实际行为编写；编写测试前应完整阅读目标实现，严禁凭假设构造断言或 Mock。
  - 当前测试环境不提供 DOM、Vue 组件挂载或端到端浏览器能力；涉及这些行为时仍需完成实际页面验证，不得用纯函数测试替代交互验证。
- **前端测试命令**：进入 `dashboard` 目录执行 `npm test`，该命令会运行 `tests/*.test.js`。
- **前端开发服务器**：`npm run serve` 通过 `dashboard/vue.config.js` 读取项目根 `.env` 的 `APP_HOST`/`APP_PORT`，代理 `/api` 的 HTTP 和 WebSocket；修改端口后必须重启开发服务器。
- **前端发布构建**：
  - Vue 源码位于 `dashboard/`，执行 `npm run build` 时固定输出到 `app/static/dashboard/`。
  - 前端源码变更影响发布页面时，开发者必须重新构建并同步提交构建产物。
  - Node.js 仅作为前端开发和构建依赖；运行 `python start.py` 的部署环境不得依赖 Node.js。

## 七、 异常处理规范
1. 业务异常必须继承自 `app.core.exceptions` 中的 `BaseBusinessException`。
2. 所有 LLM 交互异常必须封装为 `LLMException` 并附带明确的错误上下文。
3. 所有面向用户的提示文本**禁止**在代码中拼装字符串。Agent 必须先读取 `app/core/constants.py`，查找对应的 `ERR_*` 或 `MSG_*` 常量。如果不存在，则必须先在常量库中新增，严禁临时硬编码。

## 八、 依赖管理
1. 所有新依赖必须同步至 `requirements.txt`。
2. 优先使用异步库，如 `aiohttp` 代替 `requests`。
