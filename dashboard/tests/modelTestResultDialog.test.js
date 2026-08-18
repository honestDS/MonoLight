import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const readSource = relativePath => readFileSync(new URL(relativePath, import.meta.url), 'utf8')

const componentSource = readSource('../src/components/ModelTestResultDialog.vue')
const zhChannelsSource = readSource('../src/i18n/locales/zh/channels.js')
const enChannelsSource = readSource('../src/i18n/locales/en/channels.js')

const propsSource = componentSource.slice(
  componentSource.indexOf('const props = defineProps('),
  componentSource.indexOf('const emit = defineEmits('),
)

const sourceSection = (startMarker, endMarker) => {
  const start = componentSource.indexOf(startMarker)
  assert.ok(start >= 0, `missing source marker: ${startMarker}`)
  const end = componentSource.indexOf(endMarker, start)
  assert.ok(end >= 0, `missing source marker: ${endMarker}`)
  return componentSource.slice(start, end)
}

const localeValue = (source, key) => {
  const match = source.match(new RegExp(`^\\s*${key}:\\s*(['"])(.*?)\\1\\s*,?\\s*$`, 'm'))
  return match ? match[2].trim() : ''
}

test('declares visible, results, and activeId props with selection emits', () => {
  assert.match(propsSource, /\bvisible:\s*\{[\s\S]*?\btype:\s*Boolean\b/)
  assert.match(propsSource, /\bresults:\s*\{[\s\S]*?\btype:\s*Array\b/)
  assert.match(propsSource, /\bactiveId:\s*\{[\s\S]*?\btype:\s*String\b/)
  assert.match(
    componentSource,
    /defineEmits\(\[\s*['"]update:visible['"]\s*,\s*['"]update:active-id['"]\s*\]\)/,
  )
})

test('uses tabs for multiple results and synchronizes the active id', () => {
  assert.match(componentSource, /<el-tabs\s+\n?\s*v-if="normalizedResults\.length > 1"/)
  assert.match(componentSource, /:model-value="activeResultId"/)
  assert.match(componentSource, /@update:model-value="handleActiveIdChange"/)
  assert.match(
    componentSource,
    /normalizedResults\.value\.find\(result => result\.id === activeResultId\.value\)[\s\S]*?normalizedResults\.value\[0\]/,
  )
  assert.match(
    componentSource,
    /const syncActiveResult = \(\) => \{[\s\S]*?props\.activeId[\s\S]*?results\.some\(result => result\.id === requestedId\)[\s\S]*?results\[0\]\?\.id[\s\S]*?activeResultId\.value = nextId[\s\S]*?emit\('update:active-id', nextId\)/,
  )
  assert.match(
    componentSource,
    /watch\(\s*\[normalizedResults,\s*\(\) => props\.activeId\],\s*syncActiveResult,\s*\{\s*immediate:\s*true\s*\}\s*\)/,
  )
  assert.match(
    componentSource,
    /const handleActiveIdChange = value => \{[\s\S]*?activeResultId\.value = nextId[\s\S]*?emit\('update:active-id', nextId\)/,
  )
})

test('contains running, error, and success result branches', () => {
  assert.match(componentSource, /v-if="activeState\.status === 'running'"/)
  assert.match(componentSource, /v-else-if="activeState\.status === 'error'"/)
  assert.match(componentSource, /v-else-if="activeState\.status === 'success'"/)
  assert.match(componentSource, /channels\.model_test_running/)
  assert.match(componentSource, /channels\.model_test_failed/)
  assert.match(componentSource, /channels\.model_test_success/)
})

test('renders CHAT reply, usage, mode, and latency fields', () => {
  const chatBlock = sourceSection(
    '<template v-if="activeState.kind === \'CHAT\'">',
    '</template>',
  )

  assert.match(chatBlock, /activeData\.reply/)
  assert.match(chatBlock, /formatUsage\(activeData\.usage\)/)
  assert.match(chatBlock, /getTestModeLabel\(activeState\.testMode\)/)
  assert.match(chatBlock, /formatLatency\(/)
  assert.match(chatBlock, /activeData\.latency_ms/)
  assert.match(chatBlock, /channels\.chat_test_reply/)
  assert.match(chatBlock, /channels\.chat_test_usage/)
  assert.match(chatBlock, /channels\.chat_test_mode/)
  assert.match(chatBlock, /channels\.chat_test_latency|channels\.chat_test_first_char_latency/)
})

test('renders IMAGE_GENERATION URLs and base64 image data', () => {
  const imageBlock = sourceSection(
    '<template v-else-if="activeState.kind === \'IMAGE_GENERATION\'">',
    '</template>',
  )
  const imageHelper = sourceSection('const getTestImageUrl = data => {', '</script>')

  assert.match(imageBlock, /<img[\s\S]*:src="getTestImageUrl\(activeData\)"/)
  assert.match(imageHelper, /typeof image\.url === 'string'/)
  assert.match(imageHelper, /typeof image\.b64_json === 'string'/)
  assert.match(imageHelper, /data:image\/png;base64/)
})

test('uses an i18n image alt and has no hard-coded static alt text', () => {
  const imageTag = componentSource.match(/<img\b[\s\S]*?\/>/)?.[0] || ''

  assert.match(imageTag, /:alt="t\('channels\.image_generation_test_result_title'\)"/)
  assert.doesNotMatch(imageTag, /(?:^|\s)alt\s*=\s*["'][^"']+["']/i)
})

test('Chinese and English channels locales have non-empty model result labels', () => {
  for (const [locale, source] of [['zh', zhChannelsSource], ['en', enChannelsSource]]) {
    for (const key of ['model_test_view_result', 'model_test_result']) {
      const value = localeValue(source, key)
      assert.ok(value.length > 0, `${locale} channels.${key} must be non-empty`)
    }
  }
})
