<template>
  <div class="view-container">
    <div class="page-header-actions">
      <el-button type="primary" size="default" @click="showDialog('create')">新建配置</el-button>
      <el-button size="default" @click="handleRefresh">刷新列表</el-button>
    </div>

    <el-table :data="profiles" v-loading="loading" border stripe size="default">
      <el-table-column :resizable="false" prop="name" label="配置名称" min-width="120"></el-table-column>
      <el-table-column :resizable="false" prop="provider_name" label="提供商" min-width="120"></el-table-column>
      <el-table-column :resizable="false" label="模型 ID" min-width="150">
        <template #default="scope">
          {{ scope.row.configs?.provider?.model_id || '未设置' }}
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
          <div class="action-buttons">
            <el-button link type="primary" size="small" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">激活</el-button>
            <el-button link type="primary" size="small" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="handleDelete(scope.row.id)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :title="dialogType === 'create' ? '新建配置' : '编辑配置'" v-model="dialogVisible" width="50%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="120px" size="default">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane label="基础设置" name="base">
            <div class="tab-pane-content">
              <el-form-item label="配置名称">
                <el-input v-model="form.name" placeholder="唯一配置名称"></el-input>
              </el-form-item>
              <el-form-item label="模型提供商">
                <el-select v-model="form.provider_id" placeholder="选择提供商" class="full-width-input">
                  <el-option v-for="item in providers" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="关联提示词库">
                <el-select v-model="form.prompt_id" placeholder="可选关联提示词" clearable class="full-width-input">
                  <el-option v-for="item in prompts" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
            </div>
          </el-tab-pane>

          <!-- 模型设置 -->
          <el-tab-pane label="模型设置" name="model">
            <div class="tab-pane-content">
              <el-form-item label="模型 ID">
                <el-input v-model="form.configs.provider.model_id" placeholder="如 gpt-4o"></el-input>
              </el-form-item>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="Temperature">
                    <el-input-number v-model="form.configs.provider.temperature" :min="0" :max="2" :step="0.1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="Top P">
                    <el-input-number v-model="form.configs.provider.top_p" :min="0" :max="1" :step="0.05" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="最大 Token">
                    <el-input-number v-model="form.configs.provider.max_tokens" :min="0" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="启用流式输出">
                    <el-switch v-model="form.configs.provider.stream"></el-switch>
                  </el-form-item>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <!-- 安全设置 -->
          <el-tab-pane label="安全设置" name="security">
            <div class="tab-pane-content">
              <el-form-item label="审计提供商">
                <el-select v-model="form.configs.security.audit_provider_id" placeholder="选择审计服务商" clearable class="full-width-input">
                  <el-option v-for="item in providers" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="审计模型 ID">
                <el-input v-model="form.configs.security.audit_model_id" placeholder="用于审计的模型 ID"></el-input>
              </el-form-item>
              <el-form-item label="拦截阈值 (0-7)">
                <el-slider v-model="form.configs.security.audit_threshold" :min="0" :max="7" show-stops show-input></el-slider>
                <div class="help-text">分数越高，触发拦截的敏感度越低（越宽松）</div>
              </el-form-item>
            </div>
          </el-tab-pane>

          <!-- 工具设置 -->
          <el-tab-pane label="工具设置" name="tool">
            <div class="tab-pane-content">
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="Shell 超时(s)">
                    <el-input-number v-model="form.configs.tool.shell_timeout" :min="1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="最大并行数">
                    <el-input-number v-model="form.configs.tool.max_parallel_tools" :min="1" :max="20" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="最大连续轮数">
                    <el-input-number v-model="form.configs.tool.max_turns" :min="1" :max="20" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <!-- 其他设置 -->
          <el-tab-pane label="其他设置" name="other">
            <div class="tab-pane-content">
              <el-form-item label="上下文限制 K">
                <el-input-number v-model="form.configs.other.context_window_k" :min="1" class="full-width-input"></el-input-number>
                <div class="form-help-text mt-5">关联短期上下文的历史消息轮数</div>
              </el-form-item>
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">取消</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { profileApi, providerApi, promptApi } from '../api'

const { t } = useI18n()
const profiles = ref([])
const providers = ref([])
const prompts = ref([])
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

const fetchPrompts = async () => {
  try {
    const res = await promptApi.list()
    prompts.value = res.data.data
  } catch (err) {
    ElMessage.error(err.message || '加载提示词库失败')
  }
}

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
  fetchPrompts()
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
  fetchPrompts()
})
</script>

<style lang="scss">
@import "@/assets/css/profiles.scss";
</style>
