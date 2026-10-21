<script setup>
import { computed, onBeforeUnmount, onMounted, toRef, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useKnowledgeEntryEditor } from '@/composables/knowledgeBase/useKnowledgeEntryEditor.js'
import { getKnowledgeBaseProfileLabel } from '@/utils/knowledgeBaseManagement.js'

const props = defineProps({
  knowledgeBase: {
    type: Object,
    required: true
  },
  scope: {
    type: Object,
    required: true
  }
})

const emit = defineEmits(['saved'])
const { t } = useI18n()
const selectedKb = toRef(props, 'knowledgeBase')

const {
  visible,
  mode,
  formRef,
  form,
  sourceFilename,
  editingId,
  loading,
  previewing,
  submitting,
  error,
  dirty,
  isManaged,
  rules,
  openCreate,
  openEdit,
  previewFile,
  submit,
  requestClose,
  canLeave
} = useKnowledgeEntryEditor({
  t,
  selectedKb,
  scope: props.scope,
  onSaved: result => emit('saved', result)
})

const busy = computed(() => loading.value || previewing.value || submitting.value)
const uploadRef = ref(null)

const handleFileChange = (uploadFile) => {
  const preview = previewFile(uploadFile)
  uploadRef.value?.clearFiles()
  return preview
}

const handleBeforeClose = async (done) => {
  if (await requestClose()) done()
}

const handleBeforeUnload = (event) => {
  if (!dirty.value && !submitting.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => window.addEventListener('beforeunload', handleBeforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', handleBeforeUnload))

defineExpose({ openCreate, openEdit, canLeave, requestClose })
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="t(editingId === null ? 'knowledgeBase.entries_add' : 'knowledgeBase.entries_edit')"
    class="standard-dialog dialog-with-scroll-body knowledge-entry-editor-dialog"
    modal-class="knowledge-entry-editor"
    width="840px"
    center
    align-center
    :show-close="!submitting"
    :before-close="handleBeforeClose"
  >
    <section class="knowledge-entry-target" :aria-label="t('knowledgeBase.target_kb')">
      <div class="knowledge-entry-target__identity">
        <el-icon aria-hidden="true"><Collection /></el-icon>
        <div class="knowledge-entry-target__copy">
          <span class="knowledge-entry-target__label">{{ t('knowledgeBase.target_kb') }}</span>
          <strong class="knowledge-entry-target__name">{{ selectedKb?.name || '-' }}</strong>
        </div>
      </div>
      <div class="knowledge-entry-target__details">
        <el-tag type="info" effect="plain">
          {{ t(selectedKb?.knowledge_base_type === 'llm_managed' ? 'knowledgeBase.type_managed' : 'knowledgeBase.type_user') }}
        </el-tag>
        <div class="knowledge-entry-target__meta">
          <span>{{ t('knowledgeBase.owner_username') }}: {{ selectedKb?.username || t('knowledgeBase.owner_unknown') }}</span>
          <span>{{ t('knowledgeBase.owner_profiles') }}: {{ getKnowledgeBaseProfileLabel(selectedKb) }}</span>
        </div>
      </div>
    </section>

    <el-radio-group
      v-if="editingId === null"
      v-model="mode"
      :disabled="busy"
      :aria-label="t('knowledgeBase.entries_add')"
      class="knowledge-entry-mode"
    >
      <el-radio-button label="manual">
        <el-icon aria-hidden="true"><EditPen /></el-icon>
        {{ t('knowledgeBase.entries_manual') }}
      </el-radio-button>
      <el-radio-button label="file">
        <el-icon aria-hidden="true"><DocumentAdd /></el-icon>
        {{ t('knowledgeBase.entries_file') }}
      </el-radio-button>
    </el-radio-group>

    <div v-if="editingId === null && mode === 'file'" class="knowledge-entry-file-action">
      <div class="knowledge-entry-file-action__copy">
        <el-icon aria-hidden="true"><DocumentAdd /></el-icon>
        <p class="knowledge-entry-help">{{ t('knowledgeBase.entries_file_hint') }}</p>
      </div>
      <el-upload
        ref="uploadRef"
        :auto-upload="false"
        :show-file-list="false"
        :limit="1"
        accept=".txt,.md,.csv,.json,.yaml,.yml,.log"
        :disabled="busy"
        @change="handleFileChange"
      >
        <el-button :disabled="busy">
          <el-icon aria-hidden="true"><FolderOpened /></el-icon>
          {{ t('knowledgeBase.select_file') }}
        </el-button>
      </el-upload>
    </div>

    <div v-if="previewing" class="knowledge-entry-help" role="status" aria-live="polite">
      {{ t('knowledgeBase.entries_previewing') }}
    </div>

    <div v-if="sourceFilename" class="knowledge-entry-source knowledge-entry-file-name">
      {{ t('knowledgeBase.entries_source_file', { name: sourceFilename }) }}
    </div>

    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      role="alert"
    />

    <el-form
      ref="formRef"
      class="knowledge-entry-form"
      :model="form"
      :rules="rules"
      label-position="top"
      :disabled="busy"
      @submit.prevent="submit"
    >
      <el-form-item prop="name" for="knowledge-entry-name" :label="t('knowledgeBase.entries_name')">
        <el-input
          id="knowledge-entry-name"
          v-model="form.name"
          maxlength="255"
          show-word-limit
          :disabled="busy"
          :placeholder="t('knowledgeBase.entries_name_required')"
          :aria-describedby="isManaged ? 'knowledge-entry-name-hint' : undefined"
        />
        <div v-if="isManaged" id="knowledge-entry-name-hint" class="knowledge-entry-help">
          {{ t('knowledgeBase.entries_name_hint_managed') }}
        </div>
      </el-form-item>

      <el-form-item prop="content" for="knowledge-entry-content" :label="t('knowledgeBase.entries_content')">
        <el-input
          id="knowledge-entry-content"
          v-model="form.content"
          type="textarea"
          :rows="8"
          :autosize="{ minRows: 8, maxRows: 16 }"
          :disabled="busy"
          :placeholder="t('knowledgeBase.entries_content_required')"
        />
      </el-form-item>

      <el-form-item v-if="isManaged" class="knowledge-entry-maintenance">
        <div class="knowledge-entry-maintenance__copy">
          <label for="knowledge-entry-maintainable" class="knowledge-entry-maintenance__label">
            {{ t('knowledgeBase.entries_maintainable') }}
          </label>
          <p id="knowledge-entry-maintainable-hint" class="knowledge-entry-help">
            {{ t('knowledgeBase.managed_llm_maintainable_hint') }}
          </p>
        </div>
        <el-switch
          id="knowledge-entry-maintainable"
          v-model="form.llm_maintainable"
          :disabled="busy"
          :aria-label="t('knowledgeBase.entries_maintainable')"
          aria-describedby="knowledge-entry-maintainable-hint"
        />
      </el-form-item>

      <details v-if="!isManaged" class="knowledge-entry-advanced">
        <summary>{{ t('knowledgeBase.entries_advanced') }}</summary>
        <fieldset :disabled="busy" :aria-label="t('knowledgeBase.entries_advanced')">
          <el-form-item prop="chunk_size" :label="t('knowledgeBase.chunk_size')">
            <el-input-number v-model="form.chunk_size" :min="100" :max="20000" :step="100" class="full-width-input" />
            <div class="knowledge-entry-help">{{ t('knowledgeBase.chunk_size_hint') }}</div>
          </el-form-item>
          <el-form-item prop="chunk_overlap" :label="t('knowledgeBase.chunk_overlap')">
            <el-input-number v-model="form.chunk_overlap" :min="0" :max="5000" :step="50" class="full-width-input" />
            <div class="knowledge-entry-help">{{ t('knowledgeBase.chunk_overlap_hint') }}</div>
          </el-form-item>
          <el-form-item prop="batch_size" :label="t('knowledgeBase.batch_size')">
            <el-input-number v-model="form.batch_size" :min="1" :max="256" :step="1" class="full-width-input" />
            <div class="knowledge-entry-help">{{ t('knowledgeBase.batch_size_hint') }}</div>
          </el-form-item>
        </fieldset>
      </details>
    </el-form>

    <template #footer>
      <div class="knowledge-entry-editor-footer">
        <p v-if="submitting" class="knowledge-entry-help" role="status" aria-live="polite">
          {{ t('knowledgeBase.entries_submitting') }}
        </p>
        <div class="knowledge-entry-editor-footer__actions">
          <el-button :disabled="submitting" @click="requestClose">
            {{ t('knowledgeBase.cancel') }}
          </el-button>
          <el-button
            type="primary"
            :loading="submitting"
            :disabled="loading || previewing"
            @click="submit"
          >
            {{ t('knowledgeBase.entries_save') }}
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style lang="scss">
@import '@/assets/css/KnowledgeEntries.scss';
</style>
