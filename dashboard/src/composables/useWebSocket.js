// WebSocket 管理：连接、心跳与自动重连
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
    let connectionGeneration = 0
    let reconnectPending = false
    let connectionPromise = null
    let connectionReject = null
    let reconnectTimer = null

    const clearReconnectTimer = () => {
        if (reconnectTimer === null) return
        clearTimeout(reconnectTimer)
        reconnectTimer = null
    }
    
    // 连接 WebSocket
    const connect = (token, { preserveReconnectAttempts = false } = {}) => {
        if (ws.value && ws.value.readyState === WebSocket.OPEN && isConnected.value) {
            return Promise.resolve()
        }
        if (connectionPromise) {
            return connectionPromise
        }

        clearReconnectTimer()
        if (!preserveReconnectAttempts) {
            reconnectAttempts.value = 0
        }
        storedToken = token
        const generation = ++connectionGeneration
        let resolveConnection
        let rejectConnection
        const pendingConnection = new Promise((resolve, reject) => {
            resolveConnection = resolve
            rejectConnection = reject
        })
        connectionPromise = pendingConnection
        connectionReject = rejectConnection

        let socket
        try {
            socket = chatApi.createWebSocket(token)
        } catch (error) {
            connectionPromise = null
            connectionReject = null
            rejectConnection(error)
            return pendingConnection
        }
        ws.value = socket

        socket.onopen = () => {
            if (generation !== connectionGeneration || ws.value !== socket) return
            console.log('WebSocket connected')
            isConnected.value = true
            reconnectAttempts.value = 0  // 重置重连计数
            if (reconnectPending) {
                reconnectPending = false
                messageHandlers.forEach(handler => handler({ type: 'connection_reopened' }))
            }
            if (connectionPromise === pendingConnection) {
                connectionPromise = null
                connectionReject = null
            }
            resolveConnection()
        }

        socket.onmessage = (event) => {
            if (generation !== connectionGeneration || ws.value !== socket) return
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

        socket.onerror = (error) => {
            if (generation !== connectionGeneration || ws.value !== socket) return
            console.error('WebSocket error:', error)
            isConnected.value = false
            if (connectionPromise === pendingConnection) {
                connectionPromise = null
                connectionReject = null
            }
            rejectConnection(error)
        }

        socket.onclose = (event) => {
            if (generation !== connectionGeneration || ws.value !== socket) return
            console.log('WebSocket closed', event.code, event.reason)
            isConnected.value = false
            if (connectionPromise === pendingConnection) {
                connectionPromise = null
                connectionReject = null
                rejectConnection(event)
            }
            // 检查是否是正常关闭 (code 1000 = CLOSE_NORMAL, 1001 = GOING_AWAY)
            const isNormalClose = event.code === 1000 || event.code === 1001
            if (!isNormalClose) {
                reconnectPending = true
            }
            // 任何由远端触发、且仍属于当前 generation 的关闭都必须同步给上层。
            // 手动 disconnect 会先推进 generation，因此不会走到这里。
            messageHandlers.forEach(handler => handler({ type: 'connection_closed' }))
            // 检查是否是用户主动断开
            const isUserInitiated = reconnectAttempts.value >= MAX_RECONNECT_ATTEMPTS
            // 自动重连逻辑（非正常关闭且非用户主动断开）
            if (!isNormalClose && !isUserInitiated && storedToken) {
                reconnectAttempts.value++
                console.log(`WebSocket reconnecting... attempt ${reconnectAttempts.value}`)
                reconnectTimer = setTimeout(() => {
                    reconnectTimer = null
                    if (generation !== connectionGeneration || !storedToken) return
                    void connect(storedToken, { preserveReconnectAttempts: true }).catch(() => {})
                }, RECONNECT_INTERVAL)
            } else if (isUserInitiated && !isNormalClose) {
                // 只有在非正常关闭且重连失败时才显示错误
                ElMessage.warning(t('common.ws_disconnected'))
            }
        }
        return pendingConnection
    }
    
    // 断开连接
    const disconnect = () => {
        clearReconnectTimer()
        if (connectionPromise) {
            const rejectConnection = connectionReject
            connectionPromise = null
            connectionReject = null
            rejectConnection?.(t('common.ws_disconnected'))
        }
        storedToken = ''
        reconnectPending = false
        connectionGeneration += 1
        reconnectAttempts.value = MAX_RECONNECT_ATTEMPTS  // 阻止自动重连
        if (ws.value) {
            const socket = ws.value
            ws.value = null
            socket.close(1000, 'User initiated close')  // 正常关闭
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
