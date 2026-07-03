export default {
  title: '消息平台',
  create: '新增平台',
  create_and_login: '创建并扫码绑定',
  create_login_tip: '创建后将自动生成微信登录二维码，无需手动填写 Token 或 Base URL。',
  edit: '编辑平台',
  name: '名称',
  platform_type: '平台类型',
  status: '状态',
  account_id: '账号ID',
  uid: '绑定用户',
  enabled: '启用',
  api_timeout_ms: 'API超时(ms)',
  long_poll_timeout_ms: '长轮询超时(ms)',
  poll_interval_ms: '长轮询间隔(ms)',
  last_error: '最后错误',
  actions: '操作',
  login: '扫码登录',
  refresh_status: '刷新状态',
  qrcode_title: '微信 OpenClaw 扫码登录',
  qrcode_tip: '请使用微信扫码并确认登录。登录成功后 token 将自动保存到平台配置。',
  qrcode_content: '二维码内容',
  fill_required: '请填写必填项',
  create_success: '创建成功',
  update_success: '更新成功',
  submit_failed: '提交失败',
  load_failed: '加载失败',
  login_started: '二维码已生成',
  login_confirmed: '登录成功',
  login_waiting: '等待扫码确认',
  login_expired: '二维码已过期',
  copied: '已复制',
  status_map: {
    DISCONNECTED: '未连接',
    WAITING_LOGIN: '等待登录',
    CONNECTED: '已连接',
    ERROR: '异常'
  },
  type_map: {
    WEIXIN_OPENCLAW: '微信 OpenClaw'
  },
  error_map: {
    ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED: '二维码已过期',
    ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID: '二维码响应缺少必要字段',
    ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED: 'OpenClaw token 不能为空'
  }
}
