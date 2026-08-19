<template>
  <div class="login-page">
    <div class="login-lang-switcher">
      <LanguageSwitcher />
    </div>
    <div class="login-box">
      <a
        class="github-link"
        href="https://github.com/honestDS/MonoLight"
        target="_blank"
        rel="noopener noreferrer"
        aria-label="GitHub"
      >
        <img :src="githubIcon" alt="GitHub" class="github-icon">
      </a>

      <div class="login-left">
        <div class="brand-info">
          <div class="brand-title">
            <img class="brand-image" :src="logoImage" alt="" aria-hidden="true">
            <h1 class="brand-logo">MonoLight</h1>
          </div>
          <p class="brand-desc">{{ $t('login.brand_desc') }}</p>
        </div>
        <div class="illustration">
          <div class="circle-bg"></div>
        </div>
      </div>
      
      <div class="login-right">
        <div class="form-header">
          <h2>{{ $t('login.welcome') }}</h2>
          <p>{{ $t('login.subtitle') }}</p>
        </div>

        <el-form :model="form" class="login-form" @keyup.enter="handleLogin">
          <el-form-item>
            <el-input v-model="form.username" :placeholder="$t('login.username')" class="custom-input"></el-input>
          </el-form-item>
          <el-form-item>
            <el-input v-model="form.password" type="password" :placeholder="$t('login.password')" show-password class="custom-input"></el-input>
          </el-form-item>
          
          <div class="form-actions">
            <el-button type="primary" class="btn-login" :loading="loading" @click="handleLogin">{{ $t('login.submit') }}</el-button>
            <el-button plain class="btn-signup" disabled>{{ $t('login.signup') }}</el-button>
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

<script setup>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { authApi } from '../api'
import LanguageSwitcher from '../components/LanguageSwitcher.vue'
import githubIcon from '../assets/svg/github.svg'
import logoImage from '../../../logo.jpg'
const { t } = useI18n()
const router = useRouter()

const loading = ref(false)
const form = reactive({ username: '', password: '' })

const handleLogin = async () => {
  if (!form.username || !form.password) return
  loading.value = true
  try {
    const res = await authApi.login(form)
    localStorage.setItem('token', res.data.data.access_token)
    ElMessage.success(t('login.success'))
    router.push('/')
  } catch (err) {
    ElMessage.error(err.message || t('login.login_failed'))
  } finally {
    loading.value = false
  }
}

</script>

<style lang="scss">@import "@/assets/css/login.scss";</style>
