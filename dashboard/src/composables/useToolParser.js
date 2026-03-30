/**
 * 工具调用解析 composable
 * 提供聊天消息中工具调用的解析功能
 */
import { ref } from 'vue'
import {
  isToolCall,
  isToolResult,
  getToolName,
  getToolArguments,
  getToolResultName,
  getToolResultContent,
  getMessageTimestamp
} from '../utils'

export function useToolParser() {
  const activeCollapse = ref([])

  return {
    activeCollapse,
    isToolCall,
    isToolResult,
    getToolName,
    getToolArguments,
    getToolResultName,
    getToolResultContent,
    getMessageTimestamp
  }
}