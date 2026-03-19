Monobot API 真实文档 (v1) - 经路由挂载逻辑审计

### 全局规范
1. 响应格式: StandardResponse { code, message, data }
2. 认证方式: 除 /auth 外，均需 Header [Authorization: Bearer <token>]

---

### 1 认证模块 (Auth)
挂载点: /api/v1/auth

1.1 POST /api/v1/auth/login

1.2 POST /api/v1/auth/reset_admin

---

### 2 模型供应商 (Providers)
挂载点: /api/v1 (内部无前缀)

2.1 POST /api/v1/providers/create

2.2 GET /api/v1/providers/list

2.3 GET /api/v1/providers/get

2.4 POST /api/v1/providers/update

2.5 POST /api/v1/providers/delete

---

### 3 对话模块 (Chat)
挂载点: /api/v1 (内部前缀 /chat)

3.1 POST /api/v1/chat/completions

3.2 GET /api/v1/chat/sessions/list

3.3 POST /api/v1/chat/sessions/delete

---

### 4 配置预设 (Profile)
挂载点: /api/v1 (内部前缀 /profile)

4.1 POST /api/v1/profile/create

4.2 GET /api/v1/profile/list

4.3 POST /api/v1/profile/activate

4.4 POST /api/v1/profile/update

4.5 POST /api/v1/profile/delete

---

### 5 提示词库 (Prompts)
挂载点: /api/v1 (内部前缀 /prompts)

5.1 GET /api/v1/prompts/list

5.2 POST /api/v1/prompts/create

5.3 POST /api/v1/prompts/update

5.4 POST /api/v1/prompts/delete

---

### 6 用户管理 (Users)
挂载点: /api/v1/admin (内部前缀 /user)

6.1 POST /api/v1/admin/user/add

6.2 GET /api/v1/admin/user/list

6.3 POST /api/v1/admin/user/update

6.4 POST /api/v1/admin/user/delete
