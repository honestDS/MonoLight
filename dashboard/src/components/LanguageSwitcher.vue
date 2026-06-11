<template>
  <el-dropdown trigger="click" @command="handleSwitch" class="lang-switcher">
    <span class="lang-switcher-label">
      <el-icon class="lang-switcher-icon"><Operation /></el-icon>
      <span>{{ currentLabel }}</span>
    </span>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item
          v-for="item in SUPPORT_LOCALES"
          :key="item.value"
          :command="item.value"
          :class="{ 'is-active': item.value === locale }"
        >
          {{ item.label }}
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElDropdown, ElDropdownMenu, ElDropdownItem, ElIcon } from 'element-plus'
import { SUPPORT_LOCALES, setLocale } from '../i18n'

const { locale } = useI18n()

// 当前语言对应的显示名称
const currentLabel = computed(() => {
  const matched = SUPPORT_LOCALES.find((item) => item.value === locale.value)
  return matched ? matched.label : locale.value
})

// 切换语言
const handleSwitch = (value) => {
  if (value === locale.value) return
  setLocale(value)
}
</script>

<style lang="scss" scoped>
.lang-switcher {
  cursor: pointer;
}

.lang-switcher-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: inherit;
  font-size: 14px;
  outline: none;
}

.lang-switcher-icon {
  font-size: 16px;
}

:deep(.is-active) {
  color: var(--el-color-primary);
  font-weight: 600;
}
</style>
