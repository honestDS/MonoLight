<template>
  <div class="channel-editor">
    <div class="channel-editor-config">
      <div v-if="usage === 'CHAT'" class="channel-editor-config-item">
        <el-form-item :label="$t('profiles.chat_timeout')" label-width="100px">
          <el-input-number
            v-model="channel.chat_timeout"
            :min="1"
            :max="600"
            controls-position="right"
          />
        </el-form-item>
      </div>
      <div v-else-if="usage === 'EMBEDDING'" class="channel-editor-config-item">
        <el-form-item :label="$t('profiles.embedding_timeout')" label-width="100px">
          <el-input-number
            v-model="channel.embedding_timeout"
            :min="1"
            :max="600"
            controls-position="right"
          />
        </el-form-item>
      </div>
      <template v-else-if="usage === 'RERANK'">
        <div class="channel-editor-config-item">
          <el-form-item :label="$t('profiles.rerank_timeout')" label-width="100px">
            <el-input-number
              v-model="channel.rerank_timeout"
              :min="1"
              :max="120"
              controls-position="right"
            />
          </el-form-item>
        </div>
        <div class="channel-editor-config-item">
          <el-form-item :label="$t('profiles.rerank_candidate_k')" label-width="100px">
            <el-input-number
              v-model="channel.rerank_candidate_k"
              :min="1"
              :max="50"
              controls-position="right"
            />
          </el-form-item>
        </div>
        <div class="channel-editor-config-item">
          <el-form-item :label="$t('profiles.kb_query_top_k')" label-width="100px">
            <el-input-number
              v-model="channel.kb_query_top_k"
              :min="1"
              :max="50"
              controls-position="right"
            />
          </el-form-item>
        </div>
      </template>
    </div>

    <el-form-item :label="label || $t('profiles.model_id')" label-width="100px">
      <el-select
        v-model="selectedRuleKeys"
        multiple
        filterable
        collapse-tags
        collapse-tags-tooltip
        :placeholder="$t('profiles.select_models')"
        class="full-width-input"
      >
        <el-option
          v-for="item in modelOptions"
          :key="item.key"
          :label="item.label"
          :value="item.key"
        >
          <div class="channel-option">
            <span class="channel-option-label">{{ item.label }}</span>
            <el-tag v-if="item.channel_disabled" type="warning" size="small">
              {{ $t('profiles.channel_disabled') }}
            </el-tag>
            <el-tag v-else-if="item.model_disabled" type="warning" size="small">
              {{ $t('profiles.model_disabled') }}
            </el-tag>
          </div>
        </el-option>
      </el-select>
    </el-form-item>

    <div v-if="channel.rules && channel.rules.length > 0" class="channel-hints">
      <div class="channel-hint-item text-muted">{{ $t('profiles.priority_hint') }}</div>
      <div class="channel-hint-item text-muted">{{ $t('profiles.weight_hint') }}</div>
      <div class="channel-hint-item text-muted">{{ $t('profiles.drag_hint') }}</div>
      <div class="channel-hint-item text-muted">{{ $t('profiles.disabled_rule_hint') }}</div>
    </div>

    <draggable
      v-if="channel.rules && channel.rules.length > 0"
      :list="channel.rules"
      :item-key="getRuleKey"
      handle=".rule-drag-handle"
      animation="180"
      ghost-class="rule-ghost"
      chosen-class="rule-chosen"
      class="channel-rule-list"
      @end="onDragEnd"
    >
      <template #item="{ element }">
        <div class="channel-rule-card" :class="{ 'is-group-start': isGroupStart(element) }">
          <div class="rule-header">
            <span class="rule-drag-handle" :title="$t('profiles.drag_to_sort')">
              <el-icon><Rank /></el-icon>
            </span>
            <span class="rule-priority-tag">P{{ element.priority || 1 }}</span>
            <span class="rule-name">{{ getRuleLabel(element) }}</span>
            <el-tag v-if="getRuleStatus(element)" :type="getRuleStatus(element).type" size="small" class="rule-status-tag">
              {{ $t(getRuleStatus(element).labelKey) }}
            </el-tag>
            <el-button type="text" class="remove" @click="removeRule(element)">
              {{ $t('profiles.remove') }}
            </el-button>
          </div>
          <div class="channel-rule-fields">
            <div class="channel-rule-field">
              <el-form-item :label="$t('profiles.model_id')" label-width="70px">
                <el-input :model-value="getRuleLabel(element)" disabled />
              </el-form-item>
            </div>
            <div class="channel-rule-field">
              <el-form-item :label="$t('profiles.priority')" label-width="60px">
                <el-input-number v-model="element.priority" :min="1" controls-position="right" @change="handlePriorityChange" />
              </el-form-item>
            </div>
            <div class="channel-rule-field">
              <el-form-item :label="$t('profiles.weight')" label-width="60px">
                <el-input-number v-model="element.weight" :min="0" controls-position="right" />
              </el-form-item>
            </div>
          </div>
        </div>
      </template>
    </draggable>

    <span v-if="!channel.rules || channel.rules.length === 0" class="text-muted">
      {{ $t('profiles.no_rules') }}
    </span>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { ElIcon } from 'element-plus'
import { Rank } from '@element-plus/icons-vue'
import draggable from 'vuedraggable'
import { defaultChannelRule } from '../constants'

const props = defineProps({
  channel: { type: Object, required: true },
  channels: { type: Array, required: true },
  usage: { type: String, required: true },
  label: { type: String, default: '' }
})

const getRuleKey = (rule) => `${rule.channel_id}::${rule.model_id}`

const compareRules = (left, right) => {
  return (left.priority || 1) - (right.priority || 1)
}

const sortRules = () => {
  if (!props.channel.rules) return
  props.channel.rules = [...props.channel.rules].sort(compareRules)
}

// 判断某规则是否为其所在优先级组的第一条（用于视觉分组分隔）
const isGroupStart = (rule) => {
  const list = props.channel.rules || []
  const idx = list.findIndex(r => getRuleKey(r) === getRuleKey(rule))
  if (idx <= 0) return true
  return (list[idx - 1].priority || 1) !== (rule.priority || 1)
}

const modelOptions = computed(() => {
  const options = []
  props.channels
    .forEach(channel => {
      ;(channel.model_ids || [])
        .filter(model => model.usage === props.usage && model.model_id)
        .forEach(model => {
          options.push({
            key: `${channel.id}::${model.model_id}`,
            channel_id: channel.id,
            channel_name: channel.name,
            model_id: model.model_id,
            label: `${channel.name} / ${model.model_id}`,
            channel_disabled: channel.is_active === false,
            model_disabled: model.is_enabled === false
          })
        })
    })
  return options
})

// 新增/移除模型时，默认给每条规则权重 1
const rebalanceWeights = (rules) => {
  rules.forEach(rule => {
    if (rule.weight === undefined || rule.weight === null) rule.weight = 1
  })
}

const selectedRuleKeys = computed({
  get() {
    return (props.channel.rules || []).map(getRuleKey)
  },
  set(keys) {
    if (!props.channel.rules) props.channel.rules = []
    const existingRuleMap = new Map(props.channel.rules.map(rule => [getRuleKey(rule), rule]))
    let nextChatPriority = Math.max(0, ...props.channel.rules.map(rule => rule.priority || 1)) + 1

    const rules = keys
      .map(key => {
        const option = modelOptions.value.find(item => item.key === key)
        if (!option) return null
        const existingRule = existingRuleMap.get(key)
        if (existingRule) return existingRule

        const rule = {
          ...defaultChannelRule(),
          channel_id: option.channel_id,
          model_id: option.model_id
        }
        if (props.usage === 'CHAT') {
          rule.priority = nextChatPriority
          nextChatPriority += 1
        }
        return rule
      })
      .filter(Boolean)

    rebalanceWeights(rules)
    props.channel.rules = rules.sort(compareRules)
  }
})

const findRuleChannel = (rule) => {
  return props.channels.find(item => item.id === rule.channel_id)
}

const findRuleModel = (rule) => {
  const channel = findRuleChannel(rule)
  return channel?.model_ids?.find(item => item.model_id === rule.model_id && item.usage === props.usage)
}

const getRuleLabel = (rule) => {
  const channel = findRuleChannel(rule)
  const model = findRuleModel(rule)
  if (channel && model) return `${channel.name} / ${model.model_id}`
  return `${rule.channel_id} / ${rule.model_id}`
}

const getRuleStatus = (rule) => {
  const channel = findRuleChannel(rule)
  if (!channel) return { type: 'danger', labelKey: 'profiles.channel_missing' }

  const model = findRuleModel(rule)
  if (!model) return { type: 'danger', labelKey: 'profiles.model_missing' }

  if (channel.is_active === false) return { type: 'warning', labelKey: 'profiles.channel_disabled' }
  if (model.is_enabled === false) return { type: 'warning', labelKey: 'profiles.model_disabled' }

  return null
}

const removeRule = (rule) => {
  const targetKey = getRuleKey(rule)
  const originalIndex = props.channel.rules.findIndex(item => getRuleKey(item) === targetKey)
  if (originalIndex >= 0) {
    props.channel.rules.splice(originalIndex, 1)
  }
  rebalanceWeights(props.channel.rules)
  sortRules()
}

// 优先级输入框变更后重新排序
const handlePriorityChange = () => {
  sortRules()
}

// 拖拽结束后仅调整优先级，不表示同一优先级组内的轮询顺序
// - 目标优先级组只有目标项一项（无重复）：交换被拖动项与目标项的优先级
// - 目标优先级组有多项（重复）：被拖动项直接改成该目标优先级，归入该组
const onDragEnd = (evt) => {
  const rules = props.channel.rules || []
  if (!rules.length || evt.oldIndex === evt.newIndex) return

  const moved = rules[evt.newIndex]
  if (!moved) return

  // 目标项：被拖动项移动方向上、被其顶替的紧邻项
  // 向上拖（newIndex < oldIndex）目标在其后；向下拖目标在其前
  const target = evt.newIndex < evt.oldIndex
    ? rules[evt.newIndex + 1]
    : rules[evt.newIndex - 1]

  if (!target) {
    sortRules()
    return
  }

  const targetPriority = target.priority || 1
  // 统计目标优先级在除被拖动项外的出现次数
  const dupCount = rules.filter(r => r !== moved && (r.priority || 1) === targetPriority).length

  if (dupCount > 1) {
    // 目标优先级组有多项：被拖动项归入该组
    moved.priority = targetPriority
  } else {
    // 目标优先级组仅目标项一项：交换两项优先级
    const movedPriority = moved.priority || 1
    moved.priority = targetPriority
    target.priority = movedPriority
  }

  sortRules()
}

// 初始化时按 priority 排序一次；后续排序由 priority 输入变更、增删规则、拖拽调整优先级后显式触发
onMounted(() => {
  sortRules()
})
</script>

<style lang="scss" scoped>
@import '@/assets/css/ChannelEditor.scss';
</style>