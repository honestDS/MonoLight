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
        :width="sidebarWidth"
        class="sidebar"
        :class="{ 'is-collapsed': isSidebarCollapsed }"
      >
        <div class="logo-container">
          <img class="logo-image" :src="logoImage" alt="" aria-hidden="true">
          <div class="logo-content">
            <span class="logo-text">MonoLight</span>
            <span class="logo-version">MonoLight v0.1</span>
          </div>
        </div>
        <el-tooltip
          :content="$t(isSidebarCollapsed ? 'common.expand_sidebar' : 'common.collapse_sidebar')"
          :disabled="isCollapsedSubmenuOpen"
          placement="right"
        >
          <button
            type="button"
            class="sidebar-collapse-button"
            :class="{ 'is-submenu-open': isCollapsedSubmenuOpen }"
            :aria-label="$t(isSidebarCollapsed ? 'common.expand_sidebar' : 'common.collapse_sidebar')"
            @click="isSidebarCollapsed = !isSidebarCollapsed"
          >
            <el-icon
              class="sidebar-collapse-arrow"
              :class="{ 'is-collapsed': isSidebarCollapsed }"
            >
              <ArrowLeft />
            </el-icon>
          </button>
        </el-tooltip>
        <el-menu
          :default-active="$route.path"
          :collapse="isSidebarCollapsed"
          :collapse-transition="false"
          :popper-offset="-4"
          popper-class="sidebar-submenu-popper"
          router
          class="side-menu"
          @open="handleSidebarSubmenuOpen"
          @close="handleSidebarSubmenuClose">
          <el-menu-item index="/">
            <el-icon><ChatDotRound /></el-icon>
            <template #title><span>{{ $t('common.menu.chat') }}</span></template>
          </el-menu-item>
          <el-menu-item index="/users">
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
          <el-sub-menu index="/system">
            <template #title>
              <el-icon><Setting /></el-icon>
              <span>{{ $t('common.menu.system') }}</span>
            </template>
            <el-menu-item index="/profiles">
              <span>{{ $t('common.menu.profiles') }}</span>
            </el-menu-item>
            <el-menu-item index="/channels">
              <span>{{ $t('common.menu.channels') }}</span>
            </el-menu-item>
            <el-menu-item index="/message-platforms">
              <span>{{ $t('common.menu.message_platforms') }}</span>
            </el-menu-item>
            <el-menu-item index="/prompts">
              <span>{{ $t('common.menu.prompts') }}</span>
            </el-menu-item>
          </el-sub-menu>
          <el-sub-menu index="/logs">
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
            <el-button type="text" @click="logout">{{ $t('common.logout') }}</el-button>
          </div>

        </el-header>
        <el-main
          class="app-main"
          :class="{
            'app-main--memories': $route.path === '/memories',
            'app-main--chat': $route.path === '/'
          }"
        >
          <transition name="fade" mode="out-in">
            <router-view></router-view>
          </transition>
          <div class="app-footer">
              <span>&copy; 2026 MonoLight LLM Admin. All rights reserved.</span>
            </div>
          </el-main>
        </el-container>
      </el-container>
    </div>
  </el-config-provider>
</template>

<script>
import { useResizeObserver } from './composables/useResizeObserver'
import { routeNameMap } from './constants'
import LanguageSwitcher from './components/LanguageSwitcher.vue'
import githubIcon from './assets/svg/github.svg'
import logoImage from '../../logo.jpg'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import en from 'element-plus/es/locale/lang/en'

// 初始化 ResizeObserver 防抖补丁
useResizeObserver()

export default {
  name: 'App',
  components: {
    LanguageSwitcher
  },
  data() {
    return {
      githubIcon,
      logoImage,
      isSidebarCollapsed: false,
      isCollapsedSubmenuOpen: false
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
    sidebarWidth() {
      return this.isSidebarCollapsed ? '72px' : '236px'
    }
  },
  methods: {
    handleSidebarSubmenuOpen() {
      if (this.isSidebarCollapsed) {
        this.isCollapsedSubmenuOpen = true
      }
    },
    handleSidebarSubmenuClose() {
      this.isCollapsedSubmenuOpen = false
    },
    logout() {
      localStorage.removeItem('token')
      this.$router.push('/login')
    }
  }
}
</script>

<style lang="scss">
@import "@/assets/css/app.scss";
</style>
