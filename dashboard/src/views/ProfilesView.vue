<template>
  <div class="profiles-container">
    <div class="header-actions">
      <el-button type="primary" size="default" @click="showDialog('create')">新建配置</el-button>
      <el-button type="default" size="default" @click="handleRefresh" :loading="loading" style="margin-left: 10px">刷新</el-button>
    </div>

    <el-table :data="profiles" v-loading="loading" border stripe size="default">
      <el-table-column :resizable="false" prop="name" label="配置名称" min-width="120"></el-table-column>
      <el-table-column :resizable="false" prop="provider_name" label="提供商" min-width="120"></el-table-column>
      <el-table-column :resizable="false" label="模型 ID" min-width="150">
        <template #default="scope">
          {{ scope.row.configs?.provider?.model_id || '未设置' }}
        </template>
      </el-table-column>
      
      <el-table-column :resizable="false" label="推理参数" align="center">
        <template #default="scope">
          <div v-if="scope.row.configs?.provider" style="font-size: 12px">
            Temp: {{ scope.row.configs.provider.temperature }} / TopP: {{ scope.row.configs.provider.top_p }}
          </div>
        </template>
      </el-table-column>

      <el-table-column :resizable="false" label="限制" align="center">
        <template #default="scope">
          <div v-if="scope.row.configs" style="font-size: 12px">
            Tokens: {{ scope.row.configs.provider?.max_tokens }} <br/>
            Context: {{ scope.row.configs.other?.context_window_k }}K
          </div>
        </template>
      </el-table-column>
      
      <el-table-column :resizable="false" label="状态" align="center">
        <template #default="scope">
          <el-tag :type="scope.row.is_active ? 'success' : 'info'" size="default">
            {{ scope.row.is_active ? '活动中' : '闲置' }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column :resizable="false" label="操作" width="280" align="center" fixed="right">
        <template #default="scope">
          <div>
            <el-button link type="primary" size="default" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">激活</el-button>
            <el-button link type="primary" size="default" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button link type="danger" size="default" @click="handleDelete(scope.row.id)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :title="dialogType === 'create' ? '新建配置' : '编辑配置'" v-model="dialogVisible" width="700px">
      <el-form :model="form" label-width="120px" size="default">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane label="基础设置" name="base">
            <el-row :gutter="20" style="margin-top: 20px">
              <el-col :span="12">
                <el-form-item label="配置名称">
                  <el-input v-model="form.name" placeholder="唯一配置名称"></el-input>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="模型提供商">
                  <el-select v-model="form.provider_id" placeholder="选择提供商" style="width: 100%">
                    <el-option v-for="item in providers" :key="item.id" :label="item.name" :value="item.id"></el-option>
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="24">
                <el-form-item label="模型 ID">
                  <el-input v-model="form.configs.provider.model_id" placeholder="如 gpt-4o"></el-input>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="Temperature">
                  <el-input-number v-model="form.configs.provider.temperature" :min="0" :max="2" :step="0.1" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="Top P">
                  <el-input-number v-model="form.configs.provider.top_p" :min="0" :max="1" :step="0.05" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="最大 Token">
                  <el-input-number v-model="form.configs.provider.max_tokens" :min="0" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="启用流式输出">
                  <el-switch v-model="form.configs.provider.stream"></el-switch>
                </el-form-item>
              </el-col>
            </el-row>
          </el-tab-pane>

          <!-- 进阶/工具设置 -->
          <el-tab-pane label=" Agent & 系统" name="advanced">
            <el-divider content-position="left">工具调用 (Agent)</el-divider>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="Shell 超时(s)">
                  <el-input-number v-model="form.configs.tool.shell_timeout" :min="1" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="最大并行数">
                  <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="最大连续轮数">
                  <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
            </el-row>

            <el-divider content-position="left">系统设置</el-divider>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="上下文限制 K">
                  <el-input-number v-model="form.configs.other.context_window_k" :min="1" style="width: 100%"></el-input-number>
                </el-form-item>
              </el-col>
            </el-row>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { profileApi, providerApi } from '../api'

const { t } = useI18n()
const profiles = ref([])
const providers = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)
const activeTab = ref('base')

const defaultConfigs = () => ({
  provider: { model_id: '', temperature: 0.7, top_p: 1.0, max_tokens: 2048, stream: false },
  security: { audit_provider_id: null, audit_model_id: null, audit_threshold: 5 },
  tool: { shell_timeout: 30, max_parallel_tools: 5, max_turns: 5 },
  other: { context_window_k: 4 }
})

const form = reactive({
  id: null,
  name: '',
  provider_id: null,
  prompt_id: null,
  configs: defaultConfigs()
})

const fetchProviders = async () => {
  try {
    const res = await providerApi.list()
    providers.value = res.data.data
  } catch (err) {
    ElMessage.error(err.message || '加载提供商失败')
  }
}

const loadProfiles = async () => {
  loading.value = true
  try {
    const res = await profileApi.list()
    profiles.value = res.data.data || []
  } catch (err) {
    ElMessage.error(err.message || '加载列表失败')
  } finally {
    loading.value = false
  }
}

const handleRefresh = () => {
  loadProfiles()
  fetchProviders()
}

const handleActivate = async (id) => {
  try {
    const res = await profileApi.activate(id)
    ElMessage.success(res.data.message || '配置已切换')
    loadProfiles()
  } catch (err) {
    ElMessage.error(err.message || '切换失败')
  }
}

const handleDelete = async (id) => {
  try {
    await ElMessageBox.confirm(t('common.delete_confirm'), t('common.warning'), { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') })
    await profileApi.delete(id)
    ElMessage.success('配置已移除')
    loadProfiles()
  } catch (err) {
    if (err !== 'cancel') ElMessage.error(err.message || '删除失败')
  }
}

const showDialog = (type, row = null) => {
  dialogType.value = type
  activeTab.value = 'base'
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.provider_id = row.provider_id
    form.prompt_id = row.prompt_id
    // Deep merge configs to ensure all nested objects exist
    const base = defaultConfigs()
    if (row.configs) {
      Object.keys(base).forEach(key => {
        if (row.configs[key]) {
          Object.assign(base[key], row.configs[key])
        }
      })
    }
    form.configs = base
  } else {
    form.id = null
    form.name = ''
    form.provider_id = null
    form.prompt_id = null
    form.configs = defaultConfigs()
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.provider_id || !form.configs.provider.model_id) {
    return ElMessage.warning('请补全基础配置信息')
  }
  submitting.value = true
  try {
    if (dialogType.value === 'create') {
      await profileApi.create(form)
    } else {
      await profileApi.update(form.id, form)
    }
    ElMessage.success('配置已保存')
    dialogVisible.value = false
    loadProfiles()
  } catch (err) {
    ElMessage.error(err.message || '提交失败')
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  loadProfiles()
  fetchProviders()
})
</script>

<style lang="scss">
@import "@/assets/css/profiles.scss";
.profiles-container {
  padding: 20px;
  .header-actions { margin-bottom: 20px; }
  .active-tag { font-weight: bold; }
}
</style>
