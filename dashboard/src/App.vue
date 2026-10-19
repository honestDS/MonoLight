<template>
  <el-config-provider :locale="locale">
    <div id="app">
      <router-view v-if="isStandalonePage"></router-view>
    <el-container
      v-else
      class="app-wrapper"
      :class="{ 'is-sidebar-collapsed': isSidebarCollapsed }"
    >
      <el-aside
        width="var(--sidebar-current-width)"
        class="sidebar"
        :class="{ 'is-collapsed': isSidebarCollapsed }"
      >
        <div class="logo-container">
          <el-tooltip
            v-if="isSidebarCollapsed"
            :content="$t('common.expand_sidebar')"
            placement="right"
          >
            <button
              type="button"
              class="sidebar-logo-expand-button"
              :aria-label="$t('common.expand_sidebar')"
              @click="handleSidebarToggle"
            >
              <img class="logo-image" :src="logoImage" alt="" aria-hidden="true">
              <svg
                class="sidebar-panel-icon sidebar-logo-expand-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.75"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <rect x="3" y="3" width="18" height="18" rx="3" />
                <path d="M9 3v18" />
              </svg>
            </button>
          </el-tooltip>
          <img v-else class="logo-image" :src="logoImage" alt="" aria-hidden="true">
          <div class="logo-content">
            <span class="logo-text">MonoLight</span>
            <span class="logo-version">MonoLight v0.1</span>
          </div>
          <el-tooltip
            v-if="!isSidebarCollapsed"
            :content="$t('common.collapse_sidebar')"
            :disabled="isCollapsedSubmenuOpen"
            placement="right"
          >
            <button
              type="button"
              class="sidebar-collapse-button"
              :aria-label="$t('common.collapse_sidebar')"
              @click="handleSidebarToggle"
            >
              <svg
                class="sidebar-panel-icon"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.75"
                stroke-linecap="round"
                stroke-linejoin="round"
                aria-hidden="true"
              >
                <rect x="3" y="3" width="18" height="18" rx="3" />
                <path d="M9 3v18" />
              </svg>
            </button>
          </el-tooltip>
        </div>
        <el-menu
          :default-active="$route.path"
          :collapse="isSidebarCollapsed"
          :collapse-transition="false"
          :popper-offset="-4"
          popper-class="sidebar-menu-tooltip"
          router
          class="side-menu"
          @open="handleSidebarSubmenuOpen"
          @close="handleSidebarSubmenuClose">
          <el-menu-item index="/">
            <el-icon><ChatDotRound /></el-icon>
            <template #title><span>{{ $t('common.menu.chat') }}</span></template>
          </el-menu-item>
          <el-menu-item v-if="userPermissions.isSuperuser" index="/users">
            <el-icon><User /></el-icon>
            <template #title><span>{{ $t('common.menu.users') }}</span></template>
          </el-menu-item>
          <el-menu-item index="/memories">
            <el-icon><Collection /></el-icon>
            <template #title><span>{{ $t('common.menu.memories') }}</span></template>
          </el-menu-item>
          <el-menu-item index="/scheduled-tasks">
            <el-icon><Clock /></el-icon>
            <template #title><span>{{ $t('common.menu.scheduled_tasks') }}</span></template>
          </el-menu-item>
          <el-menu-item index="/knowledge-base">
            <el-icon><Reading /></el-icon>
            <template #title><span>{{ $t('common.menu.knowledge_base') }}</span></template>
          </el-menu-item>
          <el-sub-menu index="/system" popper-class="sidebar-submenu-popper">
            <template #title>
              <el-icon><Setting /></el-icon>
              <span>{{ $t('common.menu.system') }}</span>
            </template>
            <el-menu-item index="/profiles">
              <span>{{ $t('common.menu.profiles') }}</span>
            </el-menu-item>
            <el-menu-item v-if="userPermissions.isSuperuser" index="/channels">
              <span>{{ $t('common.menu.channels') }}</span>
            </el-menu-item>
            <el-menu-item index="/message-platforms">
              <span>{{ $t('common.menu.message_platforms') }}</span>
            </el-menu-item>
            <el-menu-item index="/prompts">
              <span>{{ $t('common.menu.prompts') }}</span>
            </el-menu-item>
            <el-menu-item v-if="userPermissions.isSuperuser" index="/skills">
              <span>{{ $t('common.menu.skills') }}</span>
            </el-menu-item>
          </el-sub-menu>
          <el-sub-menu v-if="userPermissions.isSuperuser" index="/logs" popper-class="sidebar-submenu-popper">
            <template #title>
              <el-icon><Document /></el-icon>
              <span>{{ $t('common.menu.logs') }}</span>
            </template>
            <el-menu-item index="/logs/realtime">
              <span>{{ $t('common.menu.realtime_logs') }}</span>
            </el-menu-item>
            <el-menu-item index="/logs/history">
              <span>{{ $t('common.menu.history_logs') }}</span>
            </el-menu-item>
          </el-sub-menu>
        </el-menu>
        <div class="sidebar-footer">
          <el-menu
            :default-active="$route.path"
            :collapse="isSidebarCollapsed"
            :collapse-transition="false"
            router
            class="side-menu-footer">
            <el-menu-item index="/docs">
              <el-icon><Tickets /></el-icon>
              <template #title><span>{{ $t('common.menu.docs') }}</span></template>
            </el-menu-item>
            <el-menu-item index="/support">
              <el-icon><Service /></el-icon>
              <template #title><span>{{ $t('common.menu.support') }}</span></template>
            </el-menu-item>
          </el-menu>
          <small class="sidebar-copyright">&copy; 2026 MonoLight. All rights reserved.</small>
        </div>
      </el-aside>
      <el-container>
        <el-header class="app-header">
          <div class="header-left">
            <span class="breadcrumb">{{ currentRouteName }}</span>
          </div>
          <div class="header-right">
            <a
              class="github-link"
              href="https://github.com/honestDS/MonoLight"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="GitHub"
            >
              <img :src="githubIcon" alt="GitHub" class="github-icon">
            </a>
            <LanguageSwitcher class="header-lang-switcher" />
            <SessionTaskCenter
              :tasks="sessionTasks"
              :error="sessionTaskError"
              :loading="sessionTasksLoading"
              @refresh="refreshSessionTasks"
              @open="openTaskSession"
            />
            <el-dropdown
              trigger="click"
              placement="bottom-end"
              @command="handleAccountCommand"
            >
              <button type="button" class="header-account-trigger">
                <el-icon aria-hidden="true"><User /></el-icon>
                <span>{{ $t('common.account.title') }}</span>
                <el-icon aria-hidden="true"><ArrowDown /></el-icon>
              </button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="change-password">
                    {{ $t('common.account.change_password') }}
                  </el-dropdown-item>
                  <el-dropdown-item command="logout" divided class="danger-dropdown-item">
                    {{ $t('common.logout') }}
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>

        </el-header>
        <el-main
          class="app-main"
          :class="{
            'app-main--chat': $route.path === '/'
          }"
        >
          <transition name="fade" mode="out-in">
            <router-view></router-view>
          </transition>
          </el-main>
        </el-container>
      </el-container>
      <el-dialog
        v-if="!isStandalonePage"
        v-model="passwordDialogVisible"
        :title="$t('common.account.change_password')"
        class="standard-dialog change-password-dialog dialog-with-scroll-body"
        align-center
        :show-close="!passwordSubmitting"
        :close-on-click-modal="!passwordSubmitting"
        :close-on-press-escape="!passwordSubmitting"
        @closed="resetPasswordForm"
      >
        <el-form
          ref="passwordFormRef"
          :model="passwordForm"
          :rules="passwordRules"
          label-position="top"
          :disabled="passwordSubmitting"
          @submit.prevent="submitPasswordChange"
        >
          <el-alert
            v-if="passwordRequestError"
            :title="passwordRequestError"
            type="error"
            :closable="false"
            show-icon
            class="change-password-error"
            role="alert"
          />
          <el-form-item
            prop="current_password"
            :label="$t('common.account.current_password')"
            for="current-password"
          >
            <el-input
              id="current-password"
              v-model="passwordForm.current_password"
              type="password"
              show-password
              autocomplete="current-password"
            />
          </el-form-item>
          <el-form-item
            prop="new_password"
            :label="$t('common.account.new_password')"
            for="new-password"
          >
            <el-input
              id="new-password"
              v-model="passwordForm.new_password"
              type="password"
              show-password
              autocomplete="new-password"
            />
            <div class="help-text">{{ $t('common.account.password_hint') }}</div>
          </el-form-item>
          <el-form-item
            prop="confirm_password"
            :label="$t('common.account.confirm_password')"
            for="confirm-password"
          >
            <el-input
              id="confirm-password"
              v-model="passwordForm.confirm_password"
              type="password"
              show-password
              autocomplete="new-password"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <div class="dialog-footer">
            <el-button :disabled="passwordSubmitting" @click="passwordDialogVisible = false">
              {{ $t('common.cancel') }}
            </el-button>
            <el-button type="primary" :loading="passwordSubmitting" @click="submitPasswordChange">
              {{ $t('common.confirm') }}
            </el-button>
          </div>
        </template>
      </el-dialog>
    </div>
  </el-config-provider>
</template>

<script>
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'

import { useSessionTasks } from './composables/chat/useSessionTasks.js'
import { useUserPermissionsStore } from './composables/useUserPermissions.js'
import { routeNameMap } from './constants'
import { authApi } from './api'
import LanguageSwitcher from './components/LanguageSwitcher.vue'
import SessionTaskCenter from './components/SessionTaskCenter.vue'
import githubIcon from './assets/svg/github.svg'
import logoImage from '../../logo.jpg'
import './assets/css/app.scss'

export default {
  name: 'App',
  components: {
    LanguageSwitcher,
    SessionTaskCenter
  },
  setup() {
    const userPermissions = useUserPermissionsStore()
    const service = useSessionTasks()
    return {
      userPermissions,
      sessionTasks: service.tasks,
      sessionTaskError: service.error,
      sessionTasksLoading: service.loading,
      refreshSessionTasks: service.refresh,
      openTaskSession: service.openSession
    }
  },
  data() {
    return {
      githubIcon,
      logoImage,
      isSidebarCollapsed: false,
      isCollapsedSubmenuOpen: false,
      passwordDialogVisible: false,
      passwordSubmitting: false,
      passwordRequestError: '',
      passwordForm: {
        current_password: '',
        new_password: '',
        confirm_password: ''
      }
    }
  },

  computed: {
    isStandalonePage() {
      return ['/login', '/setup', '/backend-unavailable'].includes(this.$route.path)
    },
    currentRouteName() {
      const path = this.$route.path
      const key = routeNameMap[path]
      return key ? this.$t(key) : this.$t('common.menu.chat') // fallback 
    },
    locale() {
      return this.$i18n.locale === 'zh' ? zhCn : en
    },
    passwordRules() {
      return {
        current_password: [
          {
            required: true,
            message: this.$t('common.account.required'),
            trigger: 'blur'
          },
          {
            validator: (_rule, value, callback) => {
              if (!value) {
                callback()
                return
              }
              if (new TextEncoder().encode(value).length > 72) {
                callback(new Error(this.$t('common.account.password_bytes')))
                return
              }
              callback()
            },
            trigger: 'blur'
          }
        ],
        new_password: [
          {
            required: true,
            message: this.$t('common.account.required'),
            trigger: 'blur'
          },
          {
            validator: (_rule, value, callback) => {
              if (!value) {
                callback()
                return
              }
              if (new TextEncoder().encode(value).length > 72) {
                callback(new Error(this.$t('common.account.password_bytes')))
                return
              }
              if (Array.from(value).length < 8) {
                callback(new Error(this.$t('common.account.password_length')))
                return
              }
              callback()
            },
            trigger: 'blur'
          }
        ],
        confirm_password: [
          {
            required: true,
            message: this.$t('common.account.required'),
            trigger: 'blur'
          },
          {
            validator: (_rule, value, callback) => {
              if (!value) {
                callback()
                return
              }
              if (value !== this.passwordForm.new_password) {
                callback(new Error(this.$t('common.account.password_mismatch')))
                return
              }
              callback()
            },
            trigger: ['blur', 'change']
          }
        ]
      }
    }
  },
  watch: {
    isStandalonePage(value) {
      if (value) {
        this.passwordDialogVisible = false
        this.resetPasswordForm()
      }
    }
  },
  methods: {
    handleSidebarToggle() {
      this.isSidebarCollapsed = !this.isSidebarCollapsed
      this.isCollapsedSubmenuOpen = false
    },
    handleSidebarSubmenuOpen() {
      if (this.isSidebarCollapsed) {
        this.isCollapsedSubmenuOpen = true
      }
    },
    handleSidebarSubmenuClose() {
      this.isCollapsedSubmenuOpen = false
    },
    handleAccountCommand(command) {
      if (command === 'logout') {
        this.logout()
        return
      }
      if (command !== 'change-password') {
        return
      }
      this.resetPasswordForm()
      this.passwordDialogVisible = true
      this.$nextTick(() => {
        if (this.$refs.passwordFormRef) {
          this.$refs.passwordFormRef.clearValidate()
        }
      })
    },
    resetPasswordForm() {
      this.passwordForm.current_password = ''
      this.passwordForm.new_password = ''
      this.passwordForm.confirm_password = ''
      this.passwordRequestError = ''
      if (this.$refs.passwordFormRef) {
        this.$refs.passwordFormRef.clearValidate()
      }
    },
    async submitPasswordChange() {
      if (this.passwordSubmitting) {
        return
      }
      const initialToken = localStorage.getItem('token')
      if (!initialToken) {
        this.logout()
        return
      }
      this.passwordSubmitting = true
      this.passwordRequestError = ''
      try {
        const isValid = await this.$refs.passwordFormRef.validate().catch(() => false)
        if (!isValid) {
          return
        }
        if (localStorage.getItem('token') !== initialToken) {
          return
        }
        await authApi.changePassword({
          current_password: this.passwordForm.current_password,
          new_password: this.passwordForm.new_password
        })
        if (localStorage.getItem('token') !== initialToken) {
          return
        }
        this.passwordDialogVisible = false
        this.resetPasswordForm()
        ElMessage.success(this.$t('common.account.success'))
        this.logout()
      } catch (err) {
        if (localStorage.getItem('token') === initialToken && this.passwordDialogVisible) {
          this.passwordRequestError = (err && err.message) || this.$t('common.account.failed')
        }
      } finally {
        this.passwordSubmitting = false
      }
    },
    logout() {
      this.userPermissions.clear()
      localStorage.removeItem('token')
      this.$router.push('/login')
    }
  }
}
</script>
