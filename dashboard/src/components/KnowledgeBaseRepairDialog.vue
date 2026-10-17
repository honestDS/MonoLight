<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps({
  visible: Boolean,
  isSuperuser: Boolean,
  ownerUid: {
    type: String,
    default: null,
  },
  owners: {
    type: Array,
    default: () => [],
  },
  loading: Boolean,
  error: {
    type: String,
    default: null,
  },
  submitting: Boolean,
})

const emit = defineEmits(['close', 'submit', 'refresh', 'update:ownerUid'])
const { t } = useI18n()

const dialogVisible = computed({
  get: () => props.visible,
  set: (value) => {
    if (!value && !props.submitting) emit('close')
  },
})
</script>

<template>
  <el-dialog
    :title="t('knowledgeBase.repair')"
    v-model="dialogVisible"
    width="600px"
    class="standard-dialog"
    center
    align-center
    :close-on-click-modal="!props.submitting"
    :close-on-press-escape="!props.submitting"
    :show-close="!props.submitting"
  >
    <el-alert
      type="warning"
      :title="t('knowledgeBase.repair')"
      :description="t('knowledgeBase.repair_user_scope')"
      :closable="false"
      class="mb-15"
    />
    <el-alert
      v-if="props.error"
      type="error"
      :title="props.error"
      :closable="false"
      class="mb-15"
    />
    <el-button
      v-if="props.error"
      link
      type="primary"
      :disabled="props.submitting"
      class="mb-15"
      @click="emit('refresh')"
    >
      {{ t('knowledgeBase.refresh') }}
    </el-button>
    <el-form label-width="120px" size="default">
      <el-form-item :label="t('knowledgeBase.owner_username')" for="knowledge-base-repair-owner">
        <el-select
          v-if="props.isSuperuser"
          id="knowledge-base-repair-owner"
          :model-value="props.ownerUid"
          filterable
          class="full-width-input"
          :placeholder="t('knowledgeBase.select_owner_user')"
          :loading="props.loading"
          :disabled="props.loading || !!props.error || props.submitting"
          @update:model-value="emit('update:ownerUid', $event)"
        >
          <el-option
            v-for="user in props.owners"
            :key="user.uid"
            :label="user.username || t('knowledgeBase.owner_unknown')"
            :value="user.uid"
          />
        </el-select>
        <el-input
          v-else
          id="knowledge-base-repair-owner"
          :model-value="props.owners[0]?.username || t('knowledgeBase.owner_unknown')"
          readonly
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="props.submitting" @click="emit('close')">
        {{ t('knowledgeBase.cancel') }}
      </el-button>
      <el-button
        type="primary"
        :loading="props.submitting"
        :disabled="props.loading || !!props.error || !(props.isSuperuser ? props.ownerUid : props.owners[0]?.uid)"
        @click="emit('submit')"
      >
        {{ t('knowledgeBase.repair') }}
      </el-button>
    </template>
  </el-dialog>
</template>
