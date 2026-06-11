import { createI18n } from 'vue-i18n'

/**
 * 自动加载 locales 目录下按语言/页面拆分的语言包
 * 目录结构约定：locales/{语言}/{命名空间}.js
 * 例如 locales/zh/chat.js 会被组装为 messages.zh.chat
 * 后期新增页面只需在对应语言目录下新增同名文件，无需修改本文件
 */
const loadLocaleMessages = () => {
  const context = require.context('./locales', true, /[A-Za-z0-9-_]+\.js$/)
  const messages = {}

  context.keys().forEach((key) => {
    // key 形如 './zh/chat.js'
    const matched = key.match(/^\.\/([A-Za-z0-9-_]+)\/([A-Za-z0-9-_]+)\.js$/)
    if (!matched) return

    const locale = matched[1]
    const namespace = matched[2]

    if (!messages[locale]) {
      messages[locale] = {}
    }
    messages[locale][namespace] = context(key).default
  })

  return messages
}

// 支持的语言列表（供语言切换组件使用）
export const SUPPORT_LOCALES = [
  { value: 'zh', label: '简体中文' },
  { value: 'en', label: 'English' }
]

// 从本地存储读取上次选择的语言，默认中文
const getDefaultLocale = () => {
  const saved = localStorage.getItem('locale')
  if (saved && SUPPORT_LOCALES.some((item) => item.value === saved)) {
    return saved
  }
  return 'zh'
}

const i18n = createI18n({
  legacy: false,
  locale: getDefaultLocale(),
  fallbackLocale: 'en',
  messages: loadLocaleMessages()
})

/**
 * 切换语言并持久化
 * @param {string} locale - 目标语言
 */
export const setLocale = (locale) => {
  i18n.global.locale.value = locale
  localStorage.setItem('locale', locale)
}

export default i18n

