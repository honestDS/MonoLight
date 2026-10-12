import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import {
  computed,
  effectScope,
  nextTick,
  reactive,
  watch,
} from 'vue'

const profileFormDialogSource = readFileSync(
  new URL('../src/components/ProfileFormDialog.vue', import.meta.url),
  'utf8',
)
const profileFormDialogScriptMatch = profileFormDialogSource.match(
  /<script\s+setup(?:\s[^>]*)?>([\s\S]*?)<\/script>/,
)
if (!profileFormDialogScriptMatch) throw new Error('ProfileFormDialog.vue script setup was not found')

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

const createHarness = ({
  dialogType = 'create',
  dialogVisible = true,
  uid = 'user-a',
  promptId = 'prompt-a',
  prompts = [],
} = {}) => {
  const form = reactive({ uid, prompt_id: promptId })
  const props = reactive({ dialogType, dialogVisible, form, prompts })
  const context = vm.createContext({
    computed,
    reactive,
    watch,
    effectScope,
    nextTick,
    defineProps: () => props,
    defineEmits: () => () => {},
    console: { log() {}, warn() {}, error() {} },
  })
  const scope = effectScope()
  let module

  scope.run(() => {
    vm.runInContext(
      `${stripImports(profileFormDialogScriptMatch[1])}
globalThis.__module = { ownerPromptOptions, props }
`,
      context,
      { filename: 'ProfileFormDialog.vue?script-setup' },
    )
    module = context.__module
  })

  return {
    form,
    props,
    ownerPromptOptions: module.ownerPromptOptions,
    async flush() {
      await nextTick()
      await Promise.resolve()
    },
    stop() {
      scope.stop()
    },
  }
}

const optionIds = harness => Array.from(harness.ownerPromptOptions.value, item => item.id)

const prompts = [
  { id: 'prompt-a', uid: 'user-a', name: 'Prompt A' },
  { id: 'prompt-b', uid: 'user-b', name: 'Prompt B' },
  { id: 'prompt-null', uid: null, name: 'Shared Prompt' },
]

test('filters prompt options by the current owner and returns none without a uid', async t => {
  const harness = createHarness({ prompts })
  t.after(() => harness.stop())

  assert.deepEqual(optionIds(harness), ['prompt-a'])

  harness.form.uid = null
  await harness.flush()

  assert.deepEqual(optionIds(harness), [])
})

test('clears stale prompt ids only when an active creation changes owner', async t => {
  const activeCreate = createHarness({ prompts })
  const sameOwner = createHarness({ prompts, promptId: 'prompt-a' })
  const validNewOwner = createHarness({ prompts, promptId: 'prompt-b' })
  const clearOwner = createHarness({ prompts, uid: 'user-b', promptId: 'prompt-b' })
  t.after(() => {
    activeCreate.stop()
    sameOwner.stop()
    validNewOwner.stop()
    clearOwner.stop()
  })

  activeCreate.form.uid = 'user-b'
  await activeCreate.flush()
  assert.equal(activeCreate.form.prompt_id, null)

  sameOwner.form.uid = 'user-a'
  await sameOwner.flush()
  assert.equal(sameOwner.form.prompt_id, 'prompt-a')

  validNewOwner.form.uid = 'user-b'
  await validNewOwner.flush()
  assert.equal(validNewOwner.form.prompt_id, 'prompt-b')

  clearOwner.form.uid = null
  await clearOwner.flush()
  assert.equal(clearOwner.form.prompt_id, null)
})

test('preserves bindings outside visible creation, including async edit prompt loading', async t => {
  const edit = createHarness({ dialogType: 'edit', prompts, promptId: 'prompt-a' })
  const hiddenCreate = createHarness({ dialogVisible: false, prompts, promptId: 'prompt-a' })
  const asyncEdit = createHarness({ dialogType: 'edit', prompts: [], promptId: 'prompt-a' })
  t.after(() => {
    edit.stop()
    hiddenCreate.stop()
    asyncEdit.stop()
  })

  edit.form.uid = 'user-b'
  hiddenCreate.form.uid = 'user-b'
  await Promise.all([edit.flush(), hiddenCreate.flush()])
  assert.equal(edit.form.prompt_id, 'prompt-a')
  assert.equal(hiddenCreate.form.prompt_id, 'prompt-a')

  assert.deepEqual(optionIds(asyncEdit), [])
  asyncEdit.props.prompts = [{ id: 'prompt-a', uid: 'user-a', name: 'Prompt A' }]
  await asyncEdit.flush()
  assert.deepEqual(optionIds(asyncEdit), ['prompt-a'])
  assert.equal(asyncEdit.form.prompt_id, 'prompt-a')
})
