<template>
  <div class="profiles-container">
    <div class="header-actions">
      <el-button type="primary" size="default" @click="showDialog('create')">新建配置</el-button>
      <el-button type="default" size="default" @click="handleRefresh" :loading="loading" style="margin-left: 10px">刷新</el-button>
    </div>

    <el-table :data="profiles" v-loading="loading" border stripe size="default">
      <el-table-column prop="name" label="配置名称" min-width="120"></el-table-column>
      <el-table-column prop="provider_name" label="提供商" min-width="120"></el-table-column>
      <el-table-column prop="model_id" label="模型" min-width="150"></el-table-column>
      
      <el-table-column label="参数 (Temp/TopP)" align="center">
        <template #default="scope">
          <span v-if="scope && scope.row">{{ scope.row.temperature }} / {{ scope.row.top_p }}</span>
        </template>
      </el-table-column>

      <el-table-column prop="max_tokens" label="最大 Token" align="center"></el-table-column>
      <el-table-column prop="context_window_k" label="上下文限制 K" align="center"></el-table-column>
      
      <el-table-column label="流式" align="center">
        <template #default="scope">
          <el-tag v-if="scope && scope.row" :type="scope.row.stream ? 'success' : 'info'" size="default">{{ scope.row.stream ? '开启' : '关闭' }}</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="状态" align="center">
        <template #default="scope">
          <el-tag v-if="scope && scope.row" :type="scope.row.is_active ? 'success' : 'info'" class="active-tag">
            {{ scope.row.is_active ? '活动中' : '闲置' }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="320" align="center" fixed="right">
        <template #default="scope">
          <div v-if="scope && scope.row">
            <el-button type="text" size="default" :disabled="scope.row.is_active" @click="handleActivate(scope.row.id)">激活</el-button>
            <el-button type="text" size="default" @click="showDialog('edit', scope.row)">编辑</el-button>
            <el-button type="text" size="default" style="color: #F56C6C" @click="handleDelete(scope.row.id)">删除</el-button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :title="dialogType === 'create' ? '新建配置' : '编辑配置'" v-model="dialogVisible" width="600px">
      <el-form :model="form" label-width="120px" size="default">
        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="名称">
              <el-input v-model="form.name" placeholder="唯一配置名称"></el-input>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="模型提供商">
              <el-select v-model="form.provider_id" placeholder="选择提供商" style="width: 100%">
                <el-option v-for="item in providers" :key="item.id" :label="item.name" :value="item.id"></el-option>
                <el-option v-if="form.provider_id === -1" label="" :value="-1" disabled style="display:none"></el-option>
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="模型 ID">
              <el-input v-model="form.model_id" placeholder="如 gpt-4o"></el-input>
            </el-form-item>
          </el-col>
        </el-row>
        
        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="Temperature">
              <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" style="width: 100%"></el-input-number>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Top P">
              <el-input-number v-model="form.top_p" :min="0" :max="1" :step="0.05" style="width: 100%"></el-input-number>
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="最大 Token">
              <el-input-number v-model="form.max_tokens" :min="0" style="width: 100%"></el-input-number>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="上下文限制 K">
              <el-input-number v-model="form.context_window_k" :min="1" style="width: 100%"></el-input-number>
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item label="启用流式输出">
          <el-switch v-model="form.stream"></el-switch>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false" size="default">取消</el-button>
        <el-button type="primary" @click="submitForm" size="default" :loading="submitting">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import { profileApi, providerApi } from '../api';

export default {
  data() {
    return {
      profiles: [],
      providers: [],
      loading: false,
      dialogVisible: false,
      dialogType: 'create',
      submitting: false,
      form: {
        id: null,
        name: '',
        provider_id: -1,
        model_id: '',
        temperature: 0.7,
        top_p: 1.0,
        max_tokens: 2048,
        stream: false,
        context_window_k: 4,
        extra_config: {}
      }
    };
  },
  methods: {
    async handleRefresh() {
      await Promise.all([this.loadProfiles(), this.fetchProviders()]);
      this.$message.success('数据已更新');
    },
    
    async fetchProviders() {
      try {
        const res = await providerApi.list();
        this.providers = res.data.data;
      } catch (err) {
        this.$message.error(err.message || '加载提供商失败');
      }
    },

    async loadProfiles() {
      this.loading = true;
      try {
        const res = await profileApi.list();
        // 根据用户提供的 JSON 结构，数据可能是直接的数组
        const data = res.data;
        this.profiles = res.data.data || [];
      } catch (err) {
        this.$message.error(err.message || '加载列表失败');
      } finally {
        this.loading = false;
      }
    },
    async handleActivate(id) {
      try {
        const res = await profileApi.activate(id);
        this.$message.success(res.data.message || '已激活');
        this.loadProfiles();
    this.fetchProviders();
      } catch (err) {
        this.$message.error(err.message || '激活失败');
      }
    },
    async handleDelete(id) {
      try {
        await this.$confirm('确定要删除吗？', '提示', { type: 'warning' });
        await profileApi.delete(id);
        this.$message.success('已删除');
        this.loadProfiles();
    this.fetchProviders();
      } catch (err) {
        if (err !== 'cancel') this.$message.error('删除失败');
      }
    },
    showDialog(type, row = null) {
      this.dialogType = type;
      if (type === 'edit' && row) {
        this.form = { ...row };
      } else {
        this.form = { id: null, name: '', provider_id: -1, model_id: '', temperature: 0.7, top_p: 1.0, max_tokens: 2048, stream: false, context_window_k: 4, extra_config: {} };
      }
      this.dialogVisible = true;
    },
    
    async submitForm() {
      let res;
      if (!this.form.provider_id || this.form.provider_id === -1) {
        this.$message.warning('请先选择模型提供商');
        return;
      }
      if (!this.form.name) {
        this.$message.warning('请输入配置名称');
        return;
      }
      this.submitting = true;

      try {
        if (this.dialogType === 'create') {
          res = await profileApi.create(this.form);
        } else {
          res = await profileApi.update(this.form.id, this.form);
        }
        this.$message.success(res.data.message || '保存成功');
        this.dialogVisible = false;
        this.loadProfiles();
    this.fetchProviders();
      } catch (err) {
        this.$message.error(err.message || '保存失败');
      } finally {
        this.submitting = false;
      }
    }
  },
  mounted() {
    this.loadProfiles();
    this.fetchProviders();
  }
}
</script>

<style lang="scss">
@import "@/assets/css/profiles.scss";
</style>
