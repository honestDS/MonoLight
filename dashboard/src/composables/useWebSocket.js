// WebSocket 管理：连接、心跳与自动重连
import { onUnmounted } from 'vue'
import { chatApi } from '../api'
import { ElMessage } from 'element-plus'
import i18n from '../i18n'
import { createWebSocketConnection } from './webSocketConnection.js'

const t = (key, ...args) => i18n.global.t(key, ...args)

export function useWebSocket() {
    const manager = createWebSocketConnection({
        createSocket: token => chatApi.createWebSocket(token),
        translate: t,
        warn: message => ElMessage.warning(message)
    })
    onUnmounted(manager.dispose)
    return manager
}
