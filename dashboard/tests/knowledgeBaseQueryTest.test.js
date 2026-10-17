import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { computed, reactive, ref } from 'vue'
import { createKnowledgeBaseScope } from '../src/utils/knowledgeBaseScope.js'

const source = readFileSync(
  new URL('../src/composables/knowledgeBase/useKnowledgeBaseQueryTest.js', import.meta.url),
  'utf8'
)
  .replace(/^import .*\r?\n/gm, '')
  .replace('export function useKnowledgeBaseQueryTest', 'function useKnowledgeBaseQueryTest')

const normalize = value => JSON.parse(JSON.stringify(value))

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function createHarness() {
  const apiCalls = []
  const errors = []
  let apiQueryTest = async () => ({ data: { data: { items: [] } } })
  let validation = () => true
  let validateCalls = 0
  let clearValidateCalls = 0
  let isActive = true
  const knowledgeBaseApi = {
    queryTest(id, params) {
      apiCalls.push([id, params])
      return apiQueryTest(id, params)
    }
  }
  const context = vm.createContext({
    computed,
    reactive,
    ref,
    knowledgeBaseApi,
    ElMessage: { error: message => errors.push(message) }
  })
  vm.runInContext(
    `${source}\nthis.useKnowledgeBaseQueryTest = useKnowledgeBaseQueryTest`,
    context,
    { filename: 'useKnowledgeBaseQueryTest.js' }
  )

  const selectedKb = ref(null)
  const scope = createKnowledgeBaseScope({
    isActive: () => isActive,
    confirm: async () => true,
    closeConfirmations() {}
  })
  const module = context.useKnowledgeBaseQueryTest({
    t: key => key,
    selectedKb,
    scope
  })
  module.queryTestFormRef.value = {
    validate(callback) {
      validateCalls += 1
      const result = Promise.resolve().then(() => validation())
      return callback
        ? result.then(async valid => {
            await callback(valid)
            return valid
          })
        : result
    },
    clearValidate() {
      clearValidateCalls += 1
    }
  }

  return {
    apiCalls,
    errors,
    module,
    scope,
    selectedKb,
    setActive(value) {
      isActive = value
    },
    setApiQueryTest(handler) {
      apiQueryTest = handler
    },
    setValidation(handler) {
      validation = handler
    },
    get validateCalls() {
      return validateCalls
    },
    get clearValidateCalls() {
      return clearValidateCalls
    },
    open(row = { id: 'kb-1' }) {
      module.showQueryTestDialog(row)
    },
    invalidateIdentity() {
      scope.invalidate()
      module.resetQueryTestState()
    }
  }
}

test('submits the selected knowledge base query and resets on reopen', async () => {
  const h = createHarness()
  const items = [{ id: 'chunk-1', content: 'match' }]
  h.setApiQueryTest(async () => ({
    data: { data: { items, retrieval_mode: 'hybrid', rerank_error: 'rerank unavailable' } }
  }))

  h.open({ id: 42 })
  h.module.queryTestForm.query = 'find this'
  h.module.queryTestForm.top_k = 8
  await h.module.submitQueryTest()

  assert.deepEqual(normalize(h.apiCalls), [[42, { query: 'find this', top_k: 8 }]])
  assert.deepEqual(normalize(h.module.queryTestResults.value), items)
  assert.equal(h.module.retrievalMode.value, 'hybrid')
  assert.equal(h.module.rerankError.value, 'rerank unavailable')
  assert.equal(h.module.queryTested.value, true)
  assert.equal(h.module.queryTesting.value, false)

  const clearsBeforeReopen = h.clearValidateCalls
  h.open({ id: 42 })
  assert.equal(h.module.queryTestForm.query, '')
  assert.equal(h.module.queryTestForm.top_k, 5)
  assert.deepEqual(normalize(h.module.queryTestResults.value), [])
  assert.equal(h.module.retrievalMode.value, null)
  assert.equal(h.module.rerankError.value, null)
  assert.equal(h.module.queryTested.value, false)
  assert.equal(h.clearValidateCalls, clearsBeforeReopen + 1)
})

test('does not submit invalid or stale validation and allows retry', async () => {
  const h = createHarness()
  h.open()
  h.module.queryTestForm.query = 'retry me'
  h.setValidation(() => false)
  await h.module.submitQueryTest()
  assert.equal(h.apiCalls.length, 0)

  h.setValidation(() => true)
  await h.module.submitQueryTest()
  assert.equal(h.apiCalls.length, 1)

  const pendingValidation = deferred()
  const h2 = createHarness()
  h2.open()
  h2.module.queryTestForm.query = 'stale validation'
  h2.setValidation(() => pendingValidation.promise)
  const submission = h2.module.submitQueryTest()
  assert.equal(h2.validateCalls, 1)
  await h2.module.submitQueryTest()
  assert.equal(h2.validateCalls, 1)

  h2.setActive(false)
  h2.invalidateIdentity()
  pendingValidation.resolve(true)
  await submission
  assert.equal(h2.apiCalls.length, 0)
})

test('ignores stale success and failure after reopening the same knowledge base', async () => {
  for (const oldOutcome of ['success', 'failure']) {
    const h = createHarness()
    const oldResponse = deferred()
    const oldStarted = deferred()
    const newResponse = deferred()
    const newStarted = deferred()
    h.setApiQueryTest((id, params) => {
      if (params.query === 'old query') {
        oldStarted.resolve()
        return oldResponse.promise
      }
      newStarted.resolve()
      return newResponse.promise
    })

    h.open({ id: 9 })
    h.module.queryTestForm.query = 'old query'
    const oldSubmission = h.module.submitQueryTest()
    await oldStarted.promise

    h.invalidateIdentity()
    h.open({ id: 9 })
    h.module.queryTestForm.query = 'new query'
    const newSubmission = h.module.submitQueryTest()
    await newStarted.promise

    if (oldOutcome === 'success') {
      oldResponse.resolve({ data: { data: { items: [{ id: 'stale' }] } } })
    } else {
      oldResponse.reject(new Error('stale failure'))
    }
    await oldSubmission

    assert.deepEqual(normalize(h.module.queryTestResults.value), [])
    assert.equal(h.module.queryTestForm.query, 'new query')
    assert.equal(h.module.queryTesting.value, true)
    assert.equal(h.module.queryTested.value, false)
    assert.deepEqual(h.errors, [])

    const currentItems = [{ id: 'current' }]
    newResponse.resolve({
      data: { data: { items: currentItems, retrieval_mode: 'dense', rerank_error: null } }
    })
    await newSubmission
    assert.deepEqual(normalize(h.module.queryTestResults.value), currentItems)
    assert.equal(h.module.retrievalMode.value, 'dense')
    assert.equal(h.module.queryTested.value, true)
    assert.equal(h.module.queryTesting.value, false)
  }
})

test('does not revive results across A-B-A scope reuse or submit without prerequisites', async () => {
  const h = createHarness()
  const oldResponse = deferred()
  const oldStarted = deferred()
  const currentResponse = deferred()
  const currentStarted = deferred()
  h.setApiQueryTest((id, params) => {
    if (params.query === 'A old') {
      oldStarted.resolve()
      return oldResponse.promise
    }
    currentStarted.resolve()
    return currentResponse.promise
  })

  h.open({ id: 7 })
  h.module.queryTestForm.query = 'A old'
  const oldSubmission = h.module.submitQueryTest()
  await oldStarted.promise
  let identity = 'A'
  const switchIdentity = next => {
    identity = next
    h.invalidateIdentity()
  }
  switchIdentity('B')
  switchIdentity('A')
  assert.equal(identity, 'A')
  h.open({ id: 7 })
  h.module.queryTestForm.query = 'A current'
  const currentSubmission = h.module.submitQueryTest()
  await currentStarted.promise

  oldResponse.resolve({ data: { data: { items: [{ id: 'revived' }] } } })
  await oldSubmission
  assert.deepEqual(normalize(h.module.queryTestResults.value), [])
  assert.equal(h.module.queryTesting.value, true)
  currentResponse.resolve({ data: { data: { items: [{ id: 'A current' }] } } })
  await currentSubmission
  assert.deepEqual(normalize(h.module.queryTestResults.value), [{ id: 'A current' }])

  h.module.resetQueryTestState()
  h.open()
  h.selectedKb.value = null
  await h.module.submitQueryTest()

  h.open()
  h.module.queryTestFormRef.value = null
  await h.module.submitQueryTest()

  h.module.queryTestFormRef.value = {
    validate: async callback => callback(true),
    clearValidate() {}
  }
  h.open()
  h.module.queryTestDialogVisible.value = false
  await h.module.submitQueryTest()
  assert.equal(h.apiCalls.length, 2)
})
