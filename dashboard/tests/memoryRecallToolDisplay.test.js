import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const dashboardRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const readSource = relativePath => readFileSync(resolve(dashboardRoot, relativePath), 'utf8')

test('memory recall tool uses a user-facing context recall title in both locales', () => {
  const utilsSource = readSource('src/utils/index.js')
  const zhSource = readSource('src/i18n/locales/zh/common.js')
  const enSource = readSource('src/i18n/locales/en/common.js')

  assert.match(
    utilsSource,
    /name === 'manage_memory_and_knowledge'[\s\S]*operation === 'recall'[\s\S]*common\.context_recall/
  )
  assert.match(zhSource, /context_recall:\s*'上下文召回'/)
  assert.match(enSource, /context_recall:\s*'Context Recall'/)
})
