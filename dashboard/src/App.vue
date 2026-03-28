<template>
  <div id="app">
    <router-view v-if="isLoginPage"></router-view>
    <el-container v-else class="app-wrapper">
      <el-aside width="220px" class="sidebar">
        <div class="logo-container">
          <div class="logo-icon">
            <i class="el-icon-s-grid"></i>
          </div>
          <div class="logo-content">
            <span class="logo-text">MonoLight</span>
            <span class="logo-version">LLM Admin v1.0</span>
          </div>
        </div>
        <el-menu
          :default-active="$route.path"
          router
          class="side-menu">
          <el-menu-item index="/">
            <span>智能交互</span>
          </el-menu-item>
          <el-menu-item index="/users">
            <span>用户管理</span>
          </el-menu-item>
          <el-sub-menu index="/system">
            <template #title>
              <span>系统配置</span>
            </template>
            <el-menu-item index="/profiles">
              <span>配置管理</span>
            </el-menu-item>
            <el-menu-item index="/providers">
              <span>模型管理</span>
            </el-menu-item>
            <el-menu-item index="/prompts">
              <span>提示词管理</span>
            </el-menu-item>
          </el-sub-menu>
        </el-menu>
        <div class="sidebar-footer">
          <el-menu
            :default-active="$route.path"
            router
            class="side-menu-footer">
            <el-menu-item index="/docs">
              <span>文档中心</span>
            </el-menu-item>
            <el-menu-item index="/support">
              <span>技术支持</span>
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
            <el-button type="text" @click="logout">退出登录</el-button>
          </div>
        </el-header>
        <el-main class="app-main">
          <transition name="fade" mode="out-in">
            <router-view></router-view>
          </transition>
          <div class="app-footer">
            <span>&copy; 2024 MonoLight LLM Admin. All rights reserved.</span>
          </div>
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script>

// 修复 ResizeObserver loop completed with undelivered notifications 错误
const debounce = (fn, delay) => {
  let timer = null;
  return function () {
    let context = this;
    let args = arguments;
    clearTimeout(timer);
    timer = setTimeout(function () {
      fn.apply(context, args);
    }, delay);
  };
};

const _ResizeObserver = window.ResizeObserver;
window.ResizeObserver = class ResizeObserver extends _ResizeObserver {
  constructor(callback) {
    callback = debounce(callback, 16);
    super(callback);
  }
};

export default {
  name: 'App',
  computed: {
    isLoginPage() {
      return this.$route.path === '/login';
    },
    currentRouteName() {
      const map = {
        '/': '智能交互',
        '/users': '用户管理',
        '/profiles': '配置管理',
        '/providers': '模型管理',
        '/prompts': '提示词管理'
      };
      // 如果是系统配置子菜单中的路由，显示系统配置
      const path = this.$route.path;
      if (path === '/profiles' || path === '/providers' || path === '/prompts') {
        return '系统配置';
      }
      return map[this.$route.path] || '控制面板';
    }
  },
  methods: {
    logout() {
      localStorage.removeItem('token');
      this.$router.push('/login');
    }
  }
}
</script>

<style lang="scss">
@import "@/assets/css/app.scss";
</style>
