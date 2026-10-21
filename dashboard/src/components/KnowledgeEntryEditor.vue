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
    class="standard-dialog dialog-with-scroll-body"
    modal-class="knowledge-entry-editor"
    width="840px"
    align-center
    :close-on-click-modal="false"
    :show-close="!submitting"
    :before-close="handleBeforeClose"
  >
    <section class="knowledge-entry-target" :aria-label="t('knowledgeBase.target_kb')">
      <div>{{ t('knowledgeBase.kb_name') }}: {{ selectedKb?.name || '-' }}</div>
      <div>
        {{ t('knowledgeBase.knowledge_base_type') }}:
        {{ t(selectedKb?.knowledge_base_type === 'llm_managed' ? 'knowledgeBase.type_managed' : 'knowledgeBase.type_user') }}
      </div>
      <div>{{ t('knowledgeBase.owner_username') }}: {{ selectedKb?.username || t('knowledgeBase.owner_unknown') }}</div>
      <div>{{ t('knowledgeBase.owner_profiles') }}: {{ getKnowledgeBaseProfileLabel(selectedKb) }}</div>
    </section>

    <el-radio-group
      v-if="editingId === null"
      v-model="mode"
      :disabled="busy"
      class="knowledge-entry-mode"
    >
      <el-radio-button label="manual">{{ t('knowledgeBase.entries_manual') }}</el-radio-button>
      <el-radio-button label="file">{{ t('knowledgeBase.entries_file') }}</el-radio-button>
    </el-radio-group>

    <div v-if="editingId === null && mode === 'file'" class="knowledge-entry-file-action">
      <span class="knowledge-entry-help">{{ t('knowledgeBase.entries_file_hint') }}</span>
      <el-upload
        ref="uploadRef"
        :auto-upload="false"
        :show-file-list="false"
        :limit="1"
        accept=".txt,.md,.csv,.json,.yaml,.yml,.log"
        :disabled="busy"
        @change="handleFileChange"
      >
        <el-button type="primary" :disabled="busy">{{ t('knowledgeBase.select_file') }}</el-button>
      </el-upload>
    </div>

    <div v-if="previewing" class="knowledge-entry-help" role="status" aria-live="polite">
      {{ t('knowledgeBase.entries_previewing') }}
    </div>

    <div v-if="sourceFilename" class="knowledge-entry-source">
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
        />
        <div v-if="isManaged" class="knowledge-entry-help">
          {{ t('knowledgeBase.entries_name_hint_managed') }}
        </div>
      </el-form-item>

      <el-form-item prop="content" for="knowledge-entry-content" :label="t('knowledgeBase.entries_content')">
        <el-input
          id="knowledge-entry-content"
          v-model="form.content"
          type="textarea"
          :rows="9"
          :autosize="{ minRows: 9, maxRows: 18 }"
          :disabled="busy"
        />
      </el-form-item>

      <el-form-item v-if="isManaged">
        <span>{{ t('knowledgeBase.entries_maintainable') }}</span>
        <el-switch
          v-model="form.llm_maintainable"
          :disabled="busy"
          :aria-label="t('knowledgeBase.entries_maintainable')"
        />
        <div class="knowledge-entry-help">
          {{ t('knowledgeBase.managed_llm_maintainable_hint') }}
        </div>
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

    <div v-if="submitting" class="knowledge-entry-help" role="status" aria-live="polite">
      {{ t('knowledgeBase.entries_submitting') }}
    </div>

    <template #footer>
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
    </template>
  </el-dialog>
</template>

<style lang="scss">
@import '@/assets/css/KnowledgeEntries.scss';
</style>
