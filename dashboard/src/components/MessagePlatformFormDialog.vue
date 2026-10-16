<template>
  <el-dialog
    :model-value="visible"
    :title="isEdit ? $t('messagePlatforms.edit') : $t('messagePlatforms.create')"
    width="720px"
    class="dialog-with-scroll-body message-platform-form-dialog"
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
        <el-select v-model="localForm.uid" class="full-width-input" :disabled="!userPermissions.isSuperuser" :clearable="userPermissions.isSuperuser" :filterable="userPermissions.isSuperuser" :loading="usersLoading">
          <el-option v-for="user in users" :key="user.uid" :label="user.username" :value="user.uid" />
        </el-select>
      </el-form-item>
      <el-form-item :label="$t('messagePlatforms.language')">
        <el-select v-model="localForm.language" class="full-width-input">
          <el-option v-for="language in platformLanguages" :key="language" :label="$t(`messagePlatforms.language_map.${language}`)" :value="language" />
        </el-select>
      </el-form-item>
      <div class="platform-settings-notice">{{ $t('messagePlatforms.session_settings_hint') }}</div>
      <el-collapse v-model="expandedGroups" class="platform-settings-groups">
        <el-collapse-item name="conversation" :title="$t('chat.more_options_conversation_settings')">
          <el-form-item :label="$t('messagePlatforms.profile')">
            <el-select
              v-model="localForm.profile_id"
              class="full-width-input"
              clearable
              filterable
              :loading="profilesLoading"
              :disabled="!localForm.uid"
              :placeholder="$t('messagePlatforms.inherited_profile')"
              :aria-label="$t('messagePlatforms.profile')"
            >
              <el-option v-for="profile in profileOptions" :key="profile.id" :label="formatProfileOptionLabel(profile, $t('messagePlatforms.default_profile_suffix'))" :value="profile.id" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <template #label>
              <span>{{ $t('chat.goal_mode') }}</span>
              <HelpTooltip :content="$t('chat.goal_mode_hint')" />
            </template>
            <el-switch v-model="localForm.config.session_settings.goal_mode" :aria-label="$t('chat.goal_mode')" />
          </el-form-item>
          <el-form-item>
            <template #label>
              <span>{{ $t('chat.max_turns') }}</span>
              <HelpTooltip :content="$t('chat.max_turns_hint')" />
            </template>
            <el-input-number
              v-model="localForm.config.session_settings.max_turns"
              :min="1"
              :max="SESSION_MAX_TURNS_UPPER_BOUND"
              :step="1"
              :precision="0"
              controls-position="right"
              :aria-label="$t('chat.max_turns')"
              :disabled="localForm.config.session_settings.goal_mode"
            />
          </el-form-item>
          <el-form-item>
            <template #label>
              <span>{{ $t('messagePlatforms.stream_llm_request') }}</span>
              <HelpTooltip :content="$t('messagePlatforms.stream_llm_request_hint')" />
            </template>
            <el-radio-group v-model="localForm.config.stream_llm_request" :aria-label="$t('messagePlatforms.stream_llm_request')">
              <el-radio-button :label="false">{{ $t('chat.more_options_non_stream') }}</el-radio-button>
              <el-radio-button :label="true">{{ $t('chat.more_options_stream') }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </el-collapse-item>

        <el-collapse-item name="display" :title="$t('chat.more_options_display_settings')">
          <el-form-item :label="$t('chat.more_options_message_format')">
            <el-radio-group v-model="localForm.config.session_settings.enable_markdown" :aria-label="$t('chat.more_options_message_format')">
              <el-radio-button :label="false">{{ $t('chat.more_options_plain') }}</el-radio-button>
              <el-radio-button :label="true">{{ $t('chat.more_options_markdown') }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="$t('chat.more_options_tool_output')">
            <el-switch v-model="localForm.config.session_settings.show_tool_calls" :aria-label="$t('chat.more_options_tool_output')" />
          </el-form-item>
          <el-form-item :label="$t('chat.show_reasoning')">
            <el-switch v-model="localForm.config.session_settings.show_reasoning" :aria-label="$t('chat.show_reasoning')" />
          </el-form-item>
        </el-collapse-item>

        <el-collapse-item name="platform" :title="$t('messagePlatforms.platform_settings')">
          <el-form-item>
            <template #label>
              <span>{{ $t('messagePlatforms.use_stream_dispatch') }}</span>
              <HelpTooltip :content="$t('messagePlatforms.use_stream_dispatch_hint')" />
            </template>
            <el-switch v-model="localForm.use_stream_dispatch" :aria-label="$t('messagePlatforms.use_stream_dispatch')" />
          </el-form-item>
          <el-form-item :label="$t('messagePlatforms.api_timeout_ms')">
            <el-input-number v-model="localForm.config.api_timeout_ms" :min="1000" :max="120000" :aria-label="$t('messagePlatforms.api_timeout_ms')" />
          </el-form-item>
          <el-form-item :label="$t('messagePlatforms.long_poll_timeout_ms')">
            <el-input-number v-model="localForm.config.long_poll_timeout_ms" :min="1000" :max="120000" :aria-label="$t('messagePlatforms.long_poll_timeout_ms')" />
          </el-form-item>
          <el-form-item :label="$t('messagePlatforms.poll_interval_ms')">
            <el-input-number v-model="localForm.config.poll_interval_ms" :min="0" :max="60000" :aria-label="$t('messagePlatforms.poll_interval_ms')" />
          </el-form-item>
          <el-form-item :label="$t('messagePlatforms.merge_single_poll_messages')">
            <el-switch v-model="localForm.config.merge_single_poll_messages" :aria-label="$t('messagePlatforms.merge_single_poll_messages')" />
          </el-form-item>
        </el-collapse-item>
      </el-collapse>
    </el-form>
    <template #footer>
      <el-button @click="emit('update:visible', false)">{{ $t('common.cancel') }}</el-button>
      <el-button type="primary" :loading="submitting" @click="emit('submit')">{{ isEdit ? $t('common.confirm') : $t('messagePlatforms.create_and_login') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { filterProfilesByUid, formatProfileOptionLabel } from '../utils/profileOptions'
import { useUserPermissionsStore } from '../composables/useUserPermissions'
import HelpTooltip from './HelpTooltip.vue'
import { SESSION_MAX_TURNS_UPPER_BOUND } from '../constants/index.js'

const props = defineProps({
  visible: { type: Boolean, default: false },
  isEdit: { type: Boolean, default: false },
  form: { type: Object, required: true },
  platformTypes: { type: Array, default: () => [] },
  platformLanguages: { type: Array, default: () => [] },
  users: { type: Array, default: () => [] },
  usersLoading: { type: Boolean, default: false },
  profiles: { type: Array, default: () => [] },
  profilesLoading: { type: Boolean, default: false },
  typeLabel: { type: Function, required: true },
  submitting: { type: Boolean, default: false }
})

const emit = defineEmits(['update:visible', 'submit'])

const userPermissions = useUserPermissionsStore()

const localForm = computed(() => props.form)
const expandedGroups = ref([])

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

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      expandedGroups.value = []
    }
  }
)
</script>

<style lang="scss">
@use '../assets/css/MessagePlatformFormDialog.scss';
</style>
