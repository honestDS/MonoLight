export default {
  title: 'Message Platforms',
  create: 'Create Platform',
  create_and_login: 'Create and Scan',
  create_login_tip: 'A Weixin login QR code will be generated after creation. Token and Base URL do not need to be entered manually.',
  edit: 'Edit Platform',
  name: 'Name',
  platform_type: 'Platform Type',
  status: 'Status',
  account_id: 'Account ID',
  uid: 'Bound User',
  enabled: 'Enabled',
  api_timeout_ms: 'API Timeout(ms)',
  long_poll_timeout_ms: 'Long Poll Timeout(ms)',
  poll_interval_ms: 'Long Poll Interval(ms)',
  last_error: 'Last Error',
  actions: 'Actions',
  login: 'QR Login',
  refresh_status: 'Refresh Status',
  qrcode_title: 'Weixin OpenClaw QR Login',
  qrcode_tip: 'Scan and confirm with Weixin. The token will be saved to the platform config after login succeeds.',
  qrcode_content: 'QR Content',
  fill_required: 'Please fill required fields',
  create_success: 'Created successfully',
  update_success: 'Updated successfully',
  submit_failed: 'Submit failed',
  load_failed: 'Load failed',
  login_started: 'QR code generated',
  login_confirmed: 'Login succeeded',
  login_waiting: 'Waiting for confirmation',
  login_expired: 'QR code expired',
  copied: 'Copied',
  status_map: {
    DISCONNECTED: 'Disconnected',
    WAITING_LOGIN: 'Waiting Login',
    CONNECTED: 'Connected',
    ERROR: 'Error'
  },
  type_map: {
    WEIXIN_OPENCLAW: 'Weixin OpenClaw'
  },
  error_map: {
    ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED: 'QR code expired',
    ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID: 'QR code response is missing required fields',
    ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED: 'OpenClaw token is required'
  }
}
