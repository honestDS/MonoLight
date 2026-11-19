import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import * as assistantResponseIdentity from '../../../src/utils/assistantResponseIdentity.js'
import * as toolResultState from '../../../src/utils/toolResultState.js'
import { truncateErrorMessage } from '../../../src/utils/errorMessage.js'
import { resolveChatActivityNotice } from '../../../src/utils/chatPresentation.js'
import { MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME } from '../../../src/constants/index.js'

const stripImports = sourceText => {
  const keptLines = []
  let inImport = false

  for (const line of sourceText.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*;?\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const stripExports = sourceText => sourceText
  .replace(/export\s*\{[\s\S]*?\}\s*;?/g, '')
  .replace(/\bexport\s+default\s+/g, '')
  .replace(/\bexport\s+(?=(?:async\s+)?function\b|const\b|let\b|var\b|class\b)/g, '')

const loadChatMessagePresentation = () => {
  const context = vm.createContext({
    ...assistantResponseIdentity,
    ...toolResultState,
    computed,
    MarkdownIt,
    hljs,
    truncateErrorMessage,
    resolveChatActivityNotice,
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
    i18n: { global: { t: key => key } }
  })
  const utilsSource = readFileSync(new URL('../../../src/utils/index.js', import.meta.url), 'utf8')
  const utilsBody = stripExports(stripImports(utilsSource))
  vm.runInContext(`(() => {
    ${utilsBody}
    globalThis.__utils = {
      getToolCallContent,
      getToolCallName,
      getToolCalls,
      getToolResultName,
      isToolCall,
      isToolResult
    }
  })()`, context, { filename: 'src/utils/index.js' })
  Object.assign(context, context.__utils)

  const presentationSource = readFileSync(
    new URL('../../../src/composables/chat/useChatMessagePresentation.js', import.meta.url),
    'utf8'
  )
  const presentationBody = stripExports(stripImports(presentationSource))
  vm.runInContext(`(() => {
    ${presentationBody}
    globalThis.__presentation = { useChatMessagePresentation }
  })()`, context, { filename: 'src/composables/chat/useChatMessagePresentation.js' })
  return context.__presentation.useChatMessagePresentation
}

const useChatMessagePresentation = loadChatMessagePresentation()

const normalizeContent = content => {
  if (typeof content !== 'string') {
    return content === undefined ? null : JSON.parse(JSON.stringify(content))
  }
  try {
    return JSON.parse(content)
  } catch {
    return content
  }
}

const summarizeDisplay = messages => JSON.parse(JSON.stringify(Array.from(messages, message => ({
  id: message.id ?? null,
  type: message.type ?? null,
  role: message.role ?? null,
  response_id: message.response_id ?? null,
  content: normalizeContent(message.content),
  reasoning_content: message.reasoning_content ?? null,
  ...(message.type === 'tool_group' ? {
    pairs: Array.from(message.pairs || [], pair => ({
      id: pair.id ?? null,
      toolCall: pair.toolCall ? {
        id: pair.toolCall.id ?? pair.toolCall.function?.id ?? null,
        name: pair.toolCall.name ?? pair.toolCall.function?.name ?? null,
        arguments: normalizeContent(pair.toolCall.arguments ?? pair.toolCall.function?.arguments)
      } : null,
      resultMessage: pair.resultMessage ? {
        id: pair.resultMessage.id ?? null,
        db_id: pair.resultMessage.db_id ?? null,
        type: pair.resultMessage.type ?? null,
        role: pair.resultMessage.role ?? null,
        response_id: pair.resultMessage.response_id ?? null,
        content: normalizeContent(pair.resultMessage.content)
      } : null
    }))
  } : {})
}))))

const storedToolCall = (id, toolCall, reasoning) => ({
  id,
  created_at: id - 200,
  type: 'tool_call',
  role: 'assistant',
  content: JSON.stringify({ role: 'assistant', tool_calls: [toolCall] }),
  ...(reasoning ? { reasoning_content: reasoning } : {})
})

const storedToolResult = (id, toolCallId, result) => ({
  id,
  created_at: id - 200,
  type: 'tool_result',
  role: 'tool',
  content: JSON.stringify({ role: 'tool', tool_call_id: toolCallId, content: result })
})

export {
  normalizeContent,
  storedToolCall,
  storedToolResult,
  summarizeDisplay,
  useChatMessagePresentation
}
