/**
 * 公共常量
 * 集中管理应用中使用的常量
 */

// 分页相关
export const PAGE_SIZE = 20

// 路由名称映射
export const routeNameMap = {
  '/': 'common.menu.chat',
  '/users': 'common.menu.users',
  '/knowledge-base': 'common.menu.knowledge_base',
  '/profiles': 'common.menu.system',
  '/providers': 'common.menu.system',
  '/prompts': 'common.menu.system',
  '/logs/realtime': 'common.menu.logs',
  '/logs/history': 'common.menu.logs'
}

// 默认渠道配置
const defaultChannelConfig = () => ({
  chat_timeout: 60.0,
  embedding_timeout: 30.0,
  rerank_timeout: 15.0,
  rerank_candidate_k: 20,
  kb_query_top_k: 5,
  rules: []
})

// 默认配置结构（用于 ProfilesView.vue）—— 渠道管理架构
export const defaultProfileConfigs = () => ({
  provider: {
    chat_channel: defaultChannelConfig(),
    embedding_channel: defaultChannelConfig(),
    rerank_channel: defaultChannelConfig(),
  },
  security: { audit_provider_id: null, audit_model_id: null, audit_threshold: 5 },
  tool: { shell_timeout: 30, max_parallel_tools: 5, executor_max_workers: 10, max_turns: 5, firecrawl_api_key: '' },
  other: {},
})

// 默认渠道规则
export const defaultChannelRule = () => ({
  provider_id: null,
  model_id: '',
  priority: 1,
  weight: 1,
})

// 默认模型条目（Provider 下）
export const defaultModelEntry = () => ({
  model_id: '',
  usage: 'CHAT',
  image_understanding: false,
  audio_understanding: false,
  video_understanding: false,
  context_window_k: 4,
  temperature: 0.7,
  top_p: 1,
  max_tokens: 2048,
  embedding_dimensions: null,
  description: '',
})

// 默认提供商表单
export const defaultProviderForm = () => ({
  name: '',
  provider_type: 'OPENAI',
  api_key: '',
  base_url: '',
  is_active: true,
  model_ids: []
})

// 默认用户表单
export const defaultUserForm = () => ({
  uid: null,
  username: '',
  password: '',
  is_active: true
})