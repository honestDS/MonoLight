<template>
  <div class="chat-view">
    <div class="message-list" ref="messageList">
      <div v-for="(msg, idx) in messages" :key="idx" :class="['message-item', msg.role]">
        <div class="content">{{ msg.content }}</div>
      </div>
    </div>
    <div class="input-area">
      <el-input v-model="inputMsg" placeholder="输入消息..." @keyup.native.enter="send">
        <el-button #append :loading="loading" @click="send">发送</el-button>
      </el-input>
    </div>
  </div>
</template>

<script>
import { chatApi } from '../api';

export default {
  data() {
    return {
      inputMsg: '',
      messages: [],
      loading: false
    };
  },
  methods: {
    async send() {
      if (!this.inputMsg.trim() || this.loading) return;
      const text = this.inputMsg;
      this.messages.push({ role: 'user', content: text });
      this.inputMsg = '';
      this.loading = true;
      try {
        const res = await chatApi.send(text);
        this.messages.push({ role: 'ai', content: res.data.data.response || '收到' });
        this.$nextTick(() => {
          const el = this.$refs.messageList;
          el.scrollTop = el.scrollHeight;
        });
      } catch (err) {
        this.$message.error('发送失败');
      } finally {
        this.loading = false;
      }
    }
  }
}
</script>

<style lang="scss">@import "@/assets/css/chat.scss";</style>