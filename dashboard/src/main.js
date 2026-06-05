import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/dist/locale/zh-cn.mjs'
import 'element-plus/dist/index.css'
import '@/assets/css/common.scss'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

const app = createApp(App)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia()).use(router).use(ElementPlus, {
  locale: zhCn,
}).use(i18n).mount('#app')
