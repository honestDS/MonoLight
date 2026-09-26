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

test('session list lives in a floating overlay panel instead of a reserved layout column', () => {
  assert.match(
    chatStyles,
    /\.chat-view-container\s*\{[\s\S]*?width:\s*100%;[\s\S]*?gap:\s*0;[\s\S]*?background:\s*var\(--color-white\);[\s\S]*?overflow:\s*hidden;/
  )
  assert.match(
    chatStyles,
    /\.chat-main\s*\{[\s\S]*?border:\s*none;[\s\S]*?border-radius:\s*var\(--radius-none\);[\s\S]*?box-shadow:\s*none;/
  )
  assert.doesNotMatch(chatStyles, /\.sessions-sidebar\s*\{/)
  assert.doesNotMatch(chatViewSource, /class="sessions-sidebar"/)
  assert.match(
    chatStyles,
    /\.chat-side-controls\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?top:\s*10px;[\s\S]*?left:\s*12px;/
  )
  assert.match(
    chatStyles,
    /\.sessions-panel\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?max-height:\s*min\(72cqh, 620px\);[\s\S]*?&\.glass-surface\s*\{[\s\S]*?border-radius:\s*var\(--radius-14\);/
  )
})

test('sessions overlay panel opens from the floating trigger and closes on selection or outside interaction', () => {
  assert.match(chatViewSource, /v-click-outside="closeSessionsPanel"/)
  assert.match(chatViewSource, /v-show="sessionsPanelOpen"/)
  assert.match(chatViewSource, /:aria-expanded="sessionsPanelOpen"/)
  assert.match(chatViewSource, /const handleSelectSession = \(session\) => \{\s*closeSessionsPanel\(\)/)
  assert.match(chatViewSource, /const handleCreateNewSession = \(\) => \{\s*closeSessionsPanel\(\)/)
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
