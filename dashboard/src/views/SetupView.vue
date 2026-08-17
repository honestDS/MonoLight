<template>
  <div class="setup-page">
    <div class="setup-language">
      <LanguageSwitcher />
    </div>

    <main class="setup-shell">
      <header class="setup-brand">
        <el-icon class="setup-brand-mark"><Setting /></el-icon>
        <span class="setup-brand-name">MonoLight</span>
      </header>

      <h1 class="setup-title">{{ t('setup.title') }}</h1>

      <section
        v-if="setupStatus.phase === 'idle' || setupStatus.phase === 'checking'"
        class="setup-status"
      >
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>{{ t('setup.status_checking') }}</span>
      </section>

      <section v-else-if="setupStatus.phase === 'error'" class="setup-status">
        <el-result
          icon="error"
          :title="t('setup.status_error_title')"
          :sub-title="statusErrorMessage"
        >
          <template #extra>
            <el-button type="primary" @click="refreshStatus">
              <el-icon><Refresh /></el-icon>
              <span>{{ t('setup.status_retry') }}</span>
            </el-button>
          </template>
        </el-result>
      </section>

      <section
        v-else-if="setupStatus.phase === 'ready' && setupStatus.required"
        class="setup-content"
      >
        <el-steps :active="activeStep" finish-status="success" class="setup-steps">
          <el-step :title="t('setup.step_admin')" />
          <el-step :title="t('setup.step_channel')" />
          <el-step :title="t('setup.step_profile')" />
        </el-steps>

        <div class="setup-section">
          <div class="setup-section-header">
            <h2 v-if="activeStep === 0">{{ t('setup.admin_title') }}</h2>
            <h2 v-else-if="activeStep === 1">{{ t('setup.channel_title') }}</h2>
            <h2 v-else>{{ t('setup.profile_title') }}</h2>
          </div>

          <el-form
            ref="adminFormRef"
            v-show="activeStep === 0"
            :model="form.admin"
            :rules="adminRules"
            label-position="top"
            @submit.prevent
          >
            <div class="setup-form-grid">
              <el-form-item :label="t('setup.username')" prop="username">
                <el-input
                  v-model="form.admin.username"
                  :placeholder="t('setup.username_placeholder')"
                  maxlength="50"
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item :label="t('setup.password')" prop="password">
                <el-input
                  v-model="form.admin.password"
                  :placeholder="t('setup.password_placeholder')"
                  type="password"
                  show-password
                  maxlength="72"
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item :label="t('setup.password_confirm')" prop="password_confirm">
                <el-input
                  v-model="form.admin.password_confirm"
                  :placeholder="t('setup.password_confirm_placeholder')"
                  type="password"
                  show-password
                  maxlength="72"
                  autocomplete="off"
                />
              </el-form-item>
            </div>
          </el-form>

          <el-form
            ref="channelFormRef"
            v-show="activeStep === 1"
            :model="form.channel"
            :rules="channelRules"
            label-position="top"
            @submit.prevent
          >
            <div class="setup-form-grid setup-form-grid--channel">
              <el-form-item :label="t('setup.channel_name')" prop="name">
                <el-input
                  v-model="form.channel.name"
                  :placeholder="t('setup.channel_name_placeholder')"
                  maxlength="200"
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item class="setup-field--wide" :label="t('setup.base_url')" prop="base_url">
                <el-input
                  v-model="form.channel.base_url"
                  :placeholder="t('setup.base_url_placeholder')"
                  maxlength="4096"
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item class="setup-field--wide" :label="t('setup.api_key')" prop="api_key">
                <el-input
                  v-model="form.channel.api_key"
                  :placeholder="t('setup.api_key_placeholder')"
                  type="password"
                  show-password
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item :label="t('setup.model_id')" prop="model_id">
                <el-input
                  v-model="form.channel.model_id"
                  :placeholder="t('setup.model_id_placeholder')"
                  maxlength="510"
                  autocomplete="off"
                />
              </el-form-item>

              <el-form-item :label="t('setup.protocol')" prop="protocol">
                <el-select v-model="form.channel.protocol" :placeholder="t('setup.protocol_placeholder')">
                  <el-option
                    v-for="option in protocolOptions"
                    :key="option.value"
                    :label="t('setup.' + option.labelKey)"
                    :value="option.value"
                  />
                </el-select>
              </el-form-item>
            </div>
          </el-form>

          <el-form
            ref="profileFormRef"
            v-show="activeStep === 2"
            :model="form.profile"
            :rules="profileRules"
            label-position="top"
            @submit.prevent
          >
            <div class="setup-form-grid">
              <el-form-item :label="t('setup.profile_name')" prop="name">
                <el-input
                  v-model="form.profile.name"
                  :placeholder="t('setup.profile_name_placeholder')"
                  maxlength="200"
                  autocomplete="off"
                />
              </el-form-item>
            </div>
          </el-form>

          <p v-if="completionError" class="setup-error">{{ completionErrorMessage }}</p>

          <div class="setup-actions">
            <el-button v-if="activeStep > 0" @click="previousStep">
              <el-icon><ArrowLeft /></el-icon>
              <span>{{ t('setup.back') }}</span>
            </el-button>
            <span v-else class="setup-actions-spacer" />

            <el-button v-if="activeStep < 2" type="primary" @click="nextStep">
              <span>{{ t('setup.next') }}</span>
              <el-icon><ArrowRight /></el-icon>
            </el-button>
            <el-button v-else type="primary" :loading="submitting" @click="completeSetup">
              <el-icon><Check /></el-icon>
              <span>{{ completionError ? t('setup.complete_retry') : t('setup.complete') }}</span>
            </el-button>
          </div>
        </div>
      </section>

      <section
        v-else-if="setupStatus.phase === 'ready' && setupStatus.required === false"
        class="setup-status"
      >
        <el-result
          icon="success"
          :title="t('setup.status_completed_title')"
          :sub-title="t('setup.status_completed_description')"
        >
          <template #extra>
            <el-button type="primary" @click="continueFromCompleted">
              <span>{{ t('setup.continue') }}</span>
              <el-icon><ArrowRight /></el-icon>
            </el-button>
          </template>
        </el-result>
      </section>

      <section v-else class="setup-status">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>{{ t('setup.status_checking') }}</span>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, Check, Loading, Refresh, Setting } from '@element-plus/icons-vue'
import { setupApi } from '@/api'
import LanguageSwitcher from '@/components/LanguageSwitcher.vue'
import {
  SETUP_PROTOCOLS,
  buildSetupRequest,
  readSetupTokenData,
  validateSetupApiKey,
  validateSetupBaseUrl,
  validateSetupModelId,
  validateSetupName,
  validateSetupPassword,
  validateSetupPasswordConfirmation,
  validateSetupProtocol,
  validateSetupUsername,
} from '@/utils/setupForm'
import {
  HOME_PATH,
  LOGIN_PATH,
  normalizeSetupError,
  refreshSetupStatus,
  setupStatusState,
} from '@/router/setupGuard'

const router = useRouter()
const { t } = useI18n()

const activeStep = ref(0)
const submitting = ref(false)
const completionError = ref(null)
const invalidTokenResponse = ref(false)
const setupStatus = ref(setupStatusState.getSnapshot())
const adminFormRef = ref()
const channelFormRef = ref()
const profileFormRef = ref()

const form = reactive({
  admin: {
    username: 'admin',
    password: '',
    password_confirm: '',
  },
  channel: {
    name: 'default',
    base_url: '',
    api_key: '',
    model_id: '',
    protocol: 'OPENAI',
  },
  profile: {
    name: 'default',
  },
})

const protocolOptions = SETUP_PROTOCOLS.map(value => ({
  value,
  labelKey: value === 'OPENAI' ? 'protocol_openai' : 'protocol_openai_responses',
}))

const createSetupValidator = validator => (_rule, value, callback) => {
  const error = validator(value)
  if (error) {
    callback(new Error(t('setup.' + error.key, error.params)))
    return
  }
  callback()
}

const adminRules = {
  username: [{ validator: createSetupValidator(validateSetupUsername), trigger: 'blur' }],
  password: [{ validator: createSetupValidator(validateSetupPassword), trigger: 'blur' }],
  password_confirm: [{
    validator: createSetupValidator(value => validateSetupPasswordConfirmation(value, form.admin.password)),
    trigger: 'blur',
  }],
}

const channelRules = {
  name: [{ validator: createSetupValidator(validateSetupName), trigger: 'blur' }],
  base_url: [{ validator: createSetupValidator(validateSetupBaseUrl), trigger: 'blur' }],
  api_key: [{ validator: createSetupValidator(validateSetupApiKey), trigger: 'blur' }],
  model_id: [{ validator: createSetupValidator(validateSetupModelId), trigger: 'blur' }],
  protocol: [{ validator: createSetupValidator(validateSetupProtocol), trigger: 'change' }],
}

const profileRules = {
  name: [{ validator: createSetupValidator(validateSetupName), trigger: 'blur' }],
}

const statusErrorMessage = computed(() => {
  const error = setupStatus.value.error
  if (
    error?.code !== null &&
    error?.code !== undefined &&
    typeof error.message === 'string' &&
    error.message.length > 0
  ) {
    return error.message
  }
  return t('setup.status_error_description')
})

const completionErrorMessage = computed(() => {
  if (invalidTokenResponse.value) {
    return t('setup.invalid_token_response')
  }

  const error = completionError.value
  if (error?.code !== null && error?.code !== undefined && typeof error.message === 'string' && error.message) {
    return error.message
  }
  return t('setup.complete_failed')
})

let unsubscribeSetupStatus

function continueFromCompleted() {
  router.replace(localStorage.getItem('token') ? HOME_PATH : LOGIN_PATH)
}

async function refreshStatus() {
  try {
    const required = await refreshSetupStatus({
      statusRequest: () => setupApi.status(),
      state: setupStatusState,
    })

    if (!required) {
      continueFromCompleted()
    }
  } catch {
    // The shared setup state provides the retryable error view.
  }
}

async function validateForm(formRef) {
  if (!formRef.value) return false

  try {
    return await formRef.value.validate()
  } catch {
    return false
  }
}

async function nextStep() {
  const formRefs = [adminFormRef, channelFormRef, profileFormRef]
  if (await validateForm(formRefs[activeStep.value])) {
    activeStep.value += 1
  }
}

function previousStep() {
  activeStep.value -= 1
}

async function completeSetup() {
  const formRefs = [adminFormRef, channelFormRef, profileFormRef]

  for (let index = 0; index < formRefs.length; index += 1) {
    if (!await validateForm(formRefs[index])) {
      activeStep.value = index
      return
    }
  }

  submitting.value = true
  completionError.value = null
  invalidTokenResponse.value = false

  try {
    const response = await setupApi.complete(buildSetupRequest(form))
    const tokenData = readSetupTokenData(response)
    const { access_token, token_type } = tokenData || {}

    if (
      !tokenData ||
      typeof access_token !== 'string' ||
      !access_token ||
      typeof token_type !== 'string' ||
      !token_type
    ) {
      completionError.value = { code: null, message: '' }
      invalidTokenResponse.value = true
      ElMessage.error(t('setup.invalid_token_response'))
      return
    }

    localStorage.setItem('token', access_token)
    setupStatusState.setReady(false)
    form.admin.password = ''
    form.admin.password_confirm = ''
    form.channel.api_key = ''
    ElMessage.success(t('setup.complete_success'))
    router.replace(HOME_PATH)
  } catch (error) {
    completionError.value = normalizeSetupError(error)
    ElMessage.error(completionErrorMessage.value)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  unsubscribeSetupStatus = setupStatusState.subscribe(snapshot => {
    setupStatus.value = snapshot
  })
  setupStatus.value = setupStatusState.getSnapshot()

  if (setupStatus.value.phase === 'idle') {
    void refreshStatus()
  }
})

onBeforeUnmount(() => {
  unsubscribeSetupStatus?.()
  unsubscribeSetupStatus = undefined
})
</script>

<style lang="scss">
@import '@/assets/css/setup.scss';
</style>
