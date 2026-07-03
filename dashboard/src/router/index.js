import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('../views/LoginView.vue') },
  { path: '/', component: () => import('../views/ChatView.vue') },
  { path: '/profiles', component: () => import('../views/ProfilesView.vue') },
  { path: '/prompts', component: () => import('../views/PromptsView.vue') },
  { path: '/scheduled-tasks', component: () => import('../views/ScheduledTasksView.vue') },
  { path: '/channels', component: () => import('../views/ChannelsView.vue') },
  { path: '/message-platforms', component: () => import('../views/MessagePlatformsView.vue') },
  { path: '/users', component: () => import('../views/UsersView.vue') },
  { path: '/knowledge-base', component: () => import('../views/KnowledgeBase.vue') },
  { path: '/logs/realtime', component: () => import('../views/RealTimeLogs.vue') },
  { path: '/logs/history', component: () => import('../views/HistoryLogs.vue') }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

router.beforeEach((to, from, next) => {
  const token = localStorage.getItem('token')
  if (to.path !== '/login' && !token) {
    next('/login')
  } else {
    next()
  }
})

export default router
