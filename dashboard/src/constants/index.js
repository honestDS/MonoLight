/**
 * 公共常量
 * 集中管理应用中使用的常量
 */

// 分页相关
export const PAGE_SIZE = 20

// 路由名称映射
export const routeNameMap = {
  '/': '智能交互',
  '/users': '用户管理',
  '/knowledge-base': '知识库管理',
  '/profiles': '系统配置',
  '/providers': '系统配置',
  '/prompts': '系统配置',
  '/logs/realtime': '系统日志',
  '/logs/history': '系统日志'
}

// 默认配置结构（用于 ProfilesView.vue）
export const defaultProfileConfigs = () => ({
  provider: {
    provider_id: null,
    model_id: '',
    embedding_provider_id: null,
    embedding_model_id: '',
    embedding_dimensions: null,
    rerank_provider_id: null,

    rerank_model_id: '',
    rerank_candidate_k: 20,
    rerank_timeout: 15.0,
    kb_query_top_k: 5,
    chat_timeout: 60.0,
    embedding_timeout: 30.0,
    temperature: 0.7,
    top_p: 1.0,
    max_tokens: 2048,
    multimodal: false,
    context_window_k: 4,
  },
  security: { audit_provider_id: null, audit_model_id: null, audit_threshold: 5 },
  tool: { shell_timeout: 30, max_parallel_tools: 5, max_turns: 5, firecrawl_api_key: '' },
  other: {},
})

// 默认提供商表单
export const defaultProviderForm = () => ({
  name: '',
  provider_type: 'OPENAI',
  model_usage: 'CHAT',
  api_key: '',
  base_url: '',
  is_active: true
})

// 默认用户表单
export const defaultUserForm = () => ({
  uid: null,
  username: '',
  password: '',
  is_active: true
})