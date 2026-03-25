import axios from 'axios'

const request = axios.create({
  baseURL: 'http://154.36.178.178:8001/api/v1',
  timeout: 10000
})

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


export const authApi = {
  login: (data) => request.post('/auth/login', data),
  resetAdmin: (token) => request.post('/auth/reset_admin', { reset_token: token })
}

export const chatApi = {
  send: (msg) => request.post('/chat/send', { message: msg })
}

export const profileApi = {
  list: () => request.get('/profiles/list'),
  create: (data) => request.post('/profiles/create', data),
  activate: (id) => request.post(`/profiles/activate?profile_id=${id}`),
  update: (id, data) => request.post(`/profiles/update?profile_id=${id}`, data),
  delete: (id) => request.post(`/profiles/delete?profile_id=${id}`)
}

export const providerApi = {
  list: () => request.get('/providers/list')
}

export default request
