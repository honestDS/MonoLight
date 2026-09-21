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

test('new message indicator is horizontally centered instead of right aligned', async () => {
  const chatStyles = await readDashboardSource('src/assets/css/chat.scss')
  const indicatorStart = chatStyles.indexOf('.new-message-indicator {')

  assert.notEqual(indicatorStart, -1)

  const indicatorPositioning = chatStyles.slice(indicatorStart, indicatorStart + 260)
  assert.match(indicatorPositioning, /left:\s*50%;/)
  assert.match(indicatorPositioning, /margin-left:\s*-21px;/)
  assert.doesNotMatch(indicatorPositioning, /right:\s*18px;/)
})
