<template>
  <div class="login-page">
    <div class="login-lang-switcher">
      <LanguageSwitcher />
    </div>
    <div class="login-box">

      <div class="login-left">
        <div class="brand-info">
          <h1 class="brand-logo">MonoLight</h1>
          <p class="brand-desc">极致、纯粹的智能进化实体</p>
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

        <el-dialog :title="$t('login.reset_admin')" v-model="resetDialog" width="400px" append-to-body center align-center>
          <div class="reset-hint">{{ $t('login.reset_hint') }}</div>
          <el-input v-model="resetToken" :placeholder="$t('login.reset_token_placeholder')" show-password class="custom-input"></el-input>
          <template #footer>
            <div class="dialog-footer">
              <el-button @click="resetDialog = false" size="default">{{ $t('login.cancel') }}</el-button>
              <el-button type="primary" :loading="resetLoading" @click="handleResetAdmin" size="default">{{ $t('login.reset_confirm') }}</el-button>
            </div>
          </template>
        </el-dialog>
        
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
          
          <div class="form-footer">
            <a href="javascript:;" class="forgot-pwd" @click="resetDialog = true">{{ $t('login.reset_admin') }}?</a>
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { authApi } from '../api'
import LanguageSwitcher from '../components/LanguageSwitcher.vue'


const { t } = useI18n()
const router = useRouter()

const loading = ref(false)
const resetLoading = ref(false)
const resetDialog = ref(false)
const resetToken = ref('')
const form = reactive({ username: '', password: '' })

const handleLogin = async () => {
  if (!form.username || !form.password) return
  loading.ref = true
  try {
    const res = await authApi.login(form)
    localStorage.setItem('token', res.data.data.access_token)
    ElMessage.success(t('login.success'))
    router.push('/')
  } catch (err) {
    ElMessage.error(err.message || '登录失败')
  } finally {
    loading.value = false
  }
}

const handleResetAdmin = async () => {
  if (!resetToken.value) return ElMessage.warning(t('login.reset_token_placeholder'))
  resetLoading.value = true
  try {
    const res = await authApi.resetAdmin(resetToken.value)
    const data = res.data.data
    ElMessageBox.alert('账号：' + data.登录账号 + '<br>初始密码：' + data.初始密码, t('login.reset_admin'), {
      dangerouslyUseHTMLString: true,
      confirmButtonText: t('login.success'), center: true
    })
    resetDialog.value = false
    resetToken.value = ''
  } catch (err) {
    ElMessage.error(err.message || '重置失败')
  } finally {
    resetLoading.value = false
  }
}
</script>

<style lang="scss">@import "@/assets/css/login.scss";</style>
