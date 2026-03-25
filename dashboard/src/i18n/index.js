import { createI18n } from 'vue-i18n'

const messages = {
  zh: {
    common: {
      confirm: '确定',
      cancel: '取消',
      warning: '系统提示',
      delete_confirm: '确定要永久删除该配置吗？'
    },
    login: {
      welcome: '欢迎',
      subtitle: '请开启您的数字进化之旅',
      username: '用户名',
      password: '密码',
      submit: '登录',
      signup: '注册',
      reset_admin: '重置管理员',
      reset_token_placeholder: '请输入重置 Token',
      reset_hint: '请输入系统 ADMIN_RESET_TOKEN 进行校验',
      reset_confirm: '立即重置',
      cancel: '取消',
      success: '验证成功，欢迎归位'
    }
  },
  en: {
    common: {
      confirm: 'Confirm',
      cancel: 'Cancel',
      warning: 'System Warning',
      delete_confirm: 'Are you sure you want to permanently delete this profile?'
    },
    login: {
      welcome: 'Welcome',
      subtitle: 'Start your digital evolution',
      username: 'Username',
      password: 'Password',
      submit: 'Login',
      signup: 'Sign up',
      reset_admin: 'Reset Admin',
      reset_token_placeholder: 'Enter Reset Token',
      reset_hint: 'Enter ADMIN_RESET_TOKEN for validation',
      reset_confirm: 'Reset Now',
      cancel: 'Cancel',
      success: 'Login successful'
    }
  }
}

const i18n = createI18n({
  legacy: false,
  locale: 'zh',
  fallbackLocale: 'en',
  messages
})

export default i18n
