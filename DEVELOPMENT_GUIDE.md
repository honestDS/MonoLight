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
2. 单行长度限制为 **120** 字符。
3. 必须在文件头部保留清晰的导入层级：标准库 -> 第三方库 -> 项目内模块。
4. 提交代码前，必须执行：
   ```bash
   ruff check . --fix
   ruff format .
   ```

## 四、 核心模型与协议
1. **标准消息对象**：所有模块间的消息传递必须使用 `app.schemas.message` 中的 `InternalMessage`。
2. **配置管理**：Profile 配置更新必须通过 `ProfileConfig` 模型进行校验，确保 JSON 数据结构的正确性。
3. **协议扩展**：新增模型供应商支持时，必须继承 `BaseTransformer` 并实现双向转换逻辑。

## 五、 数据交互规范
1. 严禁在 API 或 Dispatcher 层直接编写 SQL/select 语句，所有数据交互必须通过 CRUD层 实例进行。
2. CRUD 层应保持纯粹，仅负责数据的存取与基础过滤，不应包含 LLM 调用或复杂的跨领域业务逻辑。
3. 所有针对特定 LLM 厂商的请求参数拼接与响应解析逻辑，必须封装在对应的 Transformer 实现类中，严禁在 `LLMClient` 或 `Dispatcher` 中出现特定厂商（如 OpenAI、Anthropic）的字段名称。
4. 在涉及大模型流式输出或耗时工具调用的循环中，Agent 必须显式执行 `await db.execute(select(1))` 以保持数据库 Session 的活跃状态，防止连接因闲置被杀掉。

## 六、 新增功能与测试要求
- **目录结构**：单元测试存放在 `tests/unit`，集成测试存放在 `tests/integration`。
- **测试框架**：统一使用 `pytest` 与 `pytest-asyncio`。
- **编写指南**：
  - 每个新增的 API 路由必须在 `tests/integration` 中拥有对应的异步请求测试。
  - 使用 `conftest.py` 中定义的 `db_session` fixture 进行数据库隔离测试。
  - 测试中的 Mock 逻辑必须基于 `InternalResponse` 等标准对象，严禁使用旧版字典结构。

## 七、 异常处理规范
1. 业务异常必须继承自 `app.core.exceptions` 中的 `BaseBusinessException`。
2. 所有 LLM 交互异常必须封装为 `LLMException` 并附带明确的错误上下文。
3. 所有面向用户的提示文本**禁止**在代码中拼装字符串。Agent 必须先读取 `app/core/constants.py`，查找对应的 `ERR_*` 或 `MSG_*` 常量。如果不存在，则必须先在常量库中新增，严禁临时硬编码。

## 八、 依赖管理
1. 所有新依赖必须同步至 `requirements.txt`。
2. 优先使用异步库，如 `aiohttp` 代替 `requests`。
