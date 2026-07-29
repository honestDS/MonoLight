export default {
  title: 'Message Platforms',
  create: 'Create Platform',
  create_and_login: 'Create and Scan',
  edit: 'Edit Platform',
  name: 'Name',
  platform_type: 'Platform Type',
  status: 'Status',
  account_id: 'Account ID',
  uid: 'Bound User',
  profile: 'Profile',
  language: 'Language',
  use_stream_dispatch: 'Stream Responses',
  inherited_profile: 'Use user default profile',
  default_profile_suffix: ' (Default)',
  enabled: 'Enabled',
  api_timeout_ms: 'API Timeout(ms)',
  long_poll_timeout_ms: 'Long Poll Timeout(ms)',
  poll_interval_ms: 'Long Poll Interval(ms)',
  merge_single_poll_messages: 'Merge messages in one poll',
  last_error: 'Last Error',
  actions: 'Actions',
  login: 'QR Login',
  recover: 'Recover',
  qrcode_title: 'Weixin OpenClaw QR Login',
  qrcode_tip: 'Scan and confirm with Weixin. The token will be saved to the platform config after login succeeds.',
  fill_required: 'Please fill required fields',
  uid_required: 'Please bind a user before enabling the platform',
  create_success: 'Created successfully',
  update_success: 'Updated successfully',
  submit_failed: 'Submit failed',
  load_failed: 'Load failed',
  load_profiles_failed: 'Failed to load profiles',
  login_started: 'QR code generated',
  recover_success: 'Recovered successfully',
  login_confirmed: 'Login succeeded',
  login_waiting: 'Waiting for confirmation',
  login_expired: 'QR code expired',
  status_map: {
    DISCONNECTED: 'Disconnected',
    WAITING_LOGIN: 'Waiting Login',
    CONNECTED: 'Connected',
    ERROR: 'Error'
  },
  type_map: {
    WEIXIN_OPENCLAW: 'Weixin OpenClaw'
  },
  language_map: {
    zh: '中文',
    en: 'English'
  },
  error_map: {
    ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED: 'QR code expired',
    ERR_MESSAGE_PLATFORM_QRCODE_RESPONSE_INVALID: 'QR code response is missing required fields',
    ERR_MESSAGE_PLATFORM_UID_REQUIRED: 'A bound user is required before enabling a message platform',
    ERR_MESSAGE_PLATFORM_RECOVER_TOKEN_MISSING: 'Platform token is missing and cannot be recovered. Please scan the QR code again to bind the platform'
  }
}
