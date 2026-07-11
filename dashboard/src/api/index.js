import axios from 'axios'
import i18n from '../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8001/api/v1'
const API_BASE_URL = process.env.VUE_APP_API_BASE_URL || DEFAULT_API_BASE_URL
const API_ORIGIN = new URL(API_BASE_URL, window.location.origin).origin

const request = axios.create({
  baseURL: API_BASE_URL,
  timeout: 120000
})

const WS_BASE_URL = process.env.VUE_APP_WS_BASE_URL || API_ORIGIN.replace(/^http/, 'ws')

const getCurrentLocale = () => localStorage.getItem('locale') || 'zh'

const appendDownloadLangParam = (url) => {
  if (/[?&]lang=/.test(url)) return url
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}lang=${encodeURIComponent(getCurrentLocale())}`
}

request.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) { config.headers['Authorization'] = `Bearer ${token}` }
  config.headers['Accept-Language'] = getCurrentLocale()
  return config
})


request.interceptors.response.use(
  res => {
    // 自动解包 StandardResponse 结构
    const { code, data, message } = res.data;
    if (code !== undefined && code !== 200) {
      // 业务报错，直接抛出，让 catch 块处理
      const error = new Error(message || t('common.unknown_error'));
      error.response = { data: { message: message || t('common.unknown_error'), data } };
      return Promise.reject(error);
    }
    return res;
  },
  err => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('token');
      if (window.location.hash !== '#/login') window.location.hash = '/login';
    }
    // 统一错误提示格式：优先读取后端返回的 message 或 detail
    const errorMsg = err.response?.data?.message || err.response?.data?.detail || err.message || t('common.network_request_failed');
    err.message = errorMsg; 
    return Promise.reject(err);
  }
);

export const adminApi = {
  userList: (params) => request.get('/admin/user/list', { params }),
  userAdd: (data) => request.post('/admin/user/add', data),
  userUpdate: (data) => request.post('/admin/user/update', data),
  userDelete: (uid) => request.post(`/admin/user/delete?uid=${uid}`)
}

export const authApi = {
  login: (data) => request.post('/auth/login', data),
  resetAdmin: async (token) => {
    const res = await request.post('/auth/reset_admin', { reset_token: token })
    return res.data?.data || res.data || {}
  }
}

export const chatApi = {
    // 聊天 WS 接口
    createWebSocket(token) {
        const lang = getCurrentLocale()
        const wsUrl = `${WS_BASE_URL}/api/v1/chat/ws?token=${token}&lang=${lang}`
        console.log('WebSocket connecting to:', wsUrl)
        return new WebSocket(wsUrl)
    },

  // 聊天 completions 接口
  completions: (data) => request.post('/chat/completions', data),
  // 获取会话列表
  sessionsList: () => request.get('/chat/sessions/list'),
  // 删除会话
  deleteSession: (sessionId) => request.post(`/chat/sessions/delete?session_id=${sessionId}`),
  // 获取会话历史记录
  sessionsHistory: (sessionId, page = 1, size = 20) => request.get(`/chat/sessions/history?session_id=${sessionId}&page=${page}&size=${size}`),
  // 异步生成会话标题
  generateTitle: (data) => request.post('/chat/sessions/generate-title', data),
  // 更新会话设置
  updateSessionSetting: (sessionId, enableMarkdown) => request.post('/chat/sessions/setting', { session_id: sessionId, enable_markdown: enableMarkdown }),
  // 后台任务列表
  backgroundTasks: (params) => request.get('/chat/background-tasks', { params }),
}

export const profileApi = {
  list: (params) => request.get('/profiles/list', { params }),
  create: (data) => request.post('/profiles/create', data),
  activate: (id) => request.post(`/profiles/activate?profile_id=${id}`),
  update: (id, data) => request.post(`/profiles/update?profile_id=${id}`, data),
  delete: (id) => request.post(`/profiles/delete?profile_id=${id}`),
  types: () => request.get('/profiles/types'),
}

export const promptApi = {
  list: (params) => request.get('/prompts/list', { params }),
  create: (data) => request.post('/prompts/create', data),
  update: (id, data) => request.post(`/prompts/update?prompt_id=${id}`, data),
  delete: (id, params = {}) => request.post('/prompts/delete', null, { params: { prompt_id: id, ...params } })
}

export const channelApi = {
  list: (params) => request.get('/channels/list', { params }),
  types: () => request.get('/channels/types'),
  create: (data) => request.post('/channels/create', data),
  get: (id) => request.get(`/channels/get?channel_id=${id}`),
  update: (id, data) => request.post(`/channels/update?channel_id=${id}`, data),
  delete: (id) => request.post(`/channels/delete?channel_id=${id}`),
  models: (data) => request.post('/channels/models', data),
  testChat: (data) => request.post('/channels/test-chat', data),
  testImageGeneration: (data) => request.post('/channels/test-image-generation', data),
  testEmbeddingDimension: (channelId, modelId) => request.post(`/channels/test-embedding-dimension?channel_id=${channelId}&model_id=${encodeURIComponent(modelId)}`)
}


export const messagePlatformApi = {
  list: (params) => request.get('/message-platforms/list', { params }),
  types: () => request.get('/message-platforms/types'),
  create: (data) => request.post('/message-platforms/create', data),
  get: (id) => request.get(`/message-platforms/get?platform_id=${id}`),
  update: (id, data) => request.post(`/message-platforms/update?platform_id=${id}`, data),
  delete: (id) => request.post(`/message-platforms/delete?platform_id=${id}`),
  recover: (id) => request.post(`/message-platforms/recover?platform_id=${id}`),
  startWeixinLogin: (id) => request.post(`/message-platforms/${id}/weixin-openclaw/login/start`),
  getWeixinLoginStatus: (id) => request.get(`/message-platforms/${id}/weixin-openclaw/login/status`)
}

export const systemApi = {
  settings: () => request.get('/system/settings'),
  updateSettings: (data) => request.post('/system/settings', data),
  // 获取后端可用语言列表
  i18nLocales: () => request.get('/system/i18n/locales'),
  // 获取历史系统日志
  logsHistory: (params) => request.get('/system/logs', { params }),
  // 系统日志实时 WS 接口
  createLogsWebSocket: (token) => {
    const lang = getCurrentLocale()
    const wsUrl = `${WS_BASE_URL}/api/v1/system/logs/ws?token=${token}&lang=${lang}`
    console.log('System Logs WebSocket connecting to:', wsUrl)
    return new WebSocket(wsUrl)
  }
}

export const scheduledTaskApi = {
  list: (params) => request.get('/scheduled-tasks/list', { params }),
  create: (data) => request.post('/scheduled-tasks/create', data),
  update: (id, data) => request.post(`/scheduled-tasks/update?task_id=${id}`, data),
  delete: (id) => request.post(`/scheduled-tasks/delete?task_id=${id}`)
}

export const fileApi = {
  upload: (file, session_id) => {
    const formData = new FormData()
    formData.append('file', file)
    if (session_id) {
      formData.append('session_id', session_id)
    }
    return request.post('/upload', formData, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
  },
  getDownloadUrl: (path) => {
    return `${API_BASE_URL}/download?path=${encodeURIComponent(path)}&lang=${encodeURIComponent(getCurrentLocale())}`
  },
  resolveDownloadUrl: (url) => {
    if (!url) return '#'
    if (/^https?:\/\//i.test(url)) return appendDownloadLangParam(url)
    return appendDownloadLangParam(`${API_ORIGIN}${url.startsWith('/') ? url : `/${url}`}`)
  }
}

export const knowledgeBaseApi = {
  list: (params) => request.get('/knowledge-base/list', { params }),
  create: (data) => request.post('/knowledge-base/create', data),
  update: (id, data) => request.post(`/knowledge-base/update?kb_id=${id}`, data),
  delete: (id) => request.post(`/knowledge-base/delete?kb_id=${id}`),
  importDocument: (id, formData) => request.post(`/knowledge-base/documents/import?kb_id=${id}`, formData, {
    headers: { 'Content-Type': 'multipart/form-data' }
  }),
  documents: (id, params) => request.get('/knowledge-base/documents/list', { params: { ...params, kb_id: id } }),
  document: (id, documentId) => request.get('/knowledge-base/documents/get', { params: { kb_id: id, document_id: documentId } }),
  deleteDocument: (id, documentId) => request.post(`/knowledge-base/documents/delete?kb_id=${id}&document_id=${documentId}`),
  queryTest: (id, data) => request.post(`/knowledge-base/query-test?kb_id=${id}`, data)
}

export default request
