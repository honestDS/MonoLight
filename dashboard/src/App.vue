<template>
  <div id="app">
    <router-view v-if="isLoginPage"></router-view>
    <el-container v-else class="app-wrapper">
      <el-aside width="220px" class="sidebar">
        <div class="logo-container">
          <span class="logo-text">MonoLight</span>
        </div>
        <el-menu
          :default-active="$route.path"
          router
          class="side-menu">
          <el-menu-item index="/">
            <i class="el-icon-chat-line-round"></i>
            <span>智能交互</span>
          </el-menu-item>
          <el-menu-item index="/profiles">
            <i class="el-icon-operation"></i>
            <span>配置管理</span>
          </el-menu-item>
        </el-menu>
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
        '/profiles': '配置管理'
      };
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
