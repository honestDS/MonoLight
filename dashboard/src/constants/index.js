// 公共常量
// 分页相关
export const PAGE_SIZE = 20

// 路由名称映射
export const routeNameMap = {
  '/': 'common.menu.chat',
  '/users': 'common.menu.users',
  '/memories': 'common.menu.memories',
  '/knowledge-base': 'common.menu.knowledge_base',
  '/profiles': 'common.menu.system',
  '/channels': 'common.menu.system',
  '/message-platforms': 'common.menu.system',
  '/prompts': 'common.menu.system',
  '/scheduled-tasks': 'common.menu.scheduled_tasks',
  '/logs/realtime': 'common.menu.logs',
  '/logs/history': 'common.menu.logs',
  '/docs': 'common.menu.docs',
  '/support': 'common.menu.support'
}

export const MEMORY_TYPES = ['fact', 'preference', 'project', 'todo', 'constraint']
export const MEMORY_JOB_STATUSES = ['pending', 'running', 'retry', 'succeeded', 'failed', 'cancelled']
export const MEMORY_JOB_OPERATIONS = ['create', 'update', 'restore', 'reindex', 'delete_cleanup', 'embedding_migration', 'create_with_eviction', 'organize', 'organize_merge']
export const MEMORY_MIGRATION_STATUSES = ['preparing', 'building', 'catching_up', 'validating', 'switching', 'succeeded', 'failed', 'cancelled']

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
    context_summary_channel: defaultChannelConfig(),
    rerank_channel: defaultChannelConfig(),
    image_generation_channel: defaultChannelConfig(),
  },
  security: { audit_channel_id: null, audit_model_id: null, audit_threshold: 5, audit_confirmation_timeout_seconds: 600, audit_report_language: 'zh' },
  tool: {
    tool_timeout: 30,
    image_generation_timeout: 60,
    max_parallel_tools: 5,
    executor_max_workers: 10,
    max_turns: 5,
    background_task_max_concurrency: 2,
    scheduled_task_max_concurrency: 4,
    firecrawl_api_key: '',
    enabled_tools: ['execute_shell', 'write_file', 'firecrawl_search', 'firecrawl_scrape', 'send_file_to_user', 'list_background_tasks', 'cancel_background_task', 'generate_image', 'query_knowledge_base', 'read_multimodal_file'],
    allowed_operation_dirs: [],
    file_send_max_count: 10,
    file_send_max_single_size_mb: 50,
    file_send_max_total_size_mb: 100,
    file_send_blocked_extensions: []
  },
  other: {
    context_summary_threshold_percent: 90
  },
  memory: {
    enabled: false,
    embedding_channel_id: null,
    embedding_model_id: null,
    top_k: 5,
    candidate_k: 10,
    result_max_chars: 4000
  }
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
  protocol: 'OPENAI',
  image_understanding: false,
  audio_understanding: false,
  video_understanding: false,
  context_window_k: 4,
  temperature: 0.7,
  top_p: 1,
  max_tokens: 2048,
  embedding_dimensions: null,
  embedding_timeout: 30,
  rerank_timeout: 15,
  is_enabled: true,
  size: '1024x1024',
  quality: 'auto',
  advanced_settings: {},
  description: '',
})

export const normalizeModelEntry = (entry) => {
  const defaults = defaultModelEntry()

  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
    return defaults
  }

  return {
    ...defaults,
    ...entry,
    advanced_settings: entry.advanced_settings && typeof entry.advanced_settings === 'object' && !Array.isArray(entry.advanced_settings)
      ? { ...entry.advanced_settings }
      : {}
  }
}

// 默认渠道表单
export const defaultChannelForm = () => ({
  name: '',
  api_key: '',
  base_url: '',
  http_proxy: '',
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
