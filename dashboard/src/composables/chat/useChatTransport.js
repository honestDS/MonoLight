// 聊天通信 composable：封装 HTTP 与 WebSocket 通信
import { ElMessage } from 'element-plus'
import { chatApi } from '../../api'
import { useWebSocket } from '../useWebSocket'
import i18n from '../../i18n'
import { createChatTransport } from './chatTransportRuntime.js'

const t = (key, ...args) => i18n.global.t(key, ...args)

export function useChatTransport() {
  return createChatTransport({
    wsManager: useWebSocket(),
    api: chatApi,
    getToken: () => localStorage.getItem('token'),
    reportError: message => ElMessage.error(message),
    translate: t
  })
}
