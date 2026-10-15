<template>
  <div class="setup-page">
    <main class="setup-shell">
      <div class="setup-language">
        <LanguageSwitcher />
      </div>

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
        v-else-if="setupStatus.phase === 'ready' && (setupStatus.required || profileGuideActive)"
        class="setup-content"
      >
        <el-steps v-if="!profileGuideStarted" :active="activeStep" finish-status="success" class="setup-steps" align-center>
          <el-step :title="t('setup.step_admin')" />
          <el-step :title="t('setup.step_channel')" />
          <el-step :title="t('setup.step_profile')" />
        </el-steps>
        <el-steps v-else :active="profileGuideStep" finish-status="success" class="setup-steps" align-center>
          <el-step :title="t('profiles.base_settings')" />
          <el-step :title="t('profiles.security_settings')" />
          <el-step :title="t('profiles.tool_settings')" />
        </el-steps>

        <div class="setup-section">
          <div v-if="profileGuideActive && !profileGuideStarted" class="setup-profile-guide-entry">
            <h2>{{ t('setup.profile_title') }}</h2>
            <p class="setup-profile-guide-entry-description">{{ t('setup.profile_guide_description') }}</p>
            <div class="setup-profile-guide-entry-actions">
              <el-button @click="finishProfileGuide">
                <span>{{ t('setup.skip_step') }}</span>
              </el-button>
              <el-button type="primary" @click="startProfileGuide">
                <span>{{ t('setup.continue_configuration') }}</span>
                <el-icon><ArrowRight /></el-icon>
              </el-button>
            </div>
          </div>

          <template v-else>
            <div v-if="activeStep < 2" class="setup-section-header">
              <h2 v-if="activeStep === 0">{{ t('setup.admin_title') }}</h2>
              <h2 v-else>{{ t('setup.channel_title') }}</h2>
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
                            v-model="selectedDetectedModels"
                            multiple
                            collapse-tags
                            collapse-tags-tooltip
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
                      v-for="(entry, idx) in form.channel.model_ids"
                      :key="getModelEntryState(entry).id"
                      :entry="entry"
                      :index="idx"
                      :model-usages="['CHAT']"
                      :model-protocols="SETUP_PROTOCOLS"
                      :reasoning-effort-options="getModelEntryState(entry).reasoningEffortOptions"
                      :model-id-error="getModelEntryState(entry).modelIdError"
                      :protocol-error="getModelEntryState(entry).protocolError"
                      :advanced-settings-draft="getModelEntryState(entry).advancedSettingsDraft"
                      :advanced-settings-error="getModelEntryState(entry).advancedSettingsError"
                      :advanced-settings-expanded="getModelEntryState(entry).advancedSettingsExpanded ? [idx] : []"
                      :custom-headers-placeholder="customHeadersPlaceholder"
                      :testing="getModelEntryState(entry).testState?.status === 'running'"
                      :test-state="getModelEntryState(entry).testState"
                      :detecting-metadata="detectingMetadataEntry === entry"
                      :metadata-detection-disabled="detectingMetadataEntry !== null && detectingMetadataEntry !== entry"
                      :show-remove="form.channel.model_ids.length > 1"
                      :show-enabled="false"
                      @test-chat="openChatTestDialog(entry)"
                      @detect-metadata="detectModelMetadata(entry)"
                      @model-id-input="handleModelIdInput(entry)"
                      @protocol-change="handleProtocolChange(entry)"
                      @advanced-settings-input="handleAdvancedSettingsInput(entry)"
                      @fill-headers-template="fillCustomHeadersTemplate(entry)"
                      @test-config-change="clearChannelTest(entry)"
                      @view-test-result="openModelTestResult(entry)"
                      @remove="removeModelEntry(entry)"
                      @update:advanced-settings-draft="value => getModelEntryState(entry).advancedSettingsDraft = value"
                      @update:advanced-settings-expanded="value => getModelEntryState(entry).advancedSettingsExpanded = Array.isArray(value) && value.length > 0"
                    />

                    <el-button type="primary" :icon="Plus" @click="addModelEntry">
                      {{ t('channels.add_model') }}
                    </el-button>
                  </el-form>

                  <div v-if="activeStep === 2 && profileGuideLoading" class="setup-status">
                    <el-icon class="is-loading"><Loading /></el-icon>
                    <span>{{ t('setup.status_checking') }}</span>
                  </div>

                  <div v-else-if="activeStep === 2 && profileGuideError" class="setup-status">
                    <el-result
                      icon="error"
                      :title="t('setup.guide_load_failed')"
                      :sub-title="profileGuideError"
                    >
                      <template #extra>
                        <el-button type="primary" @click="loadProfileGuide">
                          <el-icon><Refresh /></el-icon>
                          <span>{{ t('setup.status_retry') }}</span>
                        </el-button>
                      </template>
                    </el-result>
                  </div>

                  <SetupProfileGuide
                    v-else-if="activeStep === 2 && profileGuideReady"
                    ref="profileGuideRef"
                    :active-section="profileGuideStep"
                    :transition-name="profileGuideTransitionName"
                    :form="profileGuideForm"
                    :audit-model-options="profileGuideAuditModelOptions"
                    :tool-options="profileGuideToolOptions"
                    :show-steps="false"
                  />

                  <p v-if="completionError && activeStep < 2" class="setup-error">{{ completionErrorMessage }}</p>

                  <div class="setup-actions">
                    <template v-if="activeStep < 2">
                      <el-button v-if="activeStep > 0" @click="previousStep">
                        <el-icon><ArrowLeft /></el-icon>
                        <span>{{ t('setup.back') }}</span>
                      </el-button>
                      <span v-else class="setup-actions-spacer" />

                      <el-button type="primary" :loading="submitting" @click="nextStep">
                        <span>{{ t('setup.next') }}</span>
                        <el-icon><ArrowRight /></el-icon>
                      </el-button>
                    </template>
                    <template v-else>
                      <el-button
                        v-if="profileGuideStep > 0"
                        :disabled="!profileGuideReady || profileGuideSaving"
                        @click="previousProfileGuideStep"
                      >
                        <el-icon><ArrowLeft /></el-icon>
                        <span>{{ t('setup.back') }}</span>
                      </el-button>
                      <span v-else class="setup-actions-spacer" />

                      <div class="setup-actions-right">
                        <el-button
                          :disabled="!profileGuideReady || profileGuideSaving"
                          @click="skipProfileGuideStep"
                        >
                          <span>{{ t(profileGuideStep === 2 ? 'setup.skip_and_finish' : 'setup.skip_step') }}</span>
                          <el-icon><ArrowRight /></el-icon>
                        </el-button>
                        <el-button
                          type="primary"
                          :loading="profileGuideSaving"
                          :disabled="!profileGuideReady || profileGuideSaving"
                          @click="saveProfileGuideStep"
                        >
                          <el-icon><Check /></el-icon>
                          <span>{{ t(profileGuideStep === 2 ? 'setup.save_and_finish' : 'setup.save_and_continue') }}</span>
                        </el-button>
                      </div>
                    </template>
                  </div>
                </div>
              </Transition>
            </div>
          </template>
        </div>
      </section>

      <section
        v-else-if="setupStatus.phase === 'ready' && setupStatus.required === false && !profileGuideActive"
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
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ArrowRight, Check, Loading, Plus, Refresh, Setting } from '@element-plus/icons-vue'
import { openRouterApi, profileApi, promptApi, setupApi } from '@/api'
import LanguageSwitcher from '@/components/LanguageSwitcher.vue'
import ChannelModelEntry from '@/components/ChannelModelEntry.vue'
import ModelTestResultDialog from '@/components/ModelTestResultDialog.vue'
import SetupProfileGuide from '@/components/SetupProfileGuide.vue'
import { defaultModelEntry } from '@/constants'
import {
  buildSetupRequest,
  cloneSetupProfileConfigs,
  readSetupTokenData,
  readSetupProfileGuideData,
} from '@/utils/setupForm'
import { useSetupChannelConfiguration } from '@/composables/setup/useSetupChannelConfiguration.js'
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
const profileGuideActive = ref(false)
const profileGuideStarted = ref(false)
const profileGuideStep = ref(0)
const profileGuideTransitionName = ref('step-forward')
const profileGuideLoading = ref(false)
const profileGuideReady = ref(false)
const profileGuideError = ref(null)
const profileGuideSaving = ref(false)
const profileGuideCommittedConfigs = ref(null)
const profileGuideCommittedPromptContent = ref(null)
const profileGuideRef = ref()

const profileGuideResource = reactive({
  profile_id: null,
  channel_id: null,
  channel_name: '',
  model_ids: [],
})

const profileGuideForm = reactive({
  configs: null,
  prompt: null,
})

const profileGuideToolOptions = ref([])

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
    model_ids: [defaultModelEntry()],
  },
  profile: {
    name: 'default',
  },
})

const {
  adminFormRef,
  channelFormRef,
  adminRules,
  channelRules,
  SETUP_PROTOCOLS,
  customHeadersPlaceholder,
  proxyError,
  modelEntryStates,
  detectedModels,
  selectedDetectedModels,
  detectingModels,
  detectingMetadataEntry,
  modelTestResultDialogVisible,
  activeModelTestResultId,
  chatTestDialogVisible,
  chatTestPromptError,
  chatTestForm,
  modelTestResults,
  getModelEntryState,
  clearChannelTest,
  invalidateChannelTests,
  openModelTestResult,
  resetChatTestDialogState,
  handleDetectedModelChange,
  addModelEntry,
  removeModelEntry,
  detectModelList,
  detectModelMetadata,
  openChatTestDialog,
  confirmChatTest,
  handleModelIdInput,
  handleProtocolChange,
  handleAdvancedSettingsInput,
  fillCustomHeadersTemplate,
  validateStep,
  testChatModel,
} = useSetupChannelConfiguration({ form, t, setupApi, openRouterApi, message: ElMessage })

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

const profileGuideAuditModelOptions = computed(() => {
  const { channel_id, channel_name, model_ids } = profileGuideResource
  if (channel_id === null || channel_id === undefined || !channel_name || !Array.isArray(model_ids)) return []

  return model_ids.filter(Boolean).map(model_id => ({
    key: `${channel_id}::${model_id}`,
    label: `${channel_name} / ${model_id}`,
    channel_id,
    model_id,
  }))
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

async function nextStep() {
  if (activeStep.value === 0 && await validateStep(0)) {
    stepTransitionName.value = 'step-forward'
    activeStep.value = 1
    return
  }

  if (activeStep.value === 1 && await validateStep(1)) {
    await completeSetup()
  }
}

function previousStep() {
  stepTransitionName.value = 'step-backward'
  activeStep.value -= 1
}

async function completeSetup() {
  for (let index = 0; index < 2; index += 1) {
    if (!await validateStep(index)) {
      activeStep.value = index
      return
    }
  }

  submitting.value = true
  completionError.value = null
  invalidTokenResponse.value = false

  try {
    const request = buildSetupRequest(form)
    const response = await setupApi.complete(request)
    const tokenData = readSetupTokenData(response)
    const { access_token, token_type, profile_id, channel_id } = tokenData || {}

    if (
      !tokenData ||
      typeof access_token !== 'string' ||
      !access_token ||
      typeof token_type !== 'string' ||
      !token_type ||
      !Number.isInteger(profile_id) ||
      profile_id <= 0 ||
      !Number.isInteger(channel_id) ||
      channel_id <= 0
    ) {
      completionError.value = { code: null, message: '' }
      invalidTokenResponse.value = true
      ElMessage.error(t('setup.invalid_token_response'))
      return
    }

    localStorage.setItem('token', access_token)
    profileGuideResource.profile_id = profile_id
    profileGuideResource.channel_id = channel_id
    profileGuideResource.channel_name = request.channel.name
    profileGuideResource.model_ids = request.channel.model_ids.map(model => model.model_id)
    profileGuideActive.value = true
    setupStatusState.setReady(false)
    invalidateChannelTests()
    form.admin.password = ''
    form.admin.password_confirm = ''
    form.channel.api_key = ''
    activeStep.value = 2
    ElMessage.success(t('setup.complete_success'))
  } catch (error) {
    completionError.value = normalizeSetupError(error)
    ElMessage.error(completionErrorMessage.value)
  } finally {
    submitting.value = false
  }
}

function startProfileGuide() {
  if (!profileGuideActive.value || profileGuideStarted.value) return

  profileGuideStarted.value = true
  void loadProfileGuide()
}

async function loadProfileGuide() {
  profileGuideLoading.value = true
  profileGuideReady.value = false
  profileGuideError.value = null
  profileGuideForm.configs = null
  profileGuideForm.prompt = null
  profileGuideCommittedConfigs.value = null
  profileGuideCommittedPromptContent.value = null
  profileGuideToolOptions.value = []

  try {
    const [profileResponse, promptResponse] = await Promise.all([
      profileApi.list({ page: 1, size: 1000 }),
      promptApi.list({ page: 1, size: 1000 }),
    ])
    const guideData = readSetupProfileGuideData(
      profileResponse,
      promptResponse,
      profileGuideResource.profile_id,
    )
    if (!guideData) {
      throw new Error(t('setup.guide_load_failed'))
    }

    profileGuideForm.configs = guideData.configs
    profileGuideForm.prompt = guideData.prompt
    profileGuideCommittedConfigs.value = cloneSetupProfileConfigs(guideData.configs)
    profileGuideCommittedPromptContent.value = guideData.prompt.content
    profileGuideToolOptions.value = guideData.toolOptions
    profileGuideReady.value = true
  } catch (error) {
    profileGuideError.value = error?.message || t('setup.guide_load_failed')
  } finally {
    profileGuideLoading.value = false
  }
}

function advanceProfileGuideStep() {
  if (profileGuideStep.value >= 2) {
    finishProfileGuide()
    return
  }

  profileGuideTransitionName.value = 'step-forward'
  profileGuideStep.value += 1
}

function previousProfileGuideStep() {
  if (profileGuideStep.value <= 0 || profileGuideSaving.value) return

  restoreProfileGuideDraft()
  profileGuideTransitionName.value = 'step-backward'
  profileGuideStep.value -= 1
}

function restoreProfileGuideDraft() {
  profileGuideRef.value?.discardPendingInputs()
  const configs = cloneSetupProfileConfigs(profileGuideCommittedConfigs.value)
  if (configs) {
    profileGuideForm.configs = configs
  }
  if (profileGuideForm.prompt && typeof profileGuideCommittedPromptContent.value === 'string') {
    profileGuideForm.prompt.content = profileGuideCommittedPromptContent.value
  }
}

async function saveProfileGuideStep() {
  if (!profileGuideReady.value || profileGuideSaving.value) return

  profileGuideRef.value?.commitPendingInputs()
  const prompt = profileGuideForm.prompt
  if (
    !prompt ||
    !Number.isInteger(prompt.id) ||
    prompt.id <= 0 ||
    typeof prompt.name !== 'string' ||
    !prompt.name.trim() ||
    typeof prompt.content !== 'string'
  ) {
    ElMessage.error(t('profiles.submit_failed'))
    return
  }

  const shouldUpdatePrompt = (
    profileGuideStep.value === 0 &&
    prompt.content !== profileGuideCommittedPromptContent.value
  )
  if (shouldUpdatePrompt && prompt.content === '') {
    ElMessage.warning(t('setup.default_prompt_required'))
    return
  }

  const configs = cloneSetupProfileConfigs(profileGuideForm.configs)
  if (!configs) {
    ElMessage.error(t('profiles.submit_failed'))
    return
  }

  profileGuideSaving.value = true
  try {
    const response = await profileApi.update(profileGuideResource.profile_id, { configs })
    const updatedConfigs = cloneSetupProfileConfigs(response?.data?.data?.configs)
    if (!updatedConfigs) {
      throw new Error(t('profiles.submit_failed'))
    }

    profileGuideForm.configs = updatedConfigs
    profileGuideCommittedConfigs.value = cloneSetupProfileConfigs(updatedConfigs)

    if (shouldUpdatePrompt) {
      const promptResponse = await promptApi.update(prompt.id, { content: prompt.content })
      const updatedPrompt = promptResponse?.data?.data
      if (
        !updatedPrompt ||
        updatedPrompt.id !== prompt.id ||
        typeof updatedPrompt.name !== 'string' ||
        !updatedPrompt.name.trim() ||
        typeof updatedPrompt.content !== 'string'
      ) {
        throw new Error(t('prompts.submit_failed'))
      }

      profileGuideForm.prompt = {
        id: updatedPrompt.id,
        name: updatedPrompt.name,
        content: updatedPrompt.content,
      }
      profileGuideCommittedPromptContent.value = updatedPrompt.content
    }

    ElMessage.success(t('profiles.save_success'))
    advanceProfileGuideStep()
  } catch (error) {
    ElMessage.error(error?.message || t('profiles.submit_failed'))
  } finally {
    profileGuideSaving.value = false
  }
}

function skipProfileGuideStep() {
  if (!profileGuideReady.value || profileGuideSaving.value) return
  restoreProfileGuideDraft()
  advanceProfileGuideStep()
}

function finishProfileGuide() {
  profileGuideActive.value = false
  router.replace(HOME_PATH)
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
@import '@/assets/css/SetupView.scss';
</style>
