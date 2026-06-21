// 工具调用解析 composable
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