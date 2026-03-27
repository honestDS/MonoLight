# 成功类消息 - 通用
MSG_GENERIC_SUCCESS = "操作成功"

# 成功类消息 - 用户与认证
MSG_USER_CREATED = "用户添加成功"
MSG_USER_UPDATED = "用户信息更新成功"
MSG_USER_DELETED = "用户已删除"
MSG_USER_LIST_SUCCESS = "获取用户列表成功"
MSG_LOGIN_SUCCESS = "登录成功"

# 成功类消息 - 配置(Profile)
MSG_PROFILE_CREATED = "配置创建成功"
MSG_PROFILE_UPDATED = "配置修改成功"
MSG_PROFILE_DELETED = "配置已删除"
MSG_PROFILE_ACTIVATED = "配置已激活"

# 成功类消息 - 提供商(Provider)
MSG_PROVIDER_CREATED = "模型提供商创建成功"
MSG_PROVIDER_UPDATED = "模型提供商更新成功"
MSG_PROVIDER_DELETED = "模型提供商已删除"

# 成功类消息 - 提示词(Prompt)
MSG_PROMPT_CREATED = "提示词模板创建成功"
MSG_PROMPT_UPDATED = "提示词模板更新成功"
MSG_PROMPT_DELETED = "提示词模板已删除"

# 错误类消息 - 基础
ERR_GENERIC_ERROR = "操作失败"
ERR_INTERNAL_SERVER_ERROR = "系统内部错误"
ERR_DB_OPERATION_FAILED = "数据库操作失败，请检查配置或重试"
ERR_VALIDATION_FAILED = "参数验证失败"

# 错误类消息 - 认证
ERR_ADMIN_PASSWORD_WRONG = "用户名或密码错误"
ERR_USER_NOT_FOUND_OR_DISABLED = "用户不存在或已被禁用"
ERR_INVALID_CREDENTIALS = "用户名或密码错误"
ERR_ONLY_ADMIN_ALLOWED = "只有系统超级管理员有权执行此操作"
ERR_UNAUTHORIZED = "无效的身份凭证"
ERR_LOGIN_REQUIRED = "请先登录以获取访问权限"

# 错误类消息 - 用户业务
ERR_USER_NAME_EXISTS = "用户名已存在"
ERR_USER_NOT_FOUND = "用户不存在"

# 错误类消息 - 配置业务
ERR_PROFILE_NOT_FOUND = "未找到该配置"
ERR_PROFILE_NAME_EXISTS = "配置名称已存在"
ERR_ACTIVATE_NO_PROVIDER = "无法激活未绑定提供商的配置"
ERR_DELETE_LAST_PROFILE = "不能删除最后一个配置"
ERR_DELETE_ACTIVE_PROFILE = "不能删除当前已激活的配置"

# 错误类消息 - 提供商业务
ERR_PROVIDER_NOT_FOUND = "模型提供商不存在"
ERR_PROVIDER_NAME_EXISTS = "该提供商名称已存在"

# 错误类消息 - 提示词业务
ERR_PROMPT_NOT_FOUND = "提示词模板不存在"
ERR_PROMPT_NAME_EXISTS = "提示词模板名称已存在"


ERR_PROFILE_PROVIDER_MISMATCH = (
    "当前激活的配置未关联有效的模型供应商或供应商已失效。请重新编辑并保存配置。"
)

# LLM 业务错误
ERR_LLM_CONNECTION_FAILED = "连接大模型供应商网关失败，请检查网络或代理配置"
ERR_LLM_API_RESPONSE_ERROR = "大模型 API 返回异常响应"
ERR_LLM_UNEXPECTED_ERROR = "大模型接口调用发生非预期异常"

ERR_LLM_PROVIDER_NOT_CONFIGURED = "未检测到有效的模型供应商配置或 API Key。请在管理后台检查并激活一个包含有效密钥的厂商 Profile。"
ERR_LLM_EMPTY_RESPONSE = '大模型返回了空的响应内容，请尝试重新发送指令或检查模型侧配置'

# 验证错误映射
ERR_MAP = {
    "missing": "缺失必填字段",
    "value_error.missing": "缺失必填字段",
    "string_too_short": "输入内容太短",
    "string_too_long": "输入内容太长",
    "int_parsing": "必须是有效的整数",
    "json_invalid": "无效的 JSON 格式",
    "enum": "不在允许的枚举值范围内",
    "type_error.enum": "不在允许的枚举值范围内",
}
