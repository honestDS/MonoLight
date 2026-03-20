# MonoLight API 真实文档 (v1)

### 全局规范
1. **基础 URL**: `/api/v1`
2. **响应格式**: 所有接口统一返回 `StandardResponse` 结构：
   - `code`: 状态码（200 为成功，其他为异常）
   - `message`: 提示信息
   - `data`: 业务数据（Object/Array/null）
3. **认证方式**: 
   - 除 `/auth/login` 和 `/auth/reset_admin` 外，所有接口均需在 Header 中携带：
     `Authorization: Bearer <your_access_token>`

---

### 1 认证模块 (Auth)
**基础路径**: `/api/v1/auth`

#### 1.1 登录
- **接口**: `POST /login`
- **参数**: `{ "username": "admin", "password": "..." }`
- **预期响应**: `{ "code": 200, "message": "Login successful", "data": { "access_token": "...", "token_type": "bearer" } }`
- **错误响应**: `{ "code": 401, "message": "Invalid username or password", "data": null }`

#### 1.2 重置管理员
- **接口**: `POST /reset_admin`
- **参数**: `{ "reset_token": "..." }`
- **预期响应**: `{ "code": 200, "message": "Admin account reset", "data": { "new_username": "admin", "new_password": "..." } }`
- **错误响应**: `{ "code": 403, "message": "Invalid reset token", "data": null }`

---

### 2 模型供应商 (Providers)
**基础路径**: `/api/v1/providers`

#### 2.1 创建供应商
- **接口**: `POST /create`
- **参数**: `{ "name": "OpenAI", "base_url": "...", "api_key": "..." }`
- **预期响应**: `{ "code": 200, "message": "Provider created", "data": { "id": 1 } }`

#### 2.2 获取供应商列表
- **接口**: `GET /list`
- **预期响应**: `{ "code": 200, "message": "success", "data": [ { "id": 1, "name": "..." } ] }`

#### 2.3 获取单个供应商详情
- **接口**: `GET /get`
- **参数**: `?id=1`
- **预期响应**: `{ "code": 200, "message": "success", "data": { "id": 1, "config": "..." } }`

#### 2.4 更新供应商
- **接口**: `POST /update`
- **参数**: `{ "id": 1, "api_key": "new_key" }`
- **预期响应**: `{ "code": 200, "message": "Updated successfully" }`

#### 2.5 删除供应商
- **接口**: `POST /delete`
- **参数**: `{ "id": 1 }`
- **预期响应**: `{ "code": 200, "message": "Deleted successfully" }`

---

### 3 对话模块 (Chat)
**基础路径**: `/api/v1/chat`

#### 3.1 会话补全 (Chat Completion)
- **接口**: `POST /completions`
- **参数**: `{ "session_id": "...", "message": "你好", "profile_id": 1 }`
- **预期响应**: `{ "code": 200, "message": "success", "data": { "reply": "...", "tokens": 15 } }`

#### 3.2 获取历史会话列表
- **接口**: `GET /sessions/list`
- **预期响应**: `{ "code": 200, "data": [ { "session_id": "...", "title": "..." } ] }`

#### 3.3 删除会话
- **接口**: `POST /sessions/delete`
- **参数**: `{ "session_id": "..." }`
- **预期响应**: `{ "code": 200, "message": "Session deleted" }`

---

### 4 配置预设 (Profile)
**基础路径**: `/api/v1/profile`

#### 4.1 创建预设
- **接口**: `POST /create`
- **参数**: `{ "name": "翻译专家", "prompt_id": 1, "params": { "temperature": 0.7 } }`
- **预期响应**: `{ "code": 200, "data": { "profile_id": 1 } }`

#### 4.2 激活预设
- **接口**: `POST /activate`
- **参数**: `{ "id": 1 }`
- **预期响应**: `{ "code": 200, "message": "Profile activated" }`

---

### 5 提示词库 (Prompts)
**基础路径**: `/api/v1/prompts`

#### 5.1 获取列表
- **接口**: `GET /list`
- **预期响应**: `{ "code": 200, "data": [ { "id": 1, "content": "..." } ] }`

#### 5.2 创建提示词
- **接口**: `POST /create`
- **参数**: `{ "title": "助手", "content": "You are a helpful assistant" }`
- **预期响应**: `{ "code": 200, "data": { "id": 1 } }`

---

### 6 用户管理 (Admin)
**基础路径**: `/api/v1/admin/user`

#### 6.1 获取用户列表
- **接口**: `GET /list`
- **预期响应**: `{ "code": 200, "data": [ { "uid": "...", "username": "..." } ] }`
- **错误响应**: `{ "code": 403, "message": "Permission denied" }`

#### 6.2 删除用户
- **接口**: `POST /delete`
- **参数**: `{ "uid": "..." }`
- **预期响应**: `{ "code": 200, "message": "User deleted" }`
