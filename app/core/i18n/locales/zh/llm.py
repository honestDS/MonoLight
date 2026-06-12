MESSAGES = {
    "ERR_LLM_CONNECTION_FAILED": "连接大模型供应商网关失败，请检查网络或代理配置",
    "ERR_LLM_API_RESPONSE_ERROR": "大模型 API 返回异常响应",
    "ERR_LLM_UNEXPECTED_ERROR": "大模型接口调用发生非预期异常",
"ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL": "大模型接口调用发生非预期异常: {detail}",
"ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS": "大模型 API 返回异常响应 [状态: {status}]: {detail}",
    "ERR_LLM_PROVIDER_NOT_CONFIGURED": "未检测到有效的模型供应商配置或 API Key。请在管理后台检查并激活一个包含有效密钥的厂商 Profile。",
    "ERR_LLM_EMPTY_RESPONSE": "大模型返回了空的响应内容，请尝试重新发送指令或检查模型侧配置",
    "ERR_LLM_FIRST_CHAR_TIMEOUT": "等待对话模型首字响应超时（{timeout} 秒）",
    "ERR_RERANK_FORMAT_ERROR": "Rerank 接口返回格式异常：缺少 results 列表",
    "ERR_EMBEDDING_COUNT_MISMATCH": "向量模型返回数量与文本数量不一致",
    "ERR_EMBEDDING_DIMENSION_MISMATCH": "向量模型实际输出维度为 {actual}，与配置的 {expected} 不一致",
}
