import axios from 'axios'
import i18n from '../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

const request = axios.create({
  baseURL: 'http://127.0.0.1:8001/api/v1',
  timeout: 120000  // 聊天接口timeout设为2分钟
})

// WebSocket 独立配置
const WS_BASE_URL = '127.0.0.1:8001'
const WS_PROTOCOL = window.location.protocol === 'https:' ? 'wss' : 'ws'

request.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) { config.headers['Authorization'] = `Bearer ${token}` }
  config.headers['Accept-Language'] = localStorage.getItem('locale') || 'zh'
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
        const lang = localStorage.getItem('locale') || 'zh'
        const wsUrl = `${WS_PROTOCOL}://${WS_BASE_URL}/api/v1/chat/ws?token=${token}&lang=${lang}`
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
  delete: (id) => request.post(`/prompts/delete?prompt_id=${id}`)
}

export const channelApi = {
  list: (params) => request.get('/channels/list', { params }),
  types: () => request.get('/channels/types'),
  create: (data) => request.post('/channels/create', data),
  get: (id) => request.get(`/channels/get?channel_id=${id}`),
  update: (id, data) => request.post(`/channels/update?channel_id=${id}`, data),
  delete: (id) => request.post(`/channels/delete?channel_id=${id}`),
  models: (data) => request.post('/channels/models', data),
  testEmbeddingDimension: (channelId, modelId) => request.post(`/channels/test-embedding-dimension?channel_id=${channelId}&model_id=${encodeURIComponent(modelId)}`)
}

export const systemApi = {
  // 获取历史系统日志
  logsHistory: (params) => request.get('/system/logs', { params }),
  // 系统日志实时 WS 接口
  createLogsWebSocket: (token) => {
    const lang = localStorage.getItem('locale') || 'zh'
    const wsUrl = `${WS_PROTOCOL}://${WS_BASE_URL}/api/v1/system/logs/ws?token=${token}&lang=${lang}`
    console.log('System Logs WebSocket connecting to:', wsUrl)
    return new WebSocket(wsUrl)
  }
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
    return `http://127.0.0.1:8001/api/v1/download?path=${encodeURIComponent(path)}`
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
