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
.setup-profile-guide {
  --setup-profile-guide-border: var(--color-border-light, #e4e7ed);
  --setup-profile-guide-text: var(--color-text-main, #303133);
  --setup-profile-guide-muted: var(--color-text-regular, #606266);
  --setup-profile-guide-primary: var(--color-primary, #409eff);

  width: 100%;
  min-width: 0;
  color: var(--setup-profile-guide-text);
  letter-spacing: 0;
}

.setup-profile-guide *,
.setup-profile-guide *::before,
.setup-profile-guide *::after {
  box-sizing: border-box;
  letter-spacing: 0;
}

.setup-profile-guide__steps {
  width: 100%;
  min-width: 0;
  margin: 0 0 24px;
  pointer-events: none;

  :deep(.el-step__main) {
    min-width: 0;
  }

  :deep(.el-step__title) {
    min-width: 0;
    color: var(--setup-profile-guide-muted);
    font-size: 14px;
    line-height: 1.35;
    overflow-wrap: anywhere;
    white-space: normal;
  }

  :deep(.el-step__title.is-process),
  :deep(.el-step__head.is-process) {
    color: var(--setup-profile-guide-primary);
    border-color: var(--setup-profile-guide-primary);
  }

  :deep(.el-step__title.is-success),
  :deep(.el-step__head.is-success) {
    color: var(--color-success, #67c23a);
    border-color: var(--color-success, #67c23a);
  }
}

.setup-profile-guide__content {
  width: 100%;
  min-width: 0;
  min-height: 0;
}

.setup-profile-guide__form,
.setup-profile-guide__section,
.setup-profile-guide__tool-groups,
.setup-profile-guide__tool-group {
  width: 100%;
  min-width: 0;
}

.setup-profile-guide__section-title,
.setup-profile-guide__group-title {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}

.setup-profile-guide__section-title {
  margin-bottom: 20px;
  font-size: 18px;
  font-weight: 650;
  line-height: 1.4;
}

.setup-profile-guide__section-description {
  width: 100%;
  min-width: 0;
  margin-bottom: 20px;
  color: var(--setup-profile-guide-muted);
  font-size: 14px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}

.setup-profile-guide__group-title {
  margin-bottom: 18px;
  font-size: 15px;
  font-weight: 600;
  line-height: 1.4;
}

.setup-profile-guide__tool-group + .setup-profile-guide__tool-group {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid var(--setup-profile-guide-border);
}

.setup-profile-guide__form-grid {
  display: grid;
  min-width: 0;
  align-content: start;
  gap: 0 20px;
}

.setup-profile-guide__form-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.setup-profile-guide__field,
.setup-profile-guide__form-grid > * {
  min-width: 0;
}

.setup-profile-guide__field--wide {
  grid-column: 1 / -1;
}

.setup-profile-guide__hint {
  width: 100%;
  min-width: 0;
  margin-top: 5px;
  color: var(--setup-profile-guide-muted);
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}

.setup-profile-guide__hint :deep(.el-link) {
  max-width: 100%;
  overflow-wrap: anywhere;
  white-space: normal;
}

.setup-profile-guide__control,
.setup-profile-guide__tag-input,
.setup-profile-guide__slider {
  width: 100%;
  min-width: 0;
}

.setup-profile-guide__tag-input :deep(.el-input-group__append) {
  padding: 0;
}

.setup-profile-guide__tag-input :deep(.el-input-group__append .el-button) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 32px;
  margin: 0;
  padding: 0;
}

.setup-profile-guide__tag-list {
  display: flex;
  flex-wrap: wrap;
  min-width: 0;
  margin-top: 10px;
  gap: 8px;
}

.setup-profile-guide__tag {
  max-width: 100%;
}

.setup-profile-guide__tag :deep(.el-tag__content) {
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}

.setup-profile-guide :deep(.el-form-item) {
  min-width: 0;
  margin-bottom: 20px;
}

.setup-profile-guide :deep(.el-form-item__label) {
  height: auto;
  min-width: 0;
  padding-bottom: 8px;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}

.setup-profile-guide :deep(.el-form-item__content),
.setup-profile-guide :deep(.el-input),
.setup-profile-guide :deep(.el-input-number),
.setup-profile-guide :deep(.el-select),
.setup-profile-guide :deep(.el-select__wrapper) {
  width: 100%;
  min-width: 0;
}

.setup-profile-guide :deep(.el-input__inner),
.setup-profile-guide :deep(.el-select__selected-item),
.setup-profile-guide :deep(.el-select__placeholder) {
  min-width: 0;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.setup-profile-guide :deep(.el-slider) {
  width: 100%;
  min-width: 0;
}

.setup-profile-guide :deep(.el-slider__input) {
  flex: 0 0 130px;
  width: 130px;
  max-width: 40%;
}

.step-forward-enter-active,
.step-forward-leave-active,
.step-backward-enter-active,
.step-backward-leave-active {
  transition: transform 280ms ease, opacity 280ms ease;
}

.step-forward-enter-from {
  opacity: 0;
  transform: translateX(24px);
}

.step-forward-leave-to {
  opacity: 0;
  transform: translateX(-24px);
}

.step-backward-enter-from {
  opacity: 0;
  transform: translateX(-24px);
}

.step-backward-leave-to {
  opacity: 0;
  transform: translateX(24px);
}

@media (max-width: 720px) {
  .setup-profile-guide__steps {
    margin-bottom: 20px;

    :deep(.el-step__title) {
      font-size: 12px;
    }
  }

  .setup-profile-guide__form-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
  }

  .setup-profile-guide__field--wide {
    grid-column: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .step-forward-enter-active,
  .step-forward-leave-active,
  .step-backward-enter-active,
  .step-backward-leave-active {
    transition: none;
  }

  .step-forward-enter-from,
  .step-forward-leave-to,
  .step-backward-enter-from,
  .step-backward-leave-to {
    transform: none;
  }
}
</style>
