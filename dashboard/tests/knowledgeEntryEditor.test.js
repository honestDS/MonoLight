import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { computed, effectScope, reactive, ref, watch } from 'vue'

import {
  canManageManagedKnowledge,
  createManagedKnowledgeDedupeKey,
  getManagedKnowledgeMutationFeedback,
} from '../src/utils/knowledgeBaseManagement.js'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'

const source = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeEntryEditor.js', import.meta.url),
  'utf8',
)

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

const editorScript = `${stripImports(source).replace(/\bexport\s+/g, '')}\nglobalThis.__useKnowledgeEntryEditor = useKnowledgeEntryEditor`
const response = data => ({ data: { data } })
const normalize = value => JSON.parse(JSON.stringify(value))

const deferred = () => {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

const createHarness = (t, options = {}) => {
  const messages = []
  const confirms = []
  const closedConfirmations = []
  const unmountCallbacks = []
  const requests = {
    createEntry: [],
    previewEntryFile: [],
    managedItem: [],
    updateManagedItem: [],
  }
  let active = true
  let disposed = false
  const selectedKb = ref(options.knowledgeBase ?? { id: 'kb-user', knowledge_base_type: 'user' })

  const ElMessage = (...args) => messages.push({ method: 'message', args })
  ElMessage.success = (...args) => messages.push({ method: 'success', args })
  ElMessage.error = (...args) => messages.push({ method: 'error', args })
  ElMessage.warning = (...args) => messages.push({ method: 'warning', args })

  const scope = createKnowledgeBaseScope({
    isActive: () => active,
    confirm: (...args) => {
      confirms.push(args)
      return typeof options.confirm === 'function'
        ? options.confirm(...args)
        : Promise.resolve(options.confirm ?? true)
    },
    closeConfirmations: () => closedConfirmations.push(true),
  })

  const defaultResponses = {
    createEntry: response({ status: 'created' }),
    previewEntryFile: response({ filename: 'source.txt', content: '' }),
    managedItem: response({
      id: 'managed-item',
      knowledge_key: 'managed key',
      content: 'managed content',
      version: 1,
      llm_maintainable: false,
    }),
    updateManagedItem: response({ status: 'updated' }),
  }

  const knowledgeBaseApi = Object.fromEntries(
    Object.keys(requests).map(method => [method, (...args) => {
      const call = { args }
      requests[method].push(call)
      const handler = options.api?.[method]
      return handler
        ? handler(...args, call)
        : Promise.resolve(defaultResponses[method])
    }]),
  )

  const context = vm.createContext({
    AbortController,
    FormData,
    computed,
    reactive,
    ref,
    watch,
    ElMessage,
    knowledgeBaseApi,
    canManageManagedKnowledge,
    createManagedKnowledgeDedupeKey,
    getManagedKnowledgeMutationFeedback,
    onBeforeUnmount: callback => unmountCallbacks.push(callback),
  })
  const vueScope = effectScope()
  const savedResults = []
  const tFunction = options.t ?? (key => key)
  const setup = {
    t: tFunction,
    selectedKb,
    scope,
    onSaved: async result => {
      if (options.onSaved) await options.onSaved(result)
      savedResults.push(normalize(result))
    },
  }

  vueScope.run(() => {
    context.__setup = setup
    vm.runInContext(editorScript, context, { filename: 'useKnowledgeEntryEditor.js' })
    vm.runInContext(
      'globalThis.__editor = globalThis.__useKnowledgeEntryEditor(globalThis.__setup)',
      context,
    )
  })

  const editor = context.__editor
  let validation = async () => true
  editor.formRef.value = {
    validate: () => validation(),
    clearValidate() {},
  }

  const harness = {
    options,
    editor,
    selectedKb,
    scope,
    requests,
    messages,
    confirms,
    closedConfirmations,
    savedResults,
    setValidation(nextValidation) {
      validation = nextValidation
    },
    changeKnowledgeBase(nextKnowledgeBase) {
      scope.invalidate()
      selectedKb.value = null
      selectedKb.value = nextKnowledgeBase
    },
    dispose() {
      if (disposed) return
      disposed = true
      active = false
      scope.invalidate()
      for (const callback of unmountCallbacks) callback()
      vueScope.stop()
    },
  }

  t.after(() => harness.dispose())
  return harness
}

test('USER create trims the name, preserves content and chunks, and reports non-ready indexing', async t => {
  const harness = createHarness(t, {
    knowledgeBase: { id: 'kb-user', knowledge_base_type: 'user', index_status: 'indexing' },
  })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = '  Quarterly notes  '
  editor.form.content = ' \r\nFirst line\r\nLast line \t'
  editor.form.chunk_size = 720
  editor.form.chunk_overlap = 80
  editor.form.batch_size = 24

  await editor.submit()

  assert.equal(harness.requests.createEntry.length, 1)
  assert.equal(harness.requests.createEntry[0].args[0], 'kb-user')
  assert.deepEqual(normalize(harness.requests.createEntry[0].args[1]), {
    name: 'Quarterly notes',
    content: ' \r\nFirst line\r\nLast line \t',
    chunk_size: 720,
    chunk_overlap: 80,
    batch_size: 24,
  })
  assert.ok(harness.messages.some(item => item.method === 'success'
    && item.args[0] === 'knowledgeBase.create_success'))
  assert.ok(!harness.messages.some(item => item.args.includes('knowledgeBase.entries_ready')))
  assert.deepEqual(harness.savedResults, [{ created: true }])
  assert.equal(editor.visible.value, false)
})

test('MANAGED create uses createEntry and reports submitted feedback, not indexed success', async t => {
  const harness = createHarness(t, {
    knowledgeBase: { id: 'kb-managed', knowledge_base_type: 'llm_managed', index_status: 'ready' },
  })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = ' Managed entry '
  editor.form.content = 'Original managed content\r\n'

  await editor.submit()

  assert.equal(harness.requests.createEntry.length, 1)
  assert.deepEqual(normalize(harness.requests.createEntry[0].args[1]), {
    name: 'Managed entry',
    content: 'Original managed content\r\n',
    llm_maintainable: false,
    dedupe_key: harness.requests.createEntry[0].args[1].dedupe_key,
  })
  assert.match(harness.requests.createEntry[0].args[1].dedupe_key, /^managed-ui-create:/)
  assert.ok(harness.messages.some(item => item.method === 'message'
    && item.args[0].message === 'knowledgeBase.entries_submitted'))
  assert.ok(!harness.messages.some(item => item.args.includes('knowledgeBase.entries_ready')))
  assert.deepEqual(harness.savedResults, [{ created: true }])
})

test('file preview keeps returned text verbatim and sends its source filename when created', async t => {
  const originalFile = new Blob(['uploaded bytes'], { type: 'text/plain' })
  const returnedContent = ' \r\nImported line\r\nTrailing space  '
  const harness = createHarness(t, {
    api: {
      previewEntryFile: async () => response({ filename: 'imported.txt', content: returnedContent }),
    },
  })
  const { editor } = harness
  editor.openCreate()
  await editor.previewFile({ raw: originalFile })
  editor.mode.value = 'file'

  assert.equal(editor.form.name, 'imported.txt')
  assert.equal(editor.form.content, returnedContent)
  assert.equal(editor.sourceFilename.value, 'imported.txt')
  assert.equal(await harness.requests.previewEntryFile[0].args[1].get('file').text(), 'uploaded bytes')
  assert.equal(editor.form.content, returnedContent)

  await editor.submit()

  assert.equal(harness.requests.createEntry.length, 1)
  assert.deepEqual(normalize(harness.requests.createEntry[0].args[1]), {
    name: 'imported.txt',
    content: returnedContent,
    source_filename: 'imported.txt',
    chunk_size: 1000,
    chunk_overlap: 100,
    batch_size: 16,
  })
})

test('replacing an existing draft requires confirmation and cancellation preserves it', async t => {
  const harness = createHarness(t, { confirm: async () => { throw 'cancel' } })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = 'Draft name'
  editor.form.content = 'Draft body\r\n'

  await editor.previewFile(new Blob(['replacement']))

  assert.equal(harness.confirms.length, 1)
  assert.equal(harness.requests.previewEntryFile.length, 0)
  assert.equal(editor.form.name, 'Draft name')
  assert.equal(editor.form.content, 'Draft body\r\n')
  assert.equal(editor.previewing.value, false)
})

test('preview parsing failure keeps the current draft and exposes preview feedback', async t => {
  const harness = createHarness(t, {
    confirm: async () => true,
    api: { previewEntryFile: async () => { throw new Error('invalid file') } },
  })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = 'Keep this name'
  editor.form.content = 'Keep this content'

  await editor.previewFile(new Blob(['invalid']))

  assert.equal(editor.form.name, 'Keep this name')
  assert.equal(editor.form.content, 'Keep this content')
  assert.equal(editor.error.value, 'knowledgeBase.entries_preview_failed')
  assert.equal(editor.previewing.value, false)
})

test('late file previews cannot change a reset or different knowledge-base form', async t => {
  for (const transition of ['reset', 'knowledge-base']) {
    await t.test(transition, async nested => {
      const preview = deferred()
      const harness = createHarness(nested, {
        api: { previewEntryFile: () => preview.promise },
      })
      const { editor } = harness
      editor.openCreate()
      const pendingPreview = editor.previewFile(new Blob(['late response']))

      if (transition === 'reset') {
        editor.reset()
        editor.openCreate()
      } else {
        harness.changeKnowledgeBase({ id: 'kb-next', knowledge_base_type: 'user' })
        editor.openCreate()
      }
      editor.form.name = 'New form'
      editor.form.content = 'New form content'
      preview.resolve(response({ filename: 'late.txt', content: 'stale file content' }))
      await pendingPreview

      assert.equal(editor.form.name, 'New form')
      assert.equal(editor.form.content, 'New form content')
      assert.equal(editor.sourceFilename.value, '')
      assert.equal(editor.visible.value, true)
    })
  }
})

test('submit is ignored while a file preview is pending', async t => {
  const preview = deferred()
  const harness = createHarness(t, {
    api: { previewEntryFile: () => preview.promise },
  })
  const { editor } = harness
  let validationCalls = 0
  harness.setValidation(async () => {
    validationCalls += 1
    return true
  })
  editor.openCreate()
  const pendingPreview = editor.previewFile(new Blob(['file']))

  await editor.submit()

  assert.equal(editor.previewing.value, true)
  assert.equal(validationCalls, 0)
  assert.equal(harness.requests.createEntry.length, 0)
  preview.resolve(response({ filename: 'file.txt', content: 'previewed' }))
  await pendingPreview
})

test('failed MANAGED creates retain input and reuse dedupe key until payload changes', async t => {
  const harness = createHarness(t, {
    knowledgeBase: { id: 'kb-managed', knowledge_base_type: 'llm_managed' },
    api: { createEntry: async () => { throw new Error('temporary failure') } },
  })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = 'Retry me'
  editor.form.content = 'Same request'

  await editor.submit()
  const firstPayload = harness.requests.createEntry[0].args[1]
  assert.equal(editor.visible.value, true)
  assert.equal(editor.form.name, 'Retry me')
  assert.equal(editor.form.content, 'Same request')
  assert.equal(editor.error.value, 'knowledgeBase.entries_failed')

  await editor.submit()
  const secondPayload = harness.requests.createEntry[1].args[1]
  assert.equal(secondPayload.dedupe_key, firstPayload.dedupe_key)

  editor.form.content = 'Changed request'
  await editor.submit()
  const thirdPayload = harness.requests.createEntry[2].args[1]
  assert.notEqual(thirdPayload.dedupe_key, secondPayload.dedupe_key)
  assert.equal(editor.form.content, 'Changed request')
  assert.equal(editor.visible.value, true)
})

test('existing_key and existing_content feedback preserve the managed editor panel', async t => {
  const statuses = ['existing_key', 'existing_content']
  const harness = createHarness(t, {
    knowledgeBase: { id: 'kb-managed', knowledge_base_type: 'llm_managed' },
    api: {
      createEntry: async () => response({ status: statuses.shift() }),
    },
  })
  const { editor } = harness
  editor.openCreate()
  editor.form.name = 'Existing item'
  editor.form.content = 'Existing content'

  await editor.submit()
  assert.equal(editor.visible.value, true)
  assert.equal(editor.error.value, 'knowledgeBase.managed_existing_key')
  await editor.submit()

  assert.equal(editor.visible.value, true)
  assert.equal(editor.error.value, 'knowledgeBase.managed_existing_content')
  assert.deepEqual(harness.messages.map(item => item.args[0].message), [
    'knowledgeBase.managed_existing_key',
    'knowledgeBase.managed_existing_content',
  ])
  assert.deepEqual(harness.savedResults, [])
})

test('managed edit loads the latest full item and updates with its expected version', async t => {
  const fullContent = 'Latest server content\r\nwith all lines'
  const harness = createHarness(t, {
    knowledgeBase: { id: 'kb-managed', knowledge_base_type: 'llm_managed' },
    api: {
      managedItem: async () => response({
        id: 'item-7',
        knowledge_key: 'latest key',
        content: fullContent,
        version: 42,
        llm_maintainable: true,
      }),
    },
  })
  const { editor } = harness
  await editor.openEdit({ id: 'item-7' })

  assert.equal(editor.visible.value, true)
  assert.equal(editor.loading.value, false)
  assert.equal(editor.editingId.value, 'item-7')
  assert.equal(editor.form.name, 'latest key')
  assert.equal(editor.form.content, fullContent)
  editor.form.name = '  renamed key '
  editor.form.content = 'Updated full content\r\n'
  await editor.submit()

  assert.equal(harness.requests.updateManagedItem.length, 1)
  assert.equal(harness.requests.createEntry.length, 0)
  assert.deepEqual(normalize(harness.requests.updateManagedItem[0].args), [
    'kb-managed',
    'item-7',
    {
      expected_version: 42,
      knowledge_key: 'renamed key',
      content: 'Updated full content\r\n',
      llm_maintainable: true,
      dedupe_key: harness.requests.updateManagedItem[0].args[2].dedupe_key,
    },
  ])
  assert.match(harness.requests.updateManagedItem[0].args[2].dedupe_key, /^managed-ui-update:/)
  assert.deepEqual(harness.savedResults, [{ created: false }])
})

test('USER knowledge bases cannot open managed-item editing', async t => {
  const harness = createHarness(t)
  await harness.editor.openEdit({ id: 'user-item' })

  assert.equal(harness.editor.visible.value, false)
  assert.equal(harness.editor.editingId.value, null)
  assert.equal(harness.requests.managedItem.length, 0)
})

test('asynchronous validation is single-flight and blocks leaving while submitting', async t => {
  const validation = deferred()
  const harness = createHarness(t)
  const { editor } = harness
  let validationCalls = 0
  harness.setValidation(() => {
    validationCalls += 1
    return validation.promise
  })
  editor.openCreate()
  editor.form.name = 'Valid entry'
  editor.form.content = 'Body'

  const firstSubmit = editor.submit()
  const secondSubmit = editor.submit()
  await secondSubmit

  assert.equal(validationCalls, 1)
  assert.equal(editor.submitting.value, true)
  assert.equal(await editor.canLeave(), false)
  assert.ok(harness.messages.some(item => item.method === 'warning'
    && item.args[0] === 'knowledgeBase.entries_busy'))
  validation.resolve(true)
  await firstSubmit

  assert.equal(harness.requests.createEntry.length, 1)
  assert.equal(editor.submitting.value, false)
})

test('rejected form validation does not issue a create request', async t => {
  const harness = createHarness(t)
  const { editor } = harness
  editor.openCreate()
  editor.form.name = 'Invalid entry'
  editor.form.content = 'Body'
  harness.setValidation(async () => { throw new Error('invalid form') })

  await editor.submit()

  assert.equal(harness.requests.createEntry.length, 0)
  assert.equal(editor.visible.value, true)
  assert.equal(editor.error.value, '')
})

test('a validation result from an invalidated identity cannot submit or alter its new panel', async t => {
  const validation = deferred()
  const harness = createHarness(t)
  const { editor } = harness
  harness.setValidation(() => validation.promise)
  editor.openCreate()
  editor.form.name = 'Old panel'
  editor.form.content = 'Old body'
  const pendingSubmit = editor.submit()

  harness.changeKnowledgeBase({ id: 'kb-next', knowledge_base_type: 'user' })
  editor.openCreate()
  editor.form.name = 'New panel'
  editor.form.content = 'New body'
  validation.resolve(true)
  await pendingSubmit

  assert.equal(harness.requests.createEntry.length, 0)
  assert.equal(editor.visible.value, true)
  assert.equal(editor.form.name, 'New panel')
  assert.equal(editor.form.content, 'New body')
  assert.deepEqual(harness.messages, [])
  assert.deepEqual(harness.savedResults, [])
})

test('late create success or failure cannot close or notify a different identity panel', async t => {
  for (const outcome of ['success', 'failure']) {
    await t.test(outcome, async nested => {
      const request = deferred()
      const harness = createHarness(nested, {
        api: { createEntry: () => request.promise },
      })
      const { editor } = harness
      editor.openCreate()
      editor.form.name = 'Old panel'
      editor.form.content = 'Old body'
      const pendingSubmit = editor.submit()
      await new Promise(resolve => setImmediate(resolve))
      assert.equal(harness.requests.createEntry.length, 1)

      harness.changeKnowledgeBase({ id: 'kb-next', knowledge_base_type: 'user' })
      editor.openCreate()
      editor.form.name = 'New panel'
      editor.form.content = 'New body'
      if (outcome === 'success') request.resolve(response({ status: 'created' }))
      else request.reject(new Error('late failure'))
      await pendingSubmit

      assert.equal(editor.visible.value, true)
      assert.equal(editor.form.name, 'New panel')
      assert.equal(editor.form.content, 'New body')
      assert.equal(editor.error.value, '')
      assert.deepEqual(harness.messages, [])
      assert.deepEqual(harness.savedResults, [])
    })
  }
})

test('canLeave bypasses clean forms, honors cancellation, and requestClose clears confirmed drafts', async t => {
  const harness = createHarness(t)
  const { editor } = harness
  editor.openCreate()

  assert.equal(await editor.canLeave(), true)
  assert.equal(harness.confirms.length, 0)

  editor.form.content = 'Unsaved draft'
  harness.options.confirm = async () => { throw 'cancel' }
  assert.equal(await editor.canLeave(), false)
  assert.equal(editor.visible.value, true)
  assert.equal(editor.form.content, 'Unsaved draft')

  harness.options.confirm = async () => true
  assert.equal(await editor.requestClose(), true)
  assert.equal(editor.visible.value, false)
  assert.equal(editor.form.name, '')
  assert.equal(editor.form.content, '')
})

test('an old discard confirmation cannot close a newly opened panel', async t => {
  const confirmation = deferred()
  const harness = createHarness(t, { confirm: () => confirmation.promise })
  const { editor } = harness
  editor.openCreate()
  editor.form.content = 'Old dirty draft'
  const pendingClose = editor.requestClose()

  editor.reset()
  editor.openCreate()
  editor.form.name = 'New panel'
  editor.form.content = 'New draft'
  confirmation.resolve(true)
  assert.equal(await pendingClose, false)

  assert.equal(editor.visible.value, true)
  assert.equal(editor.form.name, 'New panel')
  assert.equal(editor.form.content, 'New draft')
})
