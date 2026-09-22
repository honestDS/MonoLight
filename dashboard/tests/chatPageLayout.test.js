import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const appStyles = readFileSync(
  new URL('../src/assets/css/app.scss', import.meta.url),
  'utf8'
)
const chatStyles = readFileSync(
  new URL('../src/assets/css/chat.scss', import.meta.url),
  'utf8'
)
const chatViewSource = readFileSync(
  new URL('../src/views/ChatView.vue', import.meta.url),
  'utf8'
)

test('chat page fills app main without a reserved scrollbar gutter', () => {
  const chatMainRule = appStyles.match(/\.el-main\.app-main\.app-main--chat\s*\{([\s\S]*?)\}/)?.[1] || ''

  assert.match(chatMainRule, /padding:\s*0;/)
  assert.match(chatMainRule, /overflow:\s*hidden;/)
  assert.doesNotMatch(chatMainRule, /scrollbar-gutter:\s*stable;/)
})

test('session list and conversation share one container with a light divider', () => {
  assert.match(
    chatStyles,
    /\.chat-view-container\s*\{[\s\S]*?width:\s*100%;[\s\S]*?gap:\s*0;[\s\S]*?background:\s*var\(--color-white\);[\s\S]*?overflow:\s*hidden;/
  )
  assert.match(
    chatStyles,
    /\.sessions-sidebar\s*\{[\s\S]*?border:\s*none;[\s\S]*?border-right:\s*1px solid rgba\(var\(--color-slate-400-rgb\),\s*0\.14\);[\s\S]*?border-radius:\s*var\(--radius-none\);[\s\S]*?box-shadow:\s*none;/
  )
  assert.match(
    chatStyles,
    /\.chat-main\s*\{[\s\S]*?border:\s*none;[\s\S]*?border-radius:\s*var\(--radius-none\);[\s\S]*?box-shadow:\s*none;/
  )
})

test('session groups animate measured height for both collapse and expand', () => {
  assert.match(chatViewSource, /<Transition[\s\S]*?@enter="handleSessionGroupEnter"[\s\S]*?@leave="handleSessionGroupLeave"/)
  assert.match(chatViewSource, /v-show="!collapsedGroups\.has\(group\.key\)"[\s\S]*?class="session-group-body"/)
  assert.match(chatViewSource, /const handleSessionGroupEnter = \(element\) => \{[\s\S]*?element\.scrollHeight/)
  assert.match(chatViewSource, /const handleSessionGroupLeave = \(element\) => \{[\s\S]*?element\.style\.height = '0px'/)
  assert.match(
    chatStyles,
    /\.session-group-body\s*\{[\s\S]*?overflow:\s*hidden;[\s\S]*?transition:\s*height 0\.28s ease, opacity 0\.2s ease;/
  )
})
