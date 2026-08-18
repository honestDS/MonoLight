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

          <div class="setup-content-viewport">
            <Transition :name="stepTransitionName" mode="out-in">
              <div :key="activeStep" class="setup-step-content">
                <el-form
                  ref="adminFormRef"
                  v-show="activeStep === 0"
                  :model="form.admin"
                  :rules="adminRules"
                  label-position="top"
                  @submit.prevent
                >
                  <div class="setup-form-grid">
                    <el-form-item class="setup-field--wide" :label="t('setup.username')" prop="username">
                      <el-input
                        v-model="form.admin.username"
                        :placeholder="t('setup.username_placeholder')"
                        maxlength="50"
                        autocomplete="off"
                      />
                    </el-form-item>

                    <el-form-item class="setup-field--wide" :label="t('setup.password')" prop="password">
                      <el-input
                        v-model="form.admin.password"
                        :placeholder="t('setup.password_placeholder')"
                        type="password"
                        show-password
                        maxlength="72"
                        autocomplete="off"
                      />
                    </el-form-item>

                    <el-form-item class="setup-field--wide" :label="t('setup.password_confirm')" prop="password_confirm">
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
                  class="channel-settings-form"
                  label-position="top"
                  @submit.prevent
                >
                  <div class="setup-form-grid setup-form-grid--channel channel-settings-row--fields">
                    <el-form-item :label="t('setup.channel_name')" prop="name">
                      <el-input
                        v-model="form.channel.name"
                        :placeholder="t('setup.channel_name_placeholder')"
                        maxlength="200"
                        autocomplete="off"
                      />
                    </el-form-item>

                    <el-form-item :label="t('setup.base_url')" prop="base_url">
                      <el-input
                        v-model="form.channel.base_url"
                        :placeholder="t('setup.base_url_placeholder')"
                        maxlength="4096"
                        autocomplete="off"
                      />
                    </el-form-item>

                    <el-form-item :label="t('setup.api_key')" prop="api_key">
                      <el-input
                        v-model="form.channel.api_key"
                        :placeholder="t('setup.api_key_placeholder')"
                        type="password"
                        show-password
                        autocomplete="off"
                      />
                    </el-form-item>

                    <el-form-item
                      :label="t('channels.http_proxy')"
                      prop="http_proxy"
                      :error="proxyError"
                      class="http-proxy-form-item"
                    >
                      <div class="http-proxy-input-wrapper">
                        <el-input
                          v-model="form.channel.http_proxy"
                          :placeholder="t('channels.http_proxy_placeholder')"
                          @input="proxyError = ''"
                        />
                      </div>
                    </el-form-item>

                    <el-form-item class="channel-model-detect-form-item">
                      <div class="channel-model-detect-row">
                        <el-button type="primary" plain :loading="detectingModels" @click="detectModelList">
                          {{ t('channels.detect_model_list') }}
                        </el-button>
                        <el-select
                          v-model="selectedDetectedModel"
                          class="channel-model-detect-select"
                          popper-class="channel-model-detect-popper"
                          filterable
                          clearable
                          fit-input-width
                          :placeholder="t('channels.select_detected_model')"
                          :disabled="detectedModels.length === 0"
                          @change="handleDetectedModelChange"
                        >
                          <el-option v-for="model in detectedModels" :key="model.id" :label="model.id" :value="model.id">
                            <div class="detected-model-option">
                              <span class="detected-model-name" :title="model.id">{{ model.id }}</span>
                              <span class="detected-model-meta">
                                <span v-if="model.owned_by" class="detected-model-owner" :title="model.owned_by">{{ model.owned_by }}</span>
                              </span>
                            </div>
                          </el-option>
                        </el-select>
                      </div>
                    </el-form-item>
                  </div>

                  <ChannelModelEntry
                    :entry="form.channel"
                    :index="0"
                    :model-usages="['CHAT']"
                    :model-protocols="SETUP_PROTOCOLS"
                    model-id-prop="model_id"
                    protocol-prop="protocol"
                    :model-id-error="modelIdError"
                    :protocol-error="protocolError"
                    :advanced-settings-draft="advancedSettingsDraft"
                    :advanced-settings-error="advancedSettingsError"
                    :advanced-settings-expanded="advancedSettingsExpanded"
                    :custom-headers-placeholder="customHeadersPlaceholder"
                    :testing="modelTestState?.status === 'running'"
                    :test-state="modelTestState"
                    :detecting-metadata="detectingMetadata"
                    :metadata-detection-disabled="detectingMetadata"
                    :show-remove="false"
                    :show-enabled="false"
                    @test-chat="openChatTestDialog"
                    @detect-metadata="detectModelMetadata"
                    @model-id-input="handleModelIdInput"
                    @protocol-change="handleProtocolChange"
                    @advanced-settings-input="handleAdvancedSettingsInput"
                    @fill-headers-template="fillCustomHeadersTemplate"
                    @test-config-change="clearChannelTest"
                    @view-test-result="openModelTestResult"
                    @update:advanced-settings-draft="value => advancedSettingsDraft = value"
                    @update:advanced-settings-expanded="value => advancedSettingsExpanded = value"
                  />
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
            </Transition>
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

    <ModelTestResultDialog
      v-model:visible="modelTestResultDialogVisible"
      :results="modelTestResults"
      :active-id="activeModelTestResultId"
      @update:active-id="activeModelTestResultId = $event"
    />

    <el-dialog
      v-model="chatTestDialogVisible"
      :title="t('channels.chat_test_config_title')"
      width="520px"
      append-to-body
      :close-on-click-modal="true"
      @closed="resetChatTestDialogState"
    >
      <el-form :model="chatTestForm" label-position="top" class="chat-test-form">
        <el-form-item :label="t('channels.chat_test_mode')">
          <el-radio-group v-model="chatTestForm.testMode">
            <el-radio label="non_stream">{{ t('channels.chat_test_non_stream') }}</el-radio>
            <el-radio label="stream">{{ t('channels.chat_test_stream') }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('channels.chat_test_prompt')" :error="chatTestPromptError">
          <el-input
            v-model="chatTestForm.prompt"
            type="textarea"
            :rows="5"
            :placeholder="t('channels.chat_test_prompt_placeholder')"
            @input="chatTestPromptError = ''"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="chatTestDialogVisible = false">{{ t('channels.cancel') }}</el-button>
        <el-button type="primary" @click="confirmChatTest">{{ t('channels.chat_test_start') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, Check, Loading, Refresh, Setting } from '@element-plus/icons-vue'
import { openRouterApi, setupApi } from '@/api'
import LanguageSwitcher from '@/components/LanguageSwitcher.vue'
import ChannelModelEntry from '@/components/ChannelModelEntry.vue'
import ModelTestResultDialog from '@/components/ModelTestResultDialog.vue'
import { defaultModelEntry } from '@/constants'
import { truncateErrorMessage } from '@/utils/errorMessage.js'
import { createChannelTestManager } from '@/utils/channelTestManager.js'
import { normalizeHttpProxy, isValidHttpProxy } from '@/utils/channelHttpProxy.js'
import {
  customHeadersTemplate,
  customHeadersPlaceholder,
  formatAdvancedSettings,
  parseAdvancedSettingsDraft,
  mergeCustomHeaders,
} from '@/utils/channelAdvancedSettings.js'
import { getOpenRouterModelMatches, applyOpenRouterModelMetadata } from '@/utils/channelModelMetadata.js'
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
  validateSetupHttpProxy,
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
const stepTransitionName = ref('step-forward')
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
    http_proxy: '',
    ...defaultModelEntry(),
  },
  profile: {
    name: 'default',
  },
})

const modelIdError = ref('')
const protocolError = ref('')
const proxyError = ref('')
const advancedSettingsDraft = ref(formatAdvancedSettings(form.channel.advanced_settings))
const advancedSettingsError = ref('')
const advancedSettingsExpanded = ref([])
const detectedModels = ref([])
const selectedDetectedModel = ref('')
const detectingModels = ref(false)
const detectingMetadata = ref(false)
const modelTestState = ref(null)
const modelTestResultDialogVisible = ref(false)
const activeModelTestResultId = ref('setup-model-test')
const chatTestDialogVisible = ref(false)
const chatTestPromptError = ref('')
const chatTestForm = reactive({
  testMode: 'non_stream',
  prompt: '',
})
const channelTestManager = createChannelTestManager()

let openRouterModelsCache = null

const createSetupValidator = validator => (_rule, value, callback) => {
  const error = validator(value)
  if (error) {
    callback(new Error(t('setup.' + error.key, error.params)))
    return
  }
  callback()
}

const validateChannelProxyRule = (_rule, value, callback) => {
  const error = validateSetupHttpProxy(value)
  proxyError.value = error ? t('channels.http_proxy_format_error') : ''
  if (error) {
    callback(new Error(proxyError.value))
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
  http_proxy: [{ validator: validateChannelProxyRule, trigger: 'blur' }],
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

const modelTestResults = computed(() => {
  if (!modelTestState.value) return []

  const modelId = typeof form.channel.model_id === 'string' ? form.channel.model_id.trim() : ''
  return [{
    id: 'setup-model-test',
    label: modelId || `${t('channels.model_entry')} #1`,
    state: modelTestState.value,
  }]
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

function clearChannelTest() {
  channelTestManager.cancel(form.channel)
  modelTestState.value = null
  modelTestResultDialogVisible.value = false
}

function invalidateChannelTests() {
  channelTestManager.invalidate()
  modelTestState.value = null
  modelTestResultDialogVisible.value = false
}

function beginChannelTest(state) {
  const token = channelTestManager.begin(form.channel)
  if (!token) return null
  modelTestState.value = state
  modelTestResultDialogVisible.value = true
  return token
}

function openModelTestResult() {
  if (!modelTestState.value) return
  activeModelTestResultId.value = 'setup-model-test'
  modelTestResultDialogVisible.value = true
}

function resetDetectedModels() {
  selectedDetectedModel.value = ''
  detectedModels.value = []
}

function syncDetectedSelection() {
  const modelId = typeof form.channel.model_id === 'string' ? form.channel.model_id.trim() : ''
  const matched = detectedModels.value.find(model => model.id.trim() === modelId)
  selectedDetectedModel.value = matched?.id || ''
}

function validateChannelHttpProxy() {
  const valid = isValidHttpProxy(form.channel.http_proxy)
  proxyError.value = valid ? '' : t('channels.http_proxy_format_error')
  if (!valid) {
    ElMessage.warning(proxyError.value)
  }
  return valid
}

function validateAndMergeAdvancedSettings() {
  const result = parseAdvancedSettingsDraft(advancedSettingsDraft.value, t)
  advancedSettingsError.value = result.error
  if (result.error) {
    advancedSettingsExpanded.value = [0]
    ElMessage.warning(result.error)
    return null
  }
  return mergeCustomHeaders(form.channel, result.value)
}

function handleModelIdInput() {
  modelIdError.value = ''
  clearChannelTest()
  syncDetectedSelection()
}

function handleProtocolChange() {
  protocolError.value = ''
  clearChannelTest()
}

function handleAdvancedSettingsInput() {
  advancedSettingsError.value = ''
}

function fillCustomHeadersTemplate() {
  clearChannelTest()
  advancedSettingsDraft.value = JSON.stringify(customHeadersTemplate, null, 2)
  advancedSettingsError.value = ''
}

function handleDetectedModelChange(value) {
  if (typeof value !== 'string' || !value.trim()) return
  clearChannelTest()
  form.channel.model_id = value
  modelIdError.value = ''
}

async function detectModelList() {
  if (!form.channel.base_url || !form.channel.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.channel.api_key || !form.channel.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }
  if (!validateChannelHttpProxy()) return

  detectingModels.value = true
  try {
    const res = await setupApi.models({
      api_key: form.channel.api_key || null,
      base_url: form.channel.base_url || null,
      http_proxy: normalizeHttpProxy(form.channel.http_proxy) || null,
    })
    const models = res.data?.data?.models
    detectedModels.value = Array.isArray(models)
      ? models.filter(model => (
        model &&
        typeof model === 'object' &&
        !Array.isArray(model) &&
        typeof model.id === 'string' &&
        model.id.trim()
      ))
      : []
    syncDetectedSelection()
    if (detectedModels.value.length === 0) {
      ElMessage.warning(t('channels.model_list_empty'))
      return
    }
    ElMessage.success(t('channels.model_list_success', { count: detectedModels.value.length }))
  } catch (error) {
    ElMessage.error(error.message || t('channels.model_list_failed'))
  } finally {
    detectingModels.value = false
  }
}

function formatErrorDetail(error) {
  const detail = error?.response?.data
  let message
  if (detail && typeof detail === 'object') {
    message = detail.message || detail.error || JSON.stringify(detail)
  } else {
    message = error?.message || t('channels.chat_test_failed')
  }
  return truncateErrorMessage(message)
}

async function detectModelMetadata() {
  const entry = form.channel
  if (entry.usage !== 'CHAT') {
    return ElMessage.warning(t('channels.model_metadata_chat_only'))
  }
  if (typeof entry.model_id !== 'string' || !entry.model_id.trim()) {
    modelIdError.value = t('channels.model_id_required')
    return ElMessage.warning(t('channels.model_id_required'))
  }

  detectingMetadata.value = true
  try {
    if (!openRouterModelsCache) {
      const res = await openRouterApi.models()
      const models = res.data?.data
      if (!Array.isArray(models) || models.some(model => (
        !model || typeof model !== 'object' || Array.isArray(model)
      ))) {
        throw new Error(t('channels.model_metadata_invalid_response'))
      }
      openRouterModelsCache = models
    }

    const matches = getOpenRouterModelMatches(openRouterModelsCache, entry.model_id)
    if (matches.length === 0) {
      throw new Error(t('channels.model_metadata_not_found', { model: entry.model_id.trim() }))
    }
    if (matches.length > 1) {
      throw new Error(t('channels.model_metadata_ambiguous', { models: matches.map(model => model.id).join(', ') }))
    }

    const { fields: filledFields, model } = applyOpenRouterModelMetadata(entry, matches[0])
    if (filledFields.length === 0) {
      throw new Error(t('channels.model_metadata_no_mappable_fields'))
    }

    ElMessage.success(t('channels.model_metadata_detect_success', {
      model: typeof model.id === 'string' && model.id.trim() ? model.id : entry.model_id.trim(),
      fields: filledFields.map(field => t('channels.' + field)).join(', '),
    }))
  } catch (error) {
    ElMessage.error(error.message || t('channels.model_metadata_detect_failed'))
  } finally {
    detectingMetadata.value = false
  }
}

function openChatTestDialog() {
  chatTestForm.testMode = 'non_stream'
  chatTestForm.prompt = ''
  chatTestPromptError.value = ''
  chatTestDialogVisible.value = true
}

function resetChatTestDialogState() {
  chatTestForm.testMode = 'non_stream'
  chatTestForm.prompt = ''
  chatTestPromptError.value = ''
}

function confirmChatTest() {
  const prompt = chatTestForm.prompt.trim()
  if (!prompt) {
    chatTestPromptError.value = t('channels.chat_test_prompt_required')
    return
  }

  const testMode = chatTestForm.testMode
  chatTestDialogVisible.value = false
  void testChatModel(testMode, prompt)
}

async function testChatModel(testMode, prompt) {
  const entry = form.channel
  if (entry.usage !== 'CHAT') {
    return ElMessage.warning(t('channels.chat_test_chat_only'))
  }
  if (!entry.protocol) {
    protocolError.value = t('channels.model_protocol_required')
    return ElMessage.warning(t('channels.model_protocol_required'))
  }
  if (!form.channel.base_url || !form.channel.base_url.trim()) {
    return ElMessage.warning(t('channels.model_list_base_url_required'))
  }
  if (!form.channel.api_key || !form.channel.api_key.trim()) {
    return ElMessage.warning(t('channels.model_list_api_key_required'))
  }
  if (typeof entry.model_id !== 'string' || !entry.model_id.trim()) {
    modelIdError.value = t('channels.model_id_required')
    return ElMessage.warning(t('channels.model_id_required'))
  }
  if (!validateChannelHttpProxy()) return
  const advancedSettings = validateAndMergeAdvancedSettings()
  if (!advancedSettings) return

  const token = beginChannelTest({
    status: 'running',
    kind: 'CHAT',
    testMode,
    data: null,
    error: '',
  })
  if (!token) return

  try {
    const res = await setupApi.testChat({
      api_key: form.channel.api_key || null,
      base_url: form.channel.base_url || null,
      http_proxy: normalizeHttpProxy(form.channel.http_proxy) || null,
      model_id: entry.model_id.trim(),
      protocol: entry.protocol,
      temperature: entry.temperature,
      top_p: entry.top_p,
      max_tokens: entry.max_tokens || 0,
      test_mode: testMode,
      prompt,
      advanced_settings: advancedSettings,
    }, { signal: token.signal })
    if (!channelTestManager.isCurrent(token)) return
    modelTestState.value = {
      status: 'success',
      kind: 'CHAT',
      testMode,
      data: res.data?.data || {},
      error: '',
    }
  } catch (error) {
    if (channelTestManager.isCurrent(token)) {
      modelTestState.value = {
        status: 'error',
        kind: 'CHAT',
        testMode,
        data: null,
        error: formatErrorDetail(error),
      }
    }
  } finally {
    channelTestManager.finish(token)
  }
}

watch(
  () => [form.channel.base_url, form.channel.api_key, form.channel.http_proxy],
  () => {
    resetDetectedModels()
    invalidateChannelTests()
    proxyError.value = ''
  }
)

async function validateForm(formRef) {
  if (!formRef.value) return false

  try {
    return await formRef.value.validate()
  } catch {
    return false
  }
}

async function validateStep(index) {
  if (index === 1) {
    if (!validateChannelHttpProxy()) return false
    if (!validateAndMergeAdvancedSettings()) return false
  }

  const formRefs = [adminFormRef, channelFormRef, profileFormRef]
  return validateForm(formRefs[index])
}

async function nextStep() {
  if (await validateStep(activeStep.value)) {
    stepTransitionName.value = 'step-forward'
    activeStep.value += 1
  }
}

function previousStep() {
  stepTransitionName.value = 'step-backward'
  activeStep.value -= 1
}

async function completeSetup() {
  for (let index = 0; index < 3; index += 1) {
    if (!await validateStep(index)) {
      activeStep.value = index
      return
    }
  }

  if (!validateAndMergeAdvancedSettings()) {
    activeStep.value = 1
    return
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
    invalidateChannelTests()
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
  invalidateChannelTests()
  chatTestDialogVisible.value = false
  resetChatTestDialogState()
})
</script>

<style lang="scss">
@import '@/assets/css/ChannelFormDialog.scss';
@import '@/assets/css/setup.scss';
</style>
