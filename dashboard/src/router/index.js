import { createRouter, createWebHashHistory } from 'vue-router'
import { setupApi } from '../api'
import { BACKEND_UNAVAILABLE_PATH, createSetupGuard, setupStatusState } from './setupGuard.js'

const routes = [
  { path: '/login', component: () => import('../views/LoginView.vue') },
  { path: '/setup', component: () => import('../views/SetupView.vue') },
  { path: BACKEND_UNAVAILABLE_PATH, component: () => import('../views/BackendUnavailableView.vue') },
  { path: '/', component: () => import('../views/ChatView.vue') },
  { path: '/profiles', component: () => import('../views/ProfilesView.vue') },
  { path: '/prompts', component: () => import('../views/PromptsView.vue') },
  { path: '/scheduled-tasks', component: () => import('../views/ScheduledTasksView.vue') },
  { path: '/channels', component: () => import('../views/ChannelsView.vue') },
  { path: '/message-platforms', component: () => import('../views/MessagePlatformsView.vue') },
  { path: '/users', component: () => import('../views/UsersView.vue') },
  { path: '/memories', component: () => import('../views/MemoriesView.vue') },
  { path: '/knowledge-base', component: () => import('../views/KnowledgeBase.vue') },
  { path: '/logs/realtime', component: () => import('../views/RealTimeLogs.vue') },
  { path: '/logs/history', component: () => import('../views/HistoryLogs.vue') },
  { path: '/docs', component: () => import('../views/UnderConstructionView.vue'), props: { titleKey: 'common.menu.docs', descriptionKey: 'common.construction.docs_description' } },
  { path: '/support', component: () => import('../views/UnderConstructionView.vue'), props: { titleKey: 'common.menu.support', descriptionKey: 'common.construction.support_description' } }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

const setupGuard = createSetupGuard({
  statusRequest: () => setupApi.status(),
  getToken: () => localStorage.getItem('token'),
  state: setupStatusState
})
router.beforeEach(setupGuard)

export default router
