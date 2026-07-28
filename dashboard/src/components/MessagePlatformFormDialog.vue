<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? $t('messagePlatforms.edit') : $t('messagePlatforms.create')"
    width="720px"
    class="dialog-with-scroll-body"
    @update:model-value="emit('update:visible', $event)">
    <el-form label-width="180px" :model="form">
      <el-form-item :label="$t('messagePlatforms.name')" required>
        <el-input v-model="localForm.name" />
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.platform_type')" required>
        <el-select v-model="localForm.platform_type" class="full-width-input" :disabled="isEdit">
          <el-option v-for="item in platformTypes" :key="item" :label="typeLabel(item)" :value="item" />
        </el-select>
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.enabled')">
        <el-switch v-model="localForm.is_enabled" />
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.uid')" :required="localForm.is_enabled">
        <el-select v-model="localForm.uid" class="full-width-input" clearable filterable :loading="usersLoading">
          <el-option v-for="user in users" :key="user.uid" :label="user.username" :value="user.uid" />
        </el-select>
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.profile')">
        <el-select v-model="localForm.profile_id" class="full-width-input" clearable filterable :loading="profilesLoading" :disabled="!localForm.uid" :placeholder="$t('messagePlatforms.inherited_profile')">
          <el-option v-for="profile in profileOptions" :key="profile.id" :label="formatProfileOptionLabel(profile, $t('messagePlatforms.default_profile_suffix'))" :value="profile.id" />
        </el-select>
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.api_timeout_ms')">
        <el-input-number v-model="localForm.config.api_timeout_ms" :min="1000" :max="120000" />
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.long_poll_timeout_ms')">
        <el-input-number v-model="localForm.config.long_poll_timeout_ms" :min="1000" :max="120000" />
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.poll_interval_ms')">
        <el-input-number v-model="localForm.config.poll_interval_ms" :min="0" :max="60000" />
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.merge_single_poll_messages')">
        <el-switch v-model="localForm.config.merge_single_poll_messages" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:visible', false)">{{ $t('common.cancel') }}</el-button>
      <el-button type="primary" :loading="submitting" @click="emit('submit')">{{ isEdit ? $t('common.confirm') : $t('messagePlatforms.create_and_login') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, watch } from 'vue'
import { filterProfilesByUid, formatProfileOptionLabel } from '../utils/profileOptions'

const props = defineProps({
  visible: { type: Boolean, default: false },
  isEdit: { type: Boolean, default: false },
  form: { type: Object, required: true },
  platformTypes: { type: Array, default: () => [] },
  users: { type: Array, default: () => [] },
  usersLoading: { type: Boolean, default: false },
  profiles: { type: Array, default: () => [] },
  profilesLoading: { type: Boolean, default: false },
  typeLabel: { type: Function, required: true },
  submitting: { type: Boolean, default: false }
})

const emit = defineEmits(['update:visible', 'submit'])

const localForm = computed(() => props.form)

const profileOptions = computed(() => filterProfilesByUid(props.profiles, localForm.value.uid))

watch(
  () => localForm.value.uid,
  () => {
    if (localForm.value.profile_id !== null) {
      localForm.value.profile_id = null
    }
  },
  { flush: 'sync' }
)
</script>
