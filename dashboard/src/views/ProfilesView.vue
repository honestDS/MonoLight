<template>
  <div class="view-container">
    <BaseDataTable
      :data="profiles"
      :loading="loading"
      :total="total"
      v-model:current-page="currentPage"
      v-model:page-size="pageSize"
      create-text="新建配置"
      @create="showDialog('create')"
      @refresh="handleRefresh"
      @page-change="loadProfiles"
      @size-change="handleSizeChange">

      <el-table-column :resizable="false" prop="name" label="配置名称" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" prop="provider_name" label="提供商" min-width="120" sortable></el-table-column>
      <el-table-column :resizable="false" label="模型 ID" min-width="150" sortable>
        <template #default="scope">
          {{ scope.row.configs?.provider?.model_id || '未设置' }}
        </template>
      </el-table-column>
      
      <el-table-column :resizable="false" label="状态" align="center" sortable>
        <template #default="scope">
          <StatusTag :status="scope.row.is_active" active-text="活动中" inactive-text="闲置" />
        </template>
      </el-table-column>

      <el-table-column :resizable="false" label="操作" width="380" align="center" fixed="right">
        <template #default="scope">
          <div class="action-buttons">
            <el-button :type="scope.row.is_active ? 'info' : 'success'" size="small" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">激活</el-button>
            <el-button type="primary" size="small" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button type="danger" size="small" @click="handleDelete(scope.row.id, scope.row.name)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </BaseDataTable>

    <el-dialog :title="dialogType === 'create' ? '新建配置' : '编辑配置'" v-model="dialogVisible" width="60%" class="standard-dialog" center align-center>
      <el-form :model="form" label-width="120px" size="default">
        <el-tabs v-model="activeTab">
          <!-- 基础设置 -->
          <el-tab-pane label="基础设置" name="base">
            <div class="tab-pane-content">
              <el-form-item label="配置名称">
                <el-input v-model="form.name" placeholder="唯一配置名称"></el-input>
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
              <el-divider content-position="left"><span class="gray-divider-text">对话模型</span></el-divider>
              <el-form-item label="提供商">
                <el-select v-model="form.configs.provider.provider_id" placeholder="选择对话模型提供商" class="full-width-input">
                  <el-option v-for="item in chatProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>
              </el-form-item>
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
                  <el-form-item label="最大输出 Token">
                    <el-input-number v-model="form.configs.provider.max_tokens" :min="0" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="启用多模态支持">
                    <el-switch v-model="form.configs.provider.multimodal"></el-switch>
                  </el-form-item>
                </el-col>
              </el-row>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="上下文限制 K">
                    <el-input-number v-model="form.configs.provider.context_window_k" :min="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">限制上下文最大 Token 数（单位：K Tokens）</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="对话超时 (秒)">
                    <el-input-number v-model="form.configs.provider.chat_timeout" :min="1" :max="600" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">对话模型调用超时时间。流式对话下该超时仅作用于首字生成阶段，开始输出后不再判定超时，避免长回答被中途切断。</div>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">嵌入模型</span></el-divider>
              <el-form-item label="提供商">
                <el-select v-model="form.configs.provider.embedding_provider_id" placeholder="选择向量提供商 (可选)" clearable class="full-width-input">
                  <el-option v-for="item in embeddingProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

              </el-form-item>
              <el-form-item label="向量模型 ID">
                <el-input v-model="form.configs.provider.embedding_model_id" placeholder="可选，用于知识库的专属模型如 text-embedding-3-small"></el-input>
              </el-form-item>
              <el-form-item label="向量输出维度">
                <div style="display: flex; gap: 10px; width: 100%;">
                  <el-input-number v-model="form.configs.provider.embedding_dimensions" :min="1" :step="1" placeholder="如 1024 (可选)" style="flex: 1;"></el-input-number>
                  <el-button type="primary" @click="handleDetectDimension" :loading="detectingDimension" :disabled="!form.configs.provider.embedding_provider_id || !form.configs.provider.embedding_model_id">自动检测</el-button>
                </div>
                <div class="help-text mt-5">部分向量模型支持自定义输出维度。导入文档时会先尝试携带该维度请求；如果模型不支持，会自动回退为默认维度请求。留空则始终使用模型默认维度。</div>
              </el-form-item>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="嵌入超时 (秒)">
                    <el-input-number v-model="form.configs.provider.embedding_timeout" :min="1" :max="600" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">向量模型调用的整体超时时间，用于文档导入向量化与知识库检索时的嵌入请求。</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="知识库返回数量">
                    <el-input-number v-model="form.configs.provider.kb_query_top_k" :min="1" :max="50" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">对话时调用知识库工具最终返回给模型的片段数量。启用 Reranker 时，需确保下方“候选数量 K”大于该值，精排才会生效。</div>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">重排模型 (Reranker)</span></el-divider>

              <el-form-item label="提供商">
                <el-select v-model="form.configs.provider.rerank_provider_id" placeholder="选择 Rerank 提供商 (可选)" clearable class="full-width-input">
                  <el-option v-for="item in rerankProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

                <div class="help-text mt-5">同时配置 Rerank 提供商与模型 ID 即视为启用远程 reranker；留空则不启用。远程调用失败时自动回退到混合检索结果。</div>
              </el-form-item>
              <el-form-item label="Rerank 模型 ID">
                <el-input v-model="form.configs.provider.rerank_model_id" placeholder="可选，如 rerank-model-id"></el-input>
              </el-form-item>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="候选数量 K">
                    <el-input-number v-model="form.configs.provider.rerank_candidate_k" :min="1" :max="50" :step="1" class="full-width-input"></el-input-number>
                    <div class="help-text mt-5">送入远程 reranker 的候选片段数量（上限 50），需大于等于知识库返回数量。</div>
                  </el-form-item>
                </el-col>
                <el-col :span="12">
                  <el-form-item label="超时 (秒)">
                    <el-input-number v-model="form.configs.provider.rerank_timeout" :min="1" :max="120" :step="1" class="full-width-input"></el-input-number>
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
                  <el-option v-for="item in auditProviders" :key="item.id" :label="item.name" :value="item.id"></el-option>
                </el-select>

              </el-form-item>
              <el-form-item label="审计模型 ID">
                <el-input v-model="form.configs.security.audit_model_id" placeholder="用于审计的模型 ID"></el-input>
              </el-form-item>
              <el-form-item label="二次确认阈值 (0-7)">
                <el-slider v-model="form.configs.security.audit_threshold" :min="0" :max="7" show-stops show-input></el-slider>
                <div class="help-text">分数越高，触发二次确认的敏感度越低（越宽松）</div>
              </el-form-item>
            </div>
          </el-tab-pane>

          <!-- 工具设置 -->
          <el-tab-pane label="工具设置" name="tool">
            <div class="tab-pane-content">
              <el-divider content-position="left"><span class="gray-divider-text">调度与并行控制</span></el-divider>
              <el-row :gutter="20">
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

              <el-divider content-position="left"><span class="gray-divider-text">Shell 工具配置</span></el-divider>
              <el-row :gutter="20">
                <el-col :span="12">
                  <el-form-item label="Shell 超时(s)">
                    <el-input-number v-model="form.configs.tool.shell_timeout" :min="1" class="full-width-input"></el-input-number>
                  </el-form-item>
                </el-col>
              </el-row>

              <el-divider content-position="left"><span class="gray-divider-text">Firecrawl 配置</span></el-divider>
              <el-row :gutter="20">
                <el-col :span="24">
                  <el-form-item label="API Key">
                    <el-input v-model="form.configs.tool.firecrawl_api_key" placeholder="用于网页搜索/抓取的 Firecrawl API Key" show-password></el-input>
                    <div class="help-text mt-5">
                      用于网页搜索和抓取功能。请前往 
                      <el-link type="primary" href="https://www.firecrawl.dev/" target="_blank" :underline="false">Firecrawl 官网</el-link> 
                      注册并获取您的 API Key。
                    </div>
                  </el-form-item>
                </el-col>
              </el-row>
            </div>
          </el-tab-pane>

          <!-- 其他设置 -->
          <el-tab-pane label="其他设置" name="other">
            <div class="tab-pane-content">
              <div class="help-text">暂无其他配置项</div>
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
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { profileApi, providerApi, promptApi } from '../api'
import BaseDataTable from '../components/BaseDataTable.vue'
import StatusTag from '../components/StatusTag.vue'
import { useDeleteConfirm } from '../composables/useDeleteConfirm'
import { defaultProfileConfigs } from '../constants'

const profiles = ref([])
const providers = ref([])
const prompts = ref([])
const loading = ref(false)
const total = ref(0)
const currentPage = ref(1)
const pageSize = ref(10)
const dialogVisible = ref(false)
const dialogType = ref('create')
const submitting = ref(false)
const activeTab = ref('base')
const detectingDimension = ref(false)

// 按模型类型与启用状态过滤提供商，供各模型配置下拉仅展示“未禁用且类型符合”的选项。
// 为兼容编辑场景下已选中但当前被禁用/类型不符的存量值，额外把当前已选 id 保留在候选中，避免回显丢失。
const filterProvidersByUsage = (usage, selectedId) => {
  return providers.value.filter(
    (item) => (item.usage === usage && item.is_active) || item.id === selectedId
  )
}

const chatProviders = computed(() => filterProvidersByUsage('CHAT', form.configs.provider.provider_id))
const embeddingProviders = computed(() => filterProvidersByUsage('EMBEDDING', form.configs.provider.embedding_provider_id))
const rerankProviders = computed(() => filterProvidersByUsage('RERANK', form.configs.provider.rerank_provider_id))
const auditProviders = computed(() => filterProvidersByUsage('CHAT', form.configs.security.audit_provider_id))


const form = reactive({
  id: null,
  name: '',
  prompt_id: null,
  configs: defaultProfileConfigs()
})

// 加载配置列表
const loadProfiles = async () => {
  loading.value = true
  try {
    const res = await profileApi.list({
      page: currentPage.value,
      size: pageSize.value
    })
    profiles.value = res.data.data.items || []
    total.value = res.data.data.total || 0
  } catch (err) {
    ElMessage.error(err.message || '加载列表失败')
  } finally {
    loading.value = false
  }
}

// 使用删除确认组合式函数
const { handleDelete } = useDeleteConfirm(profileApi.delete, loadProfiles)

const fetchPrompts = async () => {
  try {
    const res = await promptApi.list({ page: 1, size: 1000 })
    prompts.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || '加载提示词库失败')
  }
}

const fetchProviders = async () => {
  try {
    const res = await providerApi.list({ page: 1, size: 1000 })
    providers.value = res.data.data.items || []
  } catch (err) {
    ElMessage.error(err.message || '加载提供商失败')
  }
}

const handleRefresh = () => {
  currentPage.value = 1
  loadProfiles()
  fetchProviders()
  fetchPrompts()
}

const handleSizeChange = () => {
  currentPage.value = 1
  loadProfiles()
}

const handleDetectDimension = async () => {
  if (!form.configs.provider.embedding_provider_id || !form.configs.provider.embedding_model_id) {
    return ElMessage.warning('请先选择向量模型提供商并填写向量模型 ID')
  }
  detectingDimension.value = true
  try {
    const res = await providerApi.testEmbeddingDimension(form.configs.provider.embedding_provider_id, form.configs.provider.embedding_model_id)
    const dim = res.data.data.dimension
    form.configs.provider.embedding_dimensions = dim
    ElMessage.success(res.data.message || `检测成功，维度已设为: ${dim}`)
  } catch (err) {
    ElMessage.error(err.message || '检测失败，请检查配置信息或模型是否支持')
  } finally {
    detectingDimension.value = false
  }
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

const showDialog = (type, row = null) => {
  dialogType.value = type
  activeTab.value = 'base'
  if (type === 'edit' && row) {
    form.id = row.id
    form.name = row.name
    form.prompt_id = row.prompt_id
    const base = defaultProfileConfigs()
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
    form.prompt_id = null
    form.configs = defaultProfileConfigs()
  }
  dialogVisible.value = true
}

const submitForm = async () => {
  if (!form.name || !form.configs.provider.provider_id || !form.configs.provider.model_id) {
    return ElMessage.warning('请补全必填配置信息（配置名称、对话模型提供商、对话模型ID）')
  }
  // Reranker 启用判定：同时配置了提供商与模型 ID 即视为启用
  const rerankProviderId = form.configs.provider.rerank_provider_id
  const rerankModelId = (form.configs.provider.rerank_model_id || '').trim()
  const hasRerankProvider = !!rerankProviderId
  const hasRerankModel = !!rerankModelId
  if (hasRerankProvider || hasRerankModel) {
    // 仅配置其一视为配置不完整
    if (!hasRerankProvider || !hasRerankModel) {
      return ElMessage.warning('启用 Reranker 需同时配置 Rerank 提供商与模型 ID，否则请将两者都留空')
    }
    // 候选数量 K 必须大于等于知识库返回数量，否则精排不会生效
    const candidateK = form.configs.provider.rerank_candidate_k
    const topK = form.configs.provider.kb_query_top_k
    if (Number(candidateK) < Number(topK)) {
      return ElMessage.warning('Rerank 候选数量 K 必须大于等于知识库返回数量，否则精排不会生效')
    }
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
@import "@/assets/css/ProfilesView.scss";
.el-form-item__content{
  gap: 10px;
}
</style>
