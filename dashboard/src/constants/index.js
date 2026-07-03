// 公共常量
// 分页相关
export const PAGE_SIZE = 20

// 路由名称映射
export const routeNameMap = {
  '/': 'common.menu.chat',
  '/users': 'common.menu.users',
  '/knowledge-base': 'common.menu.knowledge_base',
  '/profiles': 'common.menu.system',
  '/channels': 'common.menu.system',
  '/message-platforms': 'common.menu.system',
  '/prompts': 'common.menu.system',
  '/scheduled-tasks': 'common.menu.scheduled_tasks',
  '/logs/realtime': 'common.menu.logs',
  '/logs/history': 'common.menu.logs'
}

// 默认渠道配置
const defaultChannelConfig = () => ({
  chat_timeout: 60.0,
  rerank_timeout: 15.0,
  rerank_candidate_k: 20,
  kb_query_top_k: 5,
  rules: []
})

// 默认配置结构（用于 ProfilesView.vue）—— 渠道管理架构
export const defaultProfileConfigs = () => ({
  channel: {
    chat_channel: defaultChannelConfig(),
    rerank_channel: defaultChannelConfig(),
    image_generation_channel: defaultChannelConfig(),
  },
  security: { audit_channel_id: null, audit_model_id: null, audit_threshold: 5 },
  tool: {
    tool_timeout: 30,
    image_generation_timeout: 60,
    max_parallel_tools: 5,
    executor_max_workers: 10,
    max_turns: 5,
    firecrawl_api_key: '',
    enabled_tools: ['execute_shell', 'write_file', 'firecrawl_search', 'firecrawl_scrape', 'send_file_to_user', 'list_background_tasks', 'cancel_background_task', 'generate_image', 'query_knowledge_base'],
    allowed_file_send_dirs: [],
    file_send_max_count: 10,
    file_send_max_single_size_mb: 50,
    file_send_max_total_size_mb: 100,
    file_send_blocked_extensions: []
  },
  other: {},
})

// 默认渠道规则
export const defaultChannelRule = () => ({
  channel_id: null,
  model_id: '',
  priority: 1,
  weight: 1,
})

// 默认模型条目（渠道下）
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

// 默认渠道表单
export const defaultChannelForm = () => ({
  name: '',
  channel_type: 'OPENAI',
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
