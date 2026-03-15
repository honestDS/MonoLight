<template>
  <div class="login-page">
    <div class="login-box">
      <!-- 左侧品牌插图区 -->
      <div class="login-left">
        <div class="brand-info">
          <h1 class="brand-logo">MonoLight</h1>
          <p class="brand-desc">极致、纯粹的智能进化实体</p>
        </div>
        <div class="illustration">
          <!-- 使用纯 CSS 绘制简约几何图形或占位 -->
          <div class="circle-bg"></div>
        </div>
      </div>
      
      <!-- 右侧表单区 -->
      <div class="login-right">
        <div class="form-header">
          <h2>Welcome</h2>
          <p>请开启您的数字进化之旅</p>
        </div>
        
        <el-form :model="form" class="login-form" @keyup.native.enter="handleLogin">
          <el-form-item>
            <el-input v-model="form.username" placeholder="Username" class="custom-input"></el-input>
          </el-form-item>
          <el-form-item>
            <el-input v-model="form.password" type="password" placeholder="Password" show-password class="custom-input"></el-input>
          </el-form-item>
          
          <div class="form-actions">
            <el-button type="primary" class="btn-login" :loading="loading" @click="handleLogin">Login</el-button>
            <el-button plain class="btn-signup" disabled>Sign up</el-button>
          </div>
          
          <div class="form-footer">
            <a href="javascript:;" class="forgot-pwd">Forgot password?</a>
          </div>
        </el-form>

        <div class="login-copyright">
          <span>MonoLight © 2026</span>
          <div class="footer-links">
            <a href="#">Policy</a>
            <a href="#">Terms</a>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { authApi } from '../api';
export default {
  data() { return { loading: false, form: { username: '', password: '' } }; },
  methods: {
    async handleLogin() {
      if (!this.form.username || !this.form.password) return;
      this.loading = true;
      try {
        const res = await authApi.login(this.form);
        localStorage.setItem('token', res.data.data.access_token);
        this.$message.success('验证成功，欢迎归位');
        this.$router.push('/');
      } catch (err) { 
        this.$message.error(err.message || '登录凭证无效'); 
      } finally { this.loading = false; }
    }
  }
}
</script>
<style lang="scss">@import "@/assets/css/login.scss";</style>