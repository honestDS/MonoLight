/**
 * WebSocket 管理模块
 * 提供连接管理、心跳保活、自动重连等能力
 */
import { ref, onUnmounted } from 'vue'
import { chatApi } from '../api'
import { ElMessage } from 'element-plus'
import i18n from '../i18n'

const t = (key, ...args) => i18n.global.t(key, ...args)

export function useWebSocket() {
    const ws = ref(null)
    const isConnected = ref(false)
    const reconnectAttempts = ref(0)
    const MAX_RECONNECT_ATTEMPTS = 5
    const RECONNECT_INTERVAL = 3000
    
    // 消息回调
    let messageHandlers = []
    // 存储 token 用于重连
    let storedToken = ''
    
    // 连接 WebSocket
    const connect = (token) => {
        return new Promise((resolve, reject) => {
            storedToken = token
            ws.value = chatApi.createWebSocket(token)
            
            ws.value.onopen = () => {
                console.log('WebSocket connected')
                isConnected.value = true
                reconnectAttempts.value = 0  // 重置重连计数
                resolve()
            }
            
            ws.value.onmessage = (event) => {
                try {
                    // 尝试解析 JSON
                    const data = JSON.parse(event.data)
                    messageHandlers.forEach(handler => handler(data))
                } catch (e) {
                    // 如果不是 JSON，当作文本处理
                    console.log('WebSocket text message:', event.data)
                    messageHandlers.forEach(handler => handler({ type: 'raw', data: event.data }))
                }
            }
            
            ws.value.onerror = (error) => {
                console.error('WebSocket error:', error)
                isConnected.value = false
                reject(error)
            }
            
            ws.value.onclose = (event) => {
                console.log('WebSocket closed', event.code, event.reason)
                isConnected.value = false
                // 检查是否是正常关闭 (code 1000 = CLOSE_NORMAL, 1001 = GOING_AWAY)
                const isNormalClose = event.code === 1000 || event.code === 1001
                // 通知上层连接已断开（如果有回调）
                if (!isNormalClose) {
                    messageHandlers.forEach(handler => handler({ type: 'connection_closed' }))
                }
                // 检查是否是用户主动断开
                const isUserInitiated = reconnectAttempts.value >= MAX_RECONNECT_ATTEMPTS
                // 自动重连逻辑（非正常关闭且非用户主动断开）
                if (!isNormalClose && !isUserInitiated && storedToken) {
                    reconnectAttempts.value++
                    console.log(`WebSocket reconnecting... attempt ${reconnectAttempts.value}`)
                    setTimeout(() => {
                        connect(storedToken)
                    }, RECONNECT_INTERVAL)
                } else if (isUserInitiated && !isNormalClose) {
                    // 只有在非正常关闭且重连失败时才显示错误
                    ElMessage.warning(t('common.ws_disconnected'))
                }
            }
        })
    }
    
    // 断开连接
    const disconnect = () => {
        storedToken = ''
        reconnectAttempts.value = MAX_RECONNECT_ATTEMPTS  // 阻止自动重连
        if (ws.value) {
            ws.value.close(1000, 'User initiated close')  // 正常关闭
            ws.value = null
        }
        isConnected.value = false
    }
    
    // 发送消息
    const sendMessage = (data) => {
        if (ws.value && ws.value.readyState === WebSocket.OPEN) {
            ws.value.send(JSON.stringify(data))
            return true
        }
        console.warn('WebSocket not connected, message not sent')
        return false
    }
    
    // 注册消息处理
    const onMessage = (handler) => {
        // 避免重复注册相同的 handler
        if (!messageHandlers.includes(handler)) {
            messageHandlers.push(handler)
        }
        // 返回取消注册函数
        return () => {
            messageHandlers = messageHandlers.filter(h => h !== handler)
        }
    }
    
    // 心跳保活
    let heartbeatInterval = null
    const startHeartbeat = () => {
        stopHeartbeat()  // 先停止可能存在的心跳
        heartbeatInterval = setInterval(() => {
            if (ws.value && ws.value.readyState === WebSocket.OPEN) {
                ws.value.send(JSON.stringify({ type: 'ping' }))
            }
        }, 30000)
    }
    
    const stopHeartbeat = () => {
        if (heartbeatInterval) {
            clearInterval(heartbeatInterval)
            heartbeatInterval = null
        }
    }
    
    // 组件卸载时清理
    onUnmounted(() => {
        stopHeartbeat()
        disconnect()
    })
    
    return {
        ws,
        isConnected,
        reconnectAttempts,
        connect,
        disconnect,
        sendMessage,
        onMessage,
        startHeartbeat,
        stopHeartbeat
    }
}