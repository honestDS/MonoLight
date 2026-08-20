<template>
  <div class="setup-profile-guide">
    <el-steps
      v-if="showSteps"
      :active="activeSection"
      finish-status="success"
      process-status="process"
      align-center
      class="setup-profile-guide__steps"
    >
      <el-step :title="$t('profiles.base_settings')" />
      <el-step :title="$t('profiles.security_settings')" />
      <el-step :title="$t('profiles.tool_settings')" />
    </el-steps>

    <Transition :name="transitionName" mode="out-in">
      <div :key="activeSection" class="setup-profile-guide__content">
        <el-form :model="form" label-position="top" class="setup-profile-guide__form" @submit.prevent>
          <section v-if="activeSection === 0" class="setup-profile-guide__section">
            <h2 class="setup-profile-guide__section-title">
              {{ $t('profiles.base_settings') }}
            </h2>

            <div class="setup-profile-guide__form-grid">
              <el-form-item
                class="setup-profile-guide__field setup-profile-guide__field--wide"
                :label="$t('setup.default_prompt')"
              >
                <el-input
                  v-model="form.prompt.content"
                  type="textarea"
                  :rows="8"
                  :placeholder="$t('setup.default_prompt_placeholder')"
                  class="setup-profile-guide__control"
                />
                <div class="setup-profile-guide__hint">
                  {{ $t('setup.default_prompt_hint') }}
                </div>
              </el-form-item>

              <el-form-item
                class="setup-profile-guide__field setup-profile-guide__field--wide"
                :label="$t('profiles.context_summary_threshold')"
              >
                <el-select v-model="form.configs.other.context_summary_threshold_percent" class="setup-profile-guide__control">
                  <el-option
                    v-for="percent in contextSummaryThresholdOptions"
                    :key="percent"
                    :label="`${percent}%`"
                    :value="percent"
                  />
                </el-select>
                <div class="setup-profile-guide__hint">
                  {{ $t('profiles.context_summary_threshold_hint') }}
                </div>
              </el-form-item>
            </div>
          </section>

          <section v-else-if="activeSection === 1" class="setup-profile-guide__section">
            <h2 class="setup-profile-guide__section-title">
              {{ $t('profiles.security_settings') }}
            </h2>

            <div class="setup-profile-guide__section-description">
              {{ $t('setup.audit_guide_description') }}
            </div>

            <div class="setup-profile-guide__form-grid">
              <el-form-item class="setup-profile-guide__field" :label="$t('profiles.audit_model_id')">
                <el-select
                  v-model="auditModelKey"
                  :placeholder="$t('profiles.audit_model_hint')"
                  clearable
                  filterable
                  class="setup-profile-guide__control"
                >
                  <el-option
                    v-for="item in auditModelOptions"
                    :key="item.key"
                    :label="item.label"
                    :value="item.key"
                  />
                </el-select>
              </el-form-item>

              <el-form-item class="setup-profile-guide__field" :label="$t('profiles.audit_report_language')">
                <el-select v-model="form.configs.security.audit_report_language" class="setup-profile-guide__control">
                  <el-option
                    v-for="locale in localeOptions"
                    :key="locale.value"
                    :label="locale.label"
                    :value="locale.value"
                  />
                </el-select>
                <div class="setup-profile-guide__hint">
                  {{ $t('profiles.audit_report_language_hint') }}
                </div>
              </el-form-item>

              <el-form-item class="setup-profile-guide__field" :label="$t('profiles.secondary_confirmation')">
                <el-switch
                  :model-value="form.configs.security.audit_threshold > 0"
                  @update:model-value="setAuditConfirmationEnabled"
                />
                <div class="setup-profile-guide__hint">
                  {{ $t('profiles.secondary_confirmation_hint') }}
                </div>
              </el-form-item>

              <el-form-item
                v-if="form.configs.security.audit_threshold > 0"
                class="setup-profile-guide__field"
                :label="$t('profiles.audit_threshold')"
              >
                <el-slider
                  v-model="form.configs.security.audit_threshold"
                  :min="1"
                  :max="7"
                  show-stops
                  show-input
                  class="setup-profile-guide__slider"
                />
                <div class="setup-profile-guide__hint">
                  {{ $t('profiles.audit_threshold_hint') }}
                </div>
              </el-form-item>

              <el-form-item
                class="setup-profile-guide__field setup-profile-guide__field--wide"
                :label="$t('profiles.audit_confirmation_timeout_seconds')"
              >
                <el-input-number
                  v-model="form.configs.security.audit_confirmation_timeout_seconds"
                  :min="1"
                  :max="86400"
                  :step="1"
                  controls-position="right"
                  class="setup-profile-guide__control"
                />
                <div class="setup-profile-guide__hint">
                  {{ $t('profiles.audit_confirmation_timeout_seconds_hint') }}
                </div>
              </el-form-item>
            </div>
          </section>

          <section v-else class="setup-profile-guide__section">
            <h2 class="setup-profile-guide__section-title">
              {{ $t('profiles.tool_settings') }}
            </h2>

            <div class="setup-profile-guide__tool-groups">
              <section class="setup-profile-guide__tool-group">
                <h3 class="setup-profile-guide__group-title">
                  {{ $t('profiles.common_tool_config') }}
                </h3>

                <el-form-item class="setup-profile-guide__field setup-profile-guide__field--wide" :label="$t('profiles.allowed_operation_dirs')">
                  <div class="setup-profile-guide__tag-input">
                    <el-input
                      v-model="allowedOperationDirDraft"
                      :placeholder="$t('profiles.allowed_operation_dirs_placeholder')"
                      class="setup-profile-guide__control"
                      @keyup.enter="addAllowedOperationDir"
                    >
                      <template #append>
                        <el-button
                          :title="$t('profiles.add')"
                          :aria-label="$t('profiles.add')"
                          @click="addAllowedOperationDir"
                        >
                          <el-icon><Plus /></el-icon>
                        </el-button>
                      </template>
                    </el-input>
                    <div v-if="form.configs.tool.allowed_operation_dirs.length" class="setup-profile-guide__tag-list">
                      <el-tag
                        v-for="item in form.configs.tool.allowed_operation_dirs"
                        :key="item"
                        closable
                        class="setup-profile-guide__tag"
                        @close="removeAllowedOperationDir(item)"
                      >
                        {{ item }}
                      </el-tag>
                    </div>
                  </div>
                  <div class="setup-profile-guide__hint">
                    {{ $t('profiles.allowed_operation_dirs_hint') }}
                  </div>
                </el-form-item>
              </section>

              <section class="setup-profile-guide__tool-group">
                <h3 class="setup-profile-guide__group-title">
                  {{ $t('profiles.tool_visibility_config') }}
                </h3>

                <el-form-item class="setup-profile-guide__field setup-profile-guide__field--wide" :label="$t('profiles.enabled_tools')">
                  <el-select
                    v-model="form.configs.tool.enabled_tools"
                    multiple
                    :placeholder="$t('profiles.enabled_tools_placeholder')"
                    class="setup-profile-guide__control"
                  >
                    <el-option
                      v-for="item in toolOptions"
                      :key="item.value"
                      :label="item.label"
                      :value="item.value"
                    />
                  </el-select>
                  <div class="setup-profile-guide__hint">
                    {{ $t('profiles.enabled_tools_hint') }}
                  </div>
                </el-form-item>
              </section>

              <section class="setup-profile-guide__tool-group">
                <h3 class="setup-profile-guide__group-title">
                  {{ $t('profiles.firecrawl_config') }}
                </h3>

                <el-form-item class="setup-profile-guide__field setup-profile-guide__field--wide" :label="$t('profiles.api_key')">
                  <el-input
                    v-model="form.configs.tool.firecrawl_api_key"
                    :placeholder="$t('profiles.firecrawl_key_placeholder')"
                    show-password
                    class="setup-profile-guide__control"
                  />
                  <div class="setup-profile-guide__hint">
                    {{ $t('profiles.firecrawl_hint_1') }}
                    <el-link
                      type="primary"
                      href="https://www.firecrawl.dev/"
                      target="_blank"
                      underline="never"
                    >
                      {{ $t('profiles.firecrawl_hint_2') }}
                    </el-link>
                    {{ $t('profiles.firecrawl_hint_3') }}
                  </div>
                </el-form-item>
              </section>
            </div>
          </section>
        </el-form>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { computed, ref, toRefs } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { SUPPORT_LOCALES } from '@/i18n'

const props = defineProps({
  activeSection: {
    type: Number,
    required: true,
    validator: value => Number.isInteger(value) && value >= 0 && value <= 2
  },
  transitionName: {
    type: String,
    default: 'step-forward'
  },
  showSteps: {
    type: Boolean,
    default: true
  },
  form: {
    type: Object,
    required: true
  },
  auditModelOptions: {
    type: Array,
    required: true
  },
  toolOptions: {
    type: Array,
    required: true
  }
})

const { activeSection, transitionName, showSteps, form, auditModelOptions, toolOptions } = toRefs(props)
const localeOptions = SUPPORT_LOCALES
const contextSummaryThresholdOptions = [50, 60, 70, 80, 90]
const allowedOperationDirDraft = ref('')

const auditModelKey = computed({
  get() {
    const security = form.value.configs.security
    if (!security.audit_channel_id || !security.audit_model_id) return null
    return `${security.audit_channel_id}::${security.audit_model_id}`
  },
  set(key) {
    const security = form.value.configs.security
    if (!key) {
      security.audit_channel_id = null
      security.audit_model_id = null
      return
    }

    const option = auditModelOptions.value.find(item => item.key === key)
    if (!option) return
    security.audit_channel_id = option.channel_id
    security.audit_model_id = option.model_id
  }
})

const normalizeDirectory = value => String(value ?? '').trim()

const addUniqueValue = (list, rawValue, normalize) => {
  const value = normalize(rawValue)
  if (!value || list.some(item => normalize(item) === value)) return false
  list.push(value)
  return true
}

const addAllowedOperationDir = () => {
  if (allowedOperationDirDraft.value.trim()) {
    addUniqueValue(
      form.value.configs.tool.allowed_operation_dirs,
      allowedOperationDirDraft.value,
      normalizeDirectory
    )
    allowedOperationDirDraft.value = ''
  }
}

const removeAllowedOperationDir = value => {
  const list = form.value.configs.tool.allowed_operation_dirs
  const index = list.indexOf(value)
  if (index >= 0) list.splice(index, 1)
}

const setAuditConfirmationEnabled = enabled => {
  form.value.configs.security.audit_threshold = enabled ? 5 : 0
}

const commitPendingInputs = () => {
  addAllowedOperationDir()
}

const discardPendingInputs = () => {
  allowedOperationDirDraft.value = ''
}

defineExpose({ commitPendingInputs, discardPendingInputs })
</script>

<style scoped lang="scss">
@import "@/assets/css/SetupProfileGuide.scss";
</style>
