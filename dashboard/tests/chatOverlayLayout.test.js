import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const dashboardDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

async function readDashboardSource(relativePath) {
  return readFile(path.join(dashboardDirectory, relativePath), 'utf8')
}

test('in-progress todo icon uses a defined blue theme color', async () => {
  const [todoStyles, themeStyles] = await Promise.all([
    readDashboardSource('src/assets/css/SessionTodoPanel.scss'),
    readDashboardSource('src/assets/css/theme.scss')
  ])

  assert.match(
    todoStyles,
    /\.session-todo-item[\s\S]*?&\.is-in_progress[\s\S]*?\.session-todo-status-icon\s*\{[\s\S]*?color:\s*var\(--color-sky-500\);/
  )
  assert.match(
    themeStyles,
    /--color-sky-500:\s*rgb\(var\(--color-sky-500-rgb\)\);/
  )
})

test('expanded todo drawer adds an elevated shadow above the shared glass surface', async () => {
  const todoStyles = await readDashboardSource('src/assets/css/SessionTodoPanel.scss')

  assert.match(
    todoStyles,
    /\.session-todo-drawer\s*\{[\s\S]*?box-shadow:\s*var\(--glass-surface-shadow\),\s*0 18px 44px rgba\(var\(--color-slate-900-rgb\),\s*0\.14\);/
  )
})

test('new message indicator is horizontally centered instead of right aligned', async () => {
  const chatStyles = await readDashboardSource('src/assets/css/chat.scss')
  const indicatorStart = chatStyles.indexOf('.new-message-indicator {')

  assert.notEqual(indicatorStart, -1)

  const indicatorPositioning = chatStyles.slice(indicatorStart, indicatorStart + 260)
  assert.match(indicatorPositioning, /left:\s*50%;/)
  assert.match(indicatorPositioning, /margin-left:\s*-21px;/)
  assert.doesNotMatch(indicatorPositioning, /right:\s*18px;/)
})

test('special chat message surfaces use the shared chat radius while normal bubbles keep their shape', async () => {
  const [chatStyles, chatViewStyles, thinkingStyles] = await Promise.all([
    readDashboardSource('src/assets/css/chat.scss'),
    readDashboardSource('src/assets/css/ChatView.scss'),
    readDashboardSource('src/assets/css/ThinkingBlock.scss')
  ])

  assert.match(chatStyles, /&\.user\s*\{[\s\S]*?border-radius:\s*var\(--radius-16\) var\(--radius-16\) var\(--radius-5\) var\(--radius-16\);/)
  assert.match(chatStyles, /&\.ai\s*\{[\s\S]*?border-radius:\s*var\(--radius-16\) var\(--radius-16\) var\(--radius-16\) var\(--radius-5\);/)
  assert.match(chatStyles, /&\.background-system\s*\{[\s\S]*?\.content\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/)
  assert.match(
    chatStyles,
    /&\.error\s*\{[\s\S]*?\.content\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/
  )
  assert.match(chatStyles, /&\.ai \.content\.tool-call-message\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/)
  assert.match(chatStyles, /\.tool-round-collapse\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/)
  assert.match(
    chatViewStyles,
    /\.message-item\.guidance \.guidance-card\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/
  )
  assert.match(chatViewStyles, /\.audit-confirmation-card\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/)
  assert.match(thinkingStyles, /\.thinking-block\s*\{[\s\S]*?border-radius:\s*var\(--radius-chat\);/)
})

test('app sidebar menu states mirror session item geometry using sidebar palette tokens', async () => {
  const [appStyles, themeStyles] = await Promise.all([
    readDashboardSource('src/assets/css/app.scss'),
    readDashboardSource('src/assets/css/theme.scss')
  ])

  assert.match(appStyles, /\.el-menu-item,\s*\n\s*\.el-sub-menu__title\s*\{[\s\S]*?margin:\s*3px 8px;[\s\S]*?border-radius:\s*var\(--radius-6\);/)
  assert.match(appStyles, /\.el-menu-item\.is-active\s*\{[\s\S]*?background:\s*linear-gradient\([\s\S]*?var\(--color-sidebar-active-bg-start\)[\s\S]*?var\(--color-sidebar-active-bg-end\)[\s\S]*?\) !important;/)
  assert.match(appStyles, /\.el-menu-item:hover\s*\{[\s\S]*?background-color:\s*var\(--color-sidebar-hover\) !important;/)
  assert.match(appStyles, /\.el-menu-item\.is-active::before[\s\S]*?background:\s*var\(--color-sidebar-active\);[\s\S]*?box-shadow:\s*0 0 12px var\(--color-sidebar-active-shadow\);/)
  assert.match(themeStyles, /--color-sidebar-active-bg-start:\s*rgba\(var\(--color-blue-500-rgb\),\s*0\.12\);/)
  assert.match(themeStyles, /--color-sidebar-active-bg-end:\s*rgba\(var\(--color-blue-500-rgb\),\s*0\.05\);/)
})

test('sidebar collapse button uses sidebar palette without border or shadow', async () => {
  const appStyles = await readDashboardSource('src/assets/css/app.scss')

  assert.match(
    appStyles,
    /\.sidebar-collapse-button\s*\{[\s\S]*?color:\s*var\(--color-sidebar-text\);[\s\S]*?border:\s*none;[\s\S]*?box-shadow:\s*none;/
  )
  assert.match(
    appStyles,
    /\.sidebar-collapse-button:hover\s*\{[\s\S]*?color:\s*var\(--color-sidebar-active\);/
  )
})

test('session item states use the same palette as the app sidebar', async () => {
  const chatStyles = await readDashboardSource('src/assets/css/chat.scss')

  assert.match(chatStyles, /\.session-item\s*\{[\s\S]*?&:hover\s*\{[\s\S]*?background:\s*var\(--color-sidebar-hover\);/)
  assert.match(chatStyles, /&\.active\s*\{[\s\S]*?var\(--color-sidebar-active-bg-start\)[\s\S]*?var\(--color-sidebar-active-bg-end\)/)
  assert.match(chatStyles, /&\.active\s*\{[\s\S]*?&::before\s*\{[\s\S]*?background:\s*var\(--color-sidebar-active\);[\s\S]*?box-shadow:\s*0 0 12px var\(--color-sidebar-active-shadow\);/)
  assert.match(chatStyles, /\.session-title\s*\{[\s\S]*?color:\s*var\(--color-sidebar-text\);/)
  assert.match(chatStyles, /&\.active\s*\{[\s\S]*?\.session-title\s*\{[\s\S]*?color:\s*var\(--color-sidebar-active-text\);/)
})

test('app copyright lives in the sidebar footer without a standalone footer surface', async () => {
  const [appSource, appStyles] = await Promise.all([
    readDashboardSource('src/App.vue'),
    readDashboardSource('src/assets/css/app.scss')
  ])

  assert.doesNotMatch(appSource, /class="app-footer"/)
  assert.match(
    appSource,
    /class="sidebar-footer"[\s\S]*?class="sidebar-copyright"[\s\S]*?2026 MonoLight LLM Admin\. All rights reserved\./
  )
  assert.doesNotMatch(appStyles, /\.app-footer\s*\{/)

  const copyrightRule = appStyles.match(/\.sidebar-copyright\s*\{([\s\S]*?)\}/)?.[1] || ''
  assert.match(copyrightRule, /color:\s*var\(--color-sidebar-text\);/)
  assert.doesNotMatch(copyrightRule, /background(?:-color)?:/)
})
