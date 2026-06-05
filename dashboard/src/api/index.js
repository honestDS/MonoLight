import axios from 'axios'

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
  return config
})


request.interceptors.response.use(
  res => {
    // 自动解包 StandardResponse 结构
    const { code, data, message } = res.data;
    if (code !== undefined && code !== 200) {
      // 业务报错，直接抛出，让 catch 块处理
      return Promise.reject({ response: { data: { message: message || '未知错误' } } });
    }
    return res;
  },
  err => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('token');
      if (window.location.hash !== '#/login') window.location.hash = '/login';
    }
    // 统一错误提示格式：优先读取后端返回的 message 或 detail
    const errorMsg = err.response?.data?.message || err.response?.data?.detail || err.message || '网络请求失败';
    err.message = errorMsg; 
    return Promise.reject(err);
  }
);

export const adminApi = {
  userList: () => request.get('/admin/user/list'),
  userAdd: (data) => request.post('/admin/user/add', data),
  userUpdate: (data) => request.post('/admin/user/update', data),
  userDelete: (uid) => request.post(`/admin/user/delete?uid=${uid}`)
}

export const authApi = {
  login: (data) => request.post('/auth/login', data),
  resetAdmin: (token) => request.post('/auth/reset_admin', { reset_token: token })
}

export const chatApi = {
    // 聊天 WS 接口
    createWebSocket(token) {
        const wsUrl = `${WS_PROTOCOL}://${WS_BASE_URL}/api/v1/chat/ws?token=${token}`
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
}

export const profileApi = {
  list: () => request.get('/profiles/list'),
  create: (data) => request.post('/profiles/create', data),
  activate: (id) => request.post(`/profiles/activate?profile_id=${id}`),
  update: (id, data) => request.post(`/profiles/update?profile_id=${id}`, data),
  delete: (id) => request.post(`/profiles/delete?profile_id=${id}`),
  types: () => request.get('/profiles/types'),
}

export const promptApi = {
  list: () => request.get('/prompts/list'),
  create: (data) => request.post('/prompts/create', data),
  update: (id, data) => request.post(`/prompts/update?prompt_id=${id}`, data),
  delete: (id) => request.post(`/prompts/delete?prompt_id=${id}`)
}

export const providerApi = {
  list: () => request.get('/providers/list'),
  types: () => request.get('/providers/types'),
  create: (data) => request.post('/providers/create', data),
  get: (id) => request.get(`/providers/get?provider_id=${id}`),
  update: (id, data) => request.post(`/providers/update?provider_id=${id}`, data),
  delete: (id) => request.post(`/providers/delete?provider_id=${id}`)
}

export const systemApi = {
  // 获取历史系统日志
  logsHistory: (params) => request.get('/system/logs', { params }),
  // 系统日志实时 WS 接口
  createLogsWebSocket: (token) => {
    const wsUrl = `${WS_PROTOCOL}://${WS_BASE_URL}/api/v1/system/logs/ws?token=${token}`
    console.log('System Logs WebSocket connecting to:', wsUrl)
    return new WebSocket(wsUrl)
  }
}

export default request
