<template>
  <el-dialog
    :model-value="visible"
    :title="$t('messagePlatforms.qrcode_title')"
    width="520px"
    @update:model-value="emit('update:visible', $event)"
    @closed="emit('closed')">
    <div v-if="loginData" class="qrcode-box">
      <p class="text-muted">{{ $t('messagePlatforms.qrcode_tip') }}</p>
      <img v-if="qrcodeImageUrl" :src="qrcodeImageUrl" alt="qrcode" class="qrcode-image" />
      <el-input :model-value="loginData.qrcode" readonly>
        <template #append>
          <el-button @click="emit('copy')">{{ $t('common.copy') }}</el-button>
        </template>
      </el-input>
      <div class="qrcode-actions">
        <el-button type="primary" :loading="checkingLogin" @click="emit('refresh-status')">{{ $t('messagePlatforms.refresh_status') }}</el-button>
      </div>
    </div>
  </el-dialog>
</template>

<script setup>
defineProps({
  visible: { type: Boolean, default: false },
  loginData: { type: Object, default: null },
  qrcodeImageUrl: { type: String, default: '' },
  checkingLogin: { type: Boolean, default: false }
})

const emit = defineEmits(['update:visible', 'closed', 'copy', 'refresh-status'])
</script>

<style lang="scss" scoped>
.qrcode-box {
  text-align: center;
}

.qrcode-image {
  width: 260px;
  height: 260px;
  margin: 16px auto;
  display: block;
}

.qrcode-actions {
  margin-top: 16px;
}
</style>
