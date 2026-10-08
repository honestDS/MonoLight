import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope, ref } from 'vue'
import {
  CHAT_DRAFT_STORAGE_PREFIX,
  useChatDrafts
} from '../src/composables/chat/useChatDrafts.js'

const draftKey = (uid, sessionId) => (
  `${CHAT_DRAFT_STORAGE_PREFIX}${JSON.stringify([uid, sessionId])}`
)

const createStorage = (entries = [], options = {}) => {
  const values = new Map(entries)
  let reads = 0
  let writes = 0
  let removals = 0

  return {
    getItem: key => {
      reads += 1
      if (options.failGet) throw new Error('storage read blocked')
      return values.has(key) ? values.get(key) : null
    },
    setItem: (key, value) => {
      writes += 1
      if (options.failSet) throw new Error('storage write blocked')
      values.set(key, String(value))
    },
    removeItem: key => {
      removals += 1
      if (options.failRemove) throw new Error('storage removal blocked')
      values.delete(key)
    },
    has: key => values.has(key),
    value: key => values.get(key),
    snapshot: () => new Map(values),
    counts: () => ({ reads, writes, removals })
  }
}

const createHarness = (t, options = {}) => {
  const inputMsg = ref(Object.hasOwn(options, 'input') ? options.input : '')
  const currentUid = ref(Object.hasOwn(options, 'uid') ? options.uid : null)
  const currentSessionId = ref(
    Object.hasOwn(options, 'sessionId') ? options.sessionId : null
  )
  const isCurrentSessionReadOnly = ref(Boolean(options.readOnly))
  const scope = effectScope()
  const drafts = scope.run(() => useChatDrafts({
    inputMsg,
    currentUid,
    currentSessionId,
    isCurrentSessionReadOnly,
    storage: options.storage
  }))

  t.after(() => scope.stop())

  return {
    ...drafts,
    inputMsg,
    currentUid,
    currentSessionId,
    isCurrentSessionReadOnly,
    scope
  }
}

test('chat drafts persist exact text, restore after scope recreation, and clear safely', t => {
  const uid = 'user-a'
  const sessionId = 'session-a'
  const storage = createStorage()
  const first = createHarness(t, { uid, sessionId, storage })
  const key = draftKey(uid, sessionId)
  const text = '  中文第一行\n第二行  '

  first.inputMsg.value = text
  assert.equal(storage.value(key), text)

  first.scope.stop()
  const reopened = createHarness(t, { uid, sessionId, storage })
  assert.equal(reopened.inputMsg.value, text)

  reopened.inputMsg.value = ''
  assert.equal(storage.has(key), false)
  assert.doesNotThrow(() => {
    reopened.inputMsg.value = ''
    reopened.removeDraft(sessionId)
    reopened.removeDraft(sessionId)
  })
  assert.equal(storage.has(key), false)
})

test('chat drafts isolate A, B, and null sessions without writing during restore', t => {
  const uid = 'user-a'
  const storage = createStorage([
    [draftKey(uid, 'B'), 'B draft'],
    [draftKey(uid, null), 'null draft']
  ])
  const harness = createHarness(t, {
    uid,
    sessionId: 'A',
    storage
  })

  harness.inputMsg.value = 'A draft'
  assert.equal(storage.value(draftKey(uid, 'A')), 'A draft')

  harness.currentSessionId.value = 'B'
  harness.restoreSession()
  assert.equal(harness.inputMsg.value, 'B draft')
  const writesAfterBRestore = storage.counts().writes
  harness.restoreSession()
  assert.equal(harness.inputMsg.value, 'B draft')
  assert.equal(storage.counts().writes, writesAfterBRestore)

  harness.currentSessionId.value = 'C'
  harness.restoreSession()
  assert.equal(harness.inputMsg.value, '')
  assert.equal(storage.has(draftKey(uid, 'C')), false)
  assert.equal(storage.counts().writes, writesAfterBRestore)

  harness.currentSessionId.value = null
  harness.restoreSession()
  assert.equal(harness.inputMsg.value, 'null draft')
  harness.inputMsg.value = 'new null draft'
  assert.equal(storage.value(draftKey(uid, null)), 'new null draft')

  harness.currentSessionId.value = 'A'
  harness.restoreSession()
  assert.equal(harness.inputMsg.value, 'A draft')
})

test('chat drafts isolate users, discard anonymous logout input, and stop with the scope', t => {
  const sessionId = 'same-session'
  const storage = createStorage()
  const harness = createHarness(t, {
    uid: 'user-a',
    sessionId,
    storage
  })

  harness.inputMsg.value = 'A draft'
  harness.currentUid.value = null
  assert.equal(harness.inputMsg.value, '')

  harness.inputMsg.value = 'anonymous input'
  harness.currentUid.value = 'user-b'
  assert.equal(harness.inputMsg.value, '')
  assert.equal(storage.has(draftKey('user-b', sessionId)), false)
  assert.equal(storage.has(draftKey(null, sessionId)), false)

  harness.inputMsg.value = 'B draft'
  harness.currentUid.value = 'user-a'
  assert.equal(harness.inputMsg.value, 'A draft')
  assert.equal(storage.value(draftKey('user-a', sessionId)), 'A draft')
  assert.equal(storage.value(draftKey('user-b', sessionId)), 'B draft')

  const beforeStop = storage.snapshot()
  harness.scope.stop()
  harness.inputMsg.value = 'after scope stop'
  harness.currentUid.value = 'user-c'
  harness.currentSessionId.value = 'other-session'
  assert.deepEqual(storage.snapshot(), beforeStop)
})

test('late identity flushes multiple sessions and keeps an empty tombstone ahead of old storage', t => {
  const uid = 'late-user'
  const oldSession = 'old-session'
  const newSession = 'new-session'
  const clearedSession = 'cleared-session'
  const storage = createStorage([
    [draftKey(uid, oldSession), 'old local draft'],
    [draftKey(uid, clearedSession), 'old draft to clear']
  ])
  const harness = createHarness(t, {
    uid: null,
    sessionId: oldSession,
    storage
  })

  harness.inputMsg.value = 'new old-session draft'
  harness.currentSessionId.value = newSession
  harness.restoreSession()
  harness.inputMsg.value = 'new-session draft'
  harness.currentSessionId.value = clearedSession
  harness.restoreSession()
  harness.inputMsg.value = 'draft sent and cleared'
  harness.inputMsg.value = ''

  harness.currentUid.value = uid
  assert.equal(storage.value(draftKey(uid, oldSession)), 'new old-session draft')
  assert.equal(storage.value(draftKey(uid, newSession)), 'new-session draft')
  assert.equal(storage.has(draftKey(uid, clearedSession)), false)
  assert.equal(harness.inputMsg.value, '')

  const lateStorage = createStorage([
    [draftKey(uid, 'late-session'), 'old server draft']
  ])
  const lateIdentity = createHarness(t, {
    uid: null,
    sessionId: 'late-session',
    storage: lateStorage
  })
  lateIdentity.inputMsg.value = 'typed before identity arrived'
  lateIdentity.currentUid.value = uid
  assert.equal(lateIdentity.inputMsg.value, 'typed before identity arrived')
  assert.equal(
    lateStorage.value(draftKey(uid, 'late-session')),
    'typed before identity arrived'
  )
})

test('read-only sessions never restore or persist drafts across repeated opens and toggles', t => {
  const uid = 'external-user'
  const sessionId = 'external-session'
  const storage = createStorage([
    [draftKey(uid, sessionId), 'stored draft']
  ])
  const readOnly = createHarness(t, {
    uid,
    sessionId,
    input: 'external guide',
    readOnly: true,
    storage
  })

  assert.equal(readOnly.inputMsg.value, '')
  assert.equal(storage.counts().writes, 0)
  assert.equal(storage.counts().removals, 0)
  readOnly.inputMsg.value = 'typed in external mode'
  readOnly.restoreSession()
  readOnly.restoreSession()
  assert.equal(readOnly.inputMsg.value, '')
  assert.equal(storage.value(draftKey(uid, sessionId)), 'stored draft')
  assert.equal(storage.counts().writes, 0)
  assert.equal(storage.counts().removals, 0)

  const ordinaryStorage = createStorage()
  const ordinary = createHarness(t, {
    uid,
    sessionId: 'ordinary-session',
    storage: ordinaryStorage
  })
  ordinary.inputMsg.value = 'ordinary draft'
  const writesBeforeToggle = ordinaryStorage.counts().writes
  ordinary.isCurrentSessionReadOnly.value = true
  assert.equal(ordinary.inputMsg.value, '')
  ordinary.inputMsg.value = 'external guide text'
  ordinary.isCurrentSessionReadOnly.value = false
  assert.equal(ordinary.inputMsg.value, 'ordinary draft')
  assert.equal(ordinaryStorage.counts().writes, writesBeforeToggle)
  assert.equal(
    ordinaryStorage.value(draftKey(uid, 'ordinary-session')),
    'ordinary draft'
  )
})

test('adoptSessionId moves anonymous input, clears the null draft, and ignores invalid IDs', t => {
  const uid = 'known-user'
  const targetSession = 'server-session'
  const storage = createStorage([
    [draftKey(uid, null), 'stale anonymous draft']
  ])
  const harness = createHarness(t, {
    uid,
    sessionId: null,
    storage
  })

  harness.inputMsg.value = 'fresh anonymous draft'
  harness.currentSessionId.value = targetSession
  harness.adoptSessionId(targetSession)
  assert.equal(storage.has(draftKey(uid, null)), false)
  assert.equal(storage.value(draftKey(uid, targetSession)), 'fresh anonymous draft')
  assert.equal(harness.inputMsg.value, 'fresh anonymous draft')

  const afterAdoption = storage.snapshot()
  for (const invalidId of [targetSession, null, '', '  ', 42, 'other-session']) {
    assert.doesNotThrow(() => harness.adoptSessionId(invalidId))
  }
  assert.deepEqual(storage.snapshot(), afterAdoption)
  assert.equal(harness.inputMsg.value, 'fresh anonymous draft')

  const delayedStorage = createStorage()
  const delayed = createHarness(t, {
    uid: null,
    sessionId: null,
    storage: delayedStorage
  })
  delayed.inputMsg.value = 'unknown user input'
  delayed.currentSessionId.value = 'assigned-session'
  for (const invalidId of ['', '  ', null, 0, 'wrong-session']) {
    delayed.adoptSessionId(invalidId)
  }
  delayed.adoptSessionId('assigned-session')
  assert.equal(delayed.inputMsg.value, 'unknown user input')
  delayed.currentUid.value = uid
  assert.equal(delayed.inputMsg.value, 'unknown user input')
  assert.equal(delayedStorage.has(draftKey(uid, null)), false)
  assert.equal(
    delayedStorage.value(draftKey(uid, 'assigned-session')),
    'unknown user input'
  )
})

test('removeDraft deletes only its target and remains safe when repeated', t => {
  const uid = 'remove-user'
  const storage = createStorage([
    [draftKey(uid, 'A'), 'A draft'],
    [draftKey(uid, 'B'), 'B draft'],
    [draftKey(uid, null), 'null draft']
  ])
  const harness = createHarness(t, {
    uid,
    sessionId: 'A',
    storage
  })

  harness.removeDraft('A')
  assert.equal(storage.has(draftKey(uid, 'A')), false)
  assert.equal(storage.value(draftKey(uid, 'B')), 'B draft')
  assert.equal(storage.value(draftKey(uid, null)), 'null draft')

  assert.doesNotThrow(() => {
    harness.removeDraft('A')
    harness.removeDraft('A')
    harness.removeDraft(null)
    harness.removeDraft(null)
  })
  assert.equal(storage.has(draftKey(uid, 'A')), false)
  assert.equal(storage.has(draftKey(uid, null)), false)
  assert.equal(storage.value(draftKey(uid, 'B')), 'B draft')
})

test('chat drafts retain memory state when storage is missing or operations fail', t => {
  const missingStorage = createHarness(t, {
    uid: 'missing-storage-user',
    sessionId: 'missing-storage-session',
    storage: null
  })
  missingStorage.inputMsg.value = 'memory-only draft'
  assert.doesNotThrow(() => missingStorage.restoreSession())
  assert.equal(missingStorage.inputMsg.value, 'memory-only draft')

  const uid = 'failure-user'
  const sessionId = 'failure-session'
  const setFailureStorage = createStorage([
    [draftKey(uid, sessionId), 'old draft']
  ], { failSet: true })
  const setFailure = createHarness(t, {
    uid,
    sessionId,
    storage: setFailureStorage
  })
  setFailure.inputMsg.value = 'new in memory'
  assert.equal(setFailureStorage.value(draftKey(uid, sessionId)), 'old draft')
  assert.doesNotThrow(() => setFailure.restoreSession())
  assert.equal(setFailure.inputMsg.value, 'new in memory')

  const getFailure = createHarness(t, {
    uid,
    sessionId: 'get-failure-session',
    storage: createStorage([], { failGet: true })
  })
  assert.equal(getFailure.inputMsg.value, '')
  assert.doesNotThrow(() => getFailure.restoreSession())
  assert.equal(getFailure.inputMsg.value, '')

  const removeFailureStorage = createStorage([
    [draftKey(uid, 'remove-failure-session'), 'old draft']
  ], { failRemove: true })
  const removeFailure = createHarness(t, {
    uid,
    sessionId: 'remove-failure-session',
    storage: removeFailureStorage
  })
  removeFailure.inputMsg.value = ''
  assert.equal(
    removeFailureStorage.value(draftKey(uid, 'remove-failure-session')),
    'old draft'
  )
  assert.doesNotThrow(() => removeFailure.restoreSession())
  assert.equal(removeFailure.inputMsg.value, '')
  assert.doesNotThrow(() => removeFailure.removeDraft('remove-failure-session'))
  assert.equal(removeFailure.inputMsg.value, '')

  const originalLocalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  try {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      enumerable: originalLocalStorage?.enumerable ?? false,
      get: () => {
        throw new Error('localStorage getter blocked')
      }
    })
    const getterFailure = createHarness(t, {
      uid: 'getter-failure-user',
      sessionId: 'getter-failure-session'
    })
    assert.doesNotThrow(() => {
      getterFailure.inputMsg.value = 'getter failure memory draft'
      getterFailure.restoreSession()
    })
    assert.equal(getterFailure.inputMsg.value, 'getter failure memory draft')
  } finally {
    if (originalLocalStorage) {
      Object.defineProperty(globalThis, 'localStorage', originalLocalStorage)
    } else {
      delete globalThis.localStorage
    }
  }
})

test('chat drafts reject invalid identities and text while allowing null sessions without key collisions', t => {
  for (const uid of [null, undefined, '', '   ', 42]) {
    const storage = createStorage()
    const harness = createHarness(t, {
      uid,
      sessionId: 'valid-session',
      storage
    })
    harness.inputMsg.value = 'should not persist'
    assert.equal(storage.snapshot().size, 0)
  }

  for (const sessionId of [undefined, '', '   ', 42, false]) {
    const storage = createStorage()
    const harness = createHarness(t, {
      uid: 'valid-user',
      sessionId,
      storage
    })
    harness.inputMsg.value = 'should not persist'
    assert.equal(storage.snapshot().size, 0)
  }

  const nullStorage = createStorage()
  const nullSession = createHarness(t, {
    uid: 'valid-user',
    sessionId: null,
    storage: nullStorage
  })
  nullSession.inputMsg.value = 'valid null-session draft'
  assert.equal(
    nullStorage.value(draftKey('valid-user', null)),
    'valid null-session draft'
  )

  const textStorage = createStorage()
  const textHarness = createHarness(t, {
    uid: 'valid-user',
    sessionId: 'text-session',
    storage: textStorage
  })
  for (const invalidText of [123, null, {}, ['not text']]) {
    textHarness.inputMsg.value = invalidText
    assert.equal(textStorage.snapshot().size, 0)
  }
  textHarness.inputMsg.value = 'valid text'
  assert.equal(textStorage.value(draftKey('valid-user', 'text-session')), 'valid text')

  const collisionStorage = createStorage()
  const firstPair = {
    uid: 'left"|middle',
    sessionId: 'right,]'
  }
  const secondPair = {
    uid: 'left"',
    sessionId: 'middle|right,]'
  }
  const first = createHarness(t, { ...firstPair, storage: collisionStorage })
  const second = createHarness(t, { ...secondPair, storage: collisionStorage })
  first.inputMsg.value = 'first delimited draft'
  second.inputMsg.value = 'second delimited draft'
  const firstKey = draftKey(firstPair.uid, firstPair.sessionId)
  const secondKey = draftKey(secondPair.uid, secondPair.sessionId)
  assert.notEqual(firstKey, secondKey)
  assert.equal(collisionStorage.value(firstKey), 'first delimited draft')
  assert.equal(collisionStorage.value(secondKey), 'second delimited draft')
})

test('disposed draft methods cannot mutate a replacement null-session page', t => {
  const uid = 'disposed-user'
  const storage = createStorage()
  const oldHarness = createHarness(t, {
    uid,
    sessionId: null,
    storage
  })

  oldHarness.inputMsg.value = 'old draft'
  oldHarness.scope.stop()

  const newHarness = createHarness(t, {
    uid,
    sessionId: null,
    storage
  })
  newHarness.inputMsg.value = 'new page draft'

  oldHarness.currentSessionId.value = 'late-id'
  for (let attempt = 0; attempt < 2; attempt += 1) {
    oldHarness.adoptSessionId('late-id')
    oldHarness.removeDraft(null)
    oldHarness.removeDraft('late-id')
    oldHarness.restoreSession()
  }

  assert.equal(newHarness.inputMsg.value, 'new page draft')
  assert.equal(storage.value(draftKey(uid, null)), 'new page draft')
  assert.equal(storage.has(draftKey(uid, 'late-id')), false)
  assert.equal(oldHarness.inputMsg.value, 'old draft')
})
