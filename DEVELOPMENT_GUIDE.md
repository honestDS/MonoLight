# Monolight 开发规范 (v1.0)

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

## 四、 新增功能与测试要求
- **目录结构**：单元测试存放在 `tests/unit`，集成测试存放在 `tests/integration`。
- **测试框架**：统一使用 `pytest` 与 `pytest-asyncio`。
- **编写指南**：
  - 每个新增的 API 路由必须在 `tests/integration` 中拥有对应的异步请求测试。
  - 使用 `conftest.py` 中定义的 `db_session` fixture 进行数据库隔离测试，严禁直接操作生产库。
  - 断言必须清晰：除了断言状态码，还必须断言响应体（`StandardResponse`）的结构一致性。

## 五、 异常处理规范
1. 严禁使用空的 `try-except`。
2. 业务异常必须继承自 `app.core.exceptions` 中的 `BaseMonolightException`。
3. API 报错应统一抛出 FastAPI 的 `HTTPException` 或使用标准响应格式。

## 六、 依赖管理
1. 所有新依赖必须同步至 `requirements.txt`。
2. 优先使用异步库，如 `httpx` 代替 `requests`。
