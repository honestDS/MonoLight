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
    </div>
  </el-dialog>
</template>

<script setup>
defineProps({
  visible: { type: Boolean, default: false },
  loginData: { type: Object, default: null },
  qrcodeImageUrl: { type: String, default: '' }
})

const emit = defineEmits(['update:visible', 'closed'])
</script>

<style lang="scss" scoped>
@import "@/assets/css/WeixinOcLoginDialog.scss";
</style>
