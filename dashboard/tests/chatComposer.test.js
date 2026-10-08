import test from 'node:test'
import assert from 'node:assert/strict'
import { nextTick, ref } from 'vue'
import { useChatComposer } from '../src/composables/chat/useChatComposer.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function makeFixture() {
  const sendPayloads = []
  const enqueuePayloads = []
  const guidanceRequests = []
  const uploadRequests = []
  const notifications = {
    warning: [],
    error: [],
    success: []
  }
  const sessionsLoaded = []
  const scrollCalls = []
  const agentSettingSubmitting = ref(false)

  const chat = {
    isStopping: ref(false),
    isReplyRunning: ref(false),
    modeSettingSubmitting: ref(false),
    isCurrentSessionReadOnly: ref(false),
    inputMsg: ref(''),
    currentSessionId: ref('session-a'),
    messages: ref([]),
    messageList: ref({
      async scrollToBottom(behavior) {
        scrollCalls.push(behavior)
      }
    }),
    loading: ref(false),
    attachments: ref([]),
    async loadSessions() {
      sessionsLoaded.push(true)
    },
    async send() {
      sendPayloads.push({
        content: chat.inputMsg.value,
        attachments: chat.attachments.value.map(attachment => ({ ...attachment }))
      })
    },
    enqueueMessage(content, attachmentList) {
      enqueuePayloads.push({
        content,
        attachments: attachmentList.map(attachment => ({ ...attachment }))
      })
    }
  }

  const api = {
    guidanceResponse: {
      data: {
        data: {
          id: 'default-guidance',
          role: 'user',
          content: 'default'
        }
      }
    },
    async createGuidance(payload) {
      guidanceRequests.push({ ...payload })
      return api.guidanceResponse
    }
  }

  const fileApi = {
    uploadResponse: {
      data: {
        filename: 'server-file.txt',
        path: '/uploads/server-file.txt'
      }
    },
    async upload(file, sessionId) {
      uploadRequests.push({ file, sessionId })
      return fileApi.uploadResponse
    }
  }

  const notify = {
    warning(value) {
      notifications.warning.push(value)
    },
    error(value) {
      notifications.error.push(value)
    },
    success(value) {
      notifications.success.push(value)
    }
  }

  const composer = useChatComposer({
    chat,
    agentSettingSubmitting,
    api,
    fileApi,
    notify,
    translate: key => key
  })

  return {
    chat,
    agentSettingSubmitting,
    api,
    fileApi,
    composer,
    sendPayloads,
    enqueuePayloads,
    guidanceRequests,
    uploadRequests,
    notifications,
    sessionsLoaded,
    scrollCalls
  }
}

test('sends normally, queues while loading, and preserves empty loading input', async () => {
  const fixture = makeFixture()
  const { chat, composer, sendPayloads, enqueuePayloads } = fixture
  const attachment = { uid: 'attachment-1', name: 'notes.txt', path: '/notes.txt' }

  chat.inputMsg.value = 'hello with attachment'
  chat.attachments.value = [attachment]
  composer.uploadFileList.value = [{ uid: attachment.uid, name: attachment.name }]

  await composer.send()

  assert.deepEqual(sendPayloads, [{
    content: 'hello with attachment',
    attachments: [attachment]
  }])
  assert.deepEqual(composer.uploadFileList.value, [])

  chat.loading.value = true
  chat.inputMsg.value = 'queued text'
  chat.attachments.value = [attachment]
  composer.uploadFileList.value = [{ uid: attachment.uid, name: attachment.name }]

  await composer.send()

  assert.deepEqual(enqueuePayloads, [{
    content: 'queued text',
    attachments: [attachment]
  }])
  assert.equal(chat.inputMsg.value, '')
  assert.deepEqual(chat.attachments.value, [])
  assert.deepEqual(composer.uploadFileList.value, [])

  const queuedList = [{ uid: 'existing-upload', name: 'existing.txt' }]
  chat.inputMsg.value = '  '
  chat.attachments.value = []
  composer.uploadFileList.value = queuedList

  await composer.send()

  assert.equal(enqueuePayloads.length, 1)
  assert.equal(chat.inputMsg.value, '  ')
  assert.deepEqual(chat.attachments.value, [])
  assert.deepEqual(composer.uploadFileList.value, queuedList)
})

test('stopping or submitting settings blocks send without changing input or attachments', async () => {
  const blockedCases = [
    ['stopping', 'isStopping'],
    ['mode', 'modeSettingSubmitting'],
    ['agent', 'agentSettingSubmitting']
  ]

  for (const [, blocker] of blockedCases) {
    const fixture = makeFixture()
    const { chat, composer, agentSettingSubmitting, sendPayloads, enqueuePayloads } = fixture
    const attachment = { uid: `blocked-${blocker}`, name: 'blocked.txt', path: '/blocked.txt' }
    const uploadList = [{ uid: attachment.uid, name: attachment.name }]

    if (blocker === 'agentSettingSubmitting') {
      agentSettingSubmitting.value = true
    } else {
      chat[blocker].value = true
    }
    chat.loading.value = true
    chat.inputMsg.value = `keep-${blocker}`
    chat.attachments.value = [attachment]
    composer.uploadFileList.value = uploadList

    await composer.send()

    assert.deepEqual(sendPayloads, [])
    assert.deepEqual(enqueuePayloads, [])
    assert.equal(chat.inputMsg.value, `keep-${blocker}`)
    assert.deepEqual(chat.attachments.value, [attachment])
    assert.deepEqual(composer.uploadFileList.value, uploadList)
  }
})

test('creates external guidance with trimmed payload, deduplicates db_id, and isolates late responses', async () => {
  const fixture = makeFixture()
  const {
    chat,
    composer,
    api,
    guidanceRequests,
    notifications,
    sessionsLoaded,
    scrollCalls
  } = fixture

  chat.isCurrentSessionReadOnly.value = true
  chat.currentSessionId.value = 'session-a'
  chat.inputMsg.value = '  first guidance  '
  api.guidanceResponse = {
    data: {
      data: {
        id: 'persistent-guidance-1',
        db_id: 'db-guidance-1',
        role: 'user',
        content: 'first guidance'
      }
    }
  }

  await composer.send()

  assert.deepEqual(guidanceRequests, [{
    session_id: 'session-a',
    content: 'first guidance'
  }])
  assert.deepEqual(chat.messages.value, [{
    id: 'persistent-guidance-1',
    db_id: 'db-guidance-1',
    role: 'user',
    content: 'first guidance'
  }])
  assert.equal(chat.inputMsg.value, '')
  assert.deepEqual(scrollCalls, ['auto'])
  assert.deepEqual(notifications.success, ['chat.guidance_created'])
  assert.equal(sessionsLoaded.length, 1)

  chat.messages.value.push({ db_id: 'db-existing', role: 'user', content: 'existingmessage' })
  chat.inputMsg.value = 'existingmessage'
  api.guidanceResponse = {
    data: {
      data: {
        id: 'persistent-guidance-2',
        db_id: 'db-existing',
        role: 'user',
        content: 'existingmessage'
      }
    }
  }

  await composer.send()

  assert.deepEqual(guidanceRequests[1], {
    session_id: 'session-a',
    content: 'existingmessage'
  })
  assert.equal(chat.messages.value.length, 2)
  assert.equal(chat.inputMsg.value, '')
  assert.equal(sessionsLoaded.length, 2)

  const pending = deferred()
  const lateFixture = makeFixture()
  lateFixture.chat.isCurrentSessionReadOnly.value = true
  lateFixture.chat.currentSessionId.value = 'session-a'
  lateFixture.chat.inputMsg.value = '  late A  '
  lateFixture.api.createGuidance = payload => {
    lateFixture.guidanceRequests.push({ ...payload })
    return pending.promise
  }

  const firstSend = lateFixture.composer.send()
  await nextTick()

  const repeatedSend = lateFixture.composer.send()
  await repeatedSend
  assert.equal(lateFixture.guidanceRequests.length, 1)
  assert.equal(lateFixture.composer.guidanceSubmitting.value, true)

  lateFixture.chat.currentSessionId.value = 'session-b'
  lateFixture.chat.inputMsg.value = 'new B input'
  pending.resolve({
    data: {
      data: {
        id: 'late-guidance-a',
        role: 'user',
        content: 'late A'
      }
    }
  })
  await firstSend

  assert.deepEqual(lateFixture.chat.messages.value, [])
  assert.equal(lateFixture.chat.inputMsg.value, 'new B input')
  assert.equal(lateFixture.composer.guidanceSubmitting.value, false)
})

test('sends each audit decision word and blocks decisions for read-only, loading, or agent submission', async () => {
  const fixture = makeFixture()
  const { chat, composer, agentSettingSubmitting, sendPayloads } = fixture
  const decisions = [
    ['approve', 'chat.audit_approve_word'],
    ['ignore', 'chat.audit_ignore_word'],
    ['reject', 'chat.audit_reject_word']
  ]

  for (const [decision, translatedWord] of decisions) {
    chat.inputMsg.value = 'old audit input'
    chat.attachments.value = [{ uid: `${decision}-file`, path: `/${decision}` }]

    await composer.handleAuditDecision({ decision })

    assert.equal(chat.inputMsg.value, translatedWord)
    assert.deepEqual(chat.attachments.value, [])
  }

  assert.deepEqual(sendPayloads, decisions.map(([, translatedWord]) => ({
    content: translatedWord,
    attachments: []
  })))

  const blockedCases = [
    ['read-only', () => { chat.isCurrentSessionReadOnly.value = true }],
    ['loading', () => { chat.loading.value = true }],
    ['agent', () => { agentSettingSubmitting.value = true }]
  ]

  for (const [, setBlocker] of blockedCases) {
    chat.isCurrentSessionReadOnly.value = false
    chat.loading.value = false
    agentSettingSubmitting.value = false
    setBlocker()
    chat.inputMsg.value = 'preserve audit input'
    chat.attachments.value = [{ uid: 'preserve-file', path: '/preserve' }]
    const inputBefore = chat.inputMsg.value
    const attachmentsBefore = [...chat.attachments.value]
    const sendCountBefore = sendPayloads.length

    await composer.handleAuditDecision({ decision: 'approve' })

    assert.equal(sendPayloads.length, sendCountBefore)
    assert.equal(chat.inputMsg.value, inputBefore)
    assert.deepEqual(chat.attachments.value, attachmentsBefore)
  }
})

test('uploads plain files, records backend paths, reports callbacks, and removes repeatedly', async () => {
  const fixture = makeFixture()
  const {
    chat,
    composer,
    fileApi,
    uploadRequests,
    notifications
  } = fixture
  const file = {
    uid: 'plain-file-1',
    name: 'report.txt',
    type: 'text/plain',
    size: 12
  }
  const successResults = []
  const errors = []

  await composer.handleUpload({
    file,
    onSuccess: value => successResults.push(value),
    onError: error => errors.push(error)
  })

  assert.deepEqual(uploadRequests, [{ file, sessionId: 'session-a' }])
  assert.deepEqual(chat.attachments.value, [{
    uid: 'plain-file-1',
    name: 'server-file.txt',
    path: '/uploads/server-file.txt'
  }])
  assert.deepEqual(composer.uploadFileList.value, [{
    uid: 'plain-file-1',
    name: 'report.txt',
    status: 'success',
    url: ''
  }])
  assert.deepEqual(successResults, [fileApi.uploadResponse.data])
  assert.deepEqual(errors, [])
  assert.deepEqual(notifications.success, ['chat.upload_success'])

  composer.handleRemoveCustomFile(file)
  composer.handleRemoveCustomFile(file)
  assert.deepEqual(chat.attachments.value, [])
  assert.deepEqual(composer.uploadFileList.value, [])

  const failedFixture = makeFixture()
  const failedError = new Error('backend rejected file')
  failedFixture.fileApi.upload = async () => {
    throw failedError
  }
  const failedSuccess = []
  const failedErrors = []

  await failedFixture.composer.handleUpload({
    file: {
      uid: 'failed-file',
      name: 'failed.txt',
      type: 'text/plain'
    },
    onSuccess: value => failedSuccess.push(value),
    onError: error => failedErrors.push(error)
  })

  assert.deepEqual(failedSuccess, [])
  assert.deepEqual(failedErrors, [failedError])
  assert.deepEqual(failedFixture.chat.attachments.value, [])
  assert.deepEqual(failedFixture.composer.uploadFileList.value, [])
  assert.deepEqual(failedFixture.notifications.error, ['backend rejected file'])

  const readOnlyFixture = makeFixture()
  readOnlyFixture.chat.isCurrentSessionReadOnly.value = true
  const readOnlyErrors = []
  await readOnlyFixture.composer.handleUpload({
    file: {
      uid: 'readonly-file',
      name: 'readonly.txt',
      type: 'text/plain'
    },
    onError: error => readOnlyErrors.push(error)
  })

  assert.deepEqual(readOnlyFixture.uploadRequests, [])
  assert.equal(readOnlyErrors.length, 1)
  assert.equal(readOnlyErrors[0].message, 'chat.external_session_read_only')
  assert.deepEqual(readOnlyFixture.notifications.warning, ['chat.external_session_read_only'])
  assert.deepEqual(readOnlyFixture.chat.attachments.value, [])
})

test('pastes explicit clipboard files through upload, ignores directories, and leaves empty or read-only state unchanged', async () => {
  const fixture = makeFixture()
  const { chat, composer, uploadRequests } = fixture
  const pastedFile = {
    name: 'pasted.txt',
    type: 'text/plain',
    size: 7
  }
  const directoryFile = {
    name: 'folder-entry',
    type: '',
    size: 0
  }
  const directoryItem = {
    kind: 'file',
    getAsFile: () => directoryFile,
    webkitGetAsEntry: () => ({ isDirectory: true })
  }
  const fileItem = {
    kind: 'file',
    getAsFile: () => pastedFile,
    webkitGetAsEntry: () => ({ isDirectory: false })
  }

  composer.handlePaste({
    clipboardData: {
      items: [fileItem, directoryItem]
    }
  })
  assert.equal(uploadRequests.length, 1)
  assert.equal(uploadRequests[0].file, pastedFile)
  assert.equal(typeof pastedFile.uid, 'number')
  await nextTick()
  await nextTick()

  assert.deepEqual(chat.attachments.value, [{
    uid: pastedFile.uid,
    name: 'server-file.txt',
    path: '/uploads/server-file.txt'
  }])

  const uploadCount = uploadRequests.length
  const attachmentsBeforeEmptyPaste = [...chat.attachments.value]
  composer.handlePaste({ clipboardData: { items: [] } })
  composer.handlePaste({ clipboardData: {} })
  await nextTick()
  assert.equal(uploadRequests.length, uploadCount)
  assert.deepEqual(chat.attachments.value, attachmentsBeforeEmptyPaste)

  const readOnlyFixture = makeFixture()
  readOnlyFixture.chat.isCurrentSessionReadOnly.value = true
  const existingAttachment = { uid: 'existing', name: 'existing.txt', path: '/existing' }
  readOnlyFixture.chat.attachments.value = [existingAttachment]
  readOnlyFixture.composer.uploadFileList.value = [{ uid: 'existing', name: 'existing.txt' }]

  readOnlyFixture.composer.handlePaste({
    clipboardData: {
      items: [fileItem]
    }
  })
  await nextTick()

  assert.deepEqual(readOnlyFixture.uploadRequests, [])
  assert.deepEqual(readOnlyFixture.chat.attachments.value, [existingAttachment])
  assert.deepEqual(readOnlyFixture.composer.uploadFileList.value, [{
    uid: 'existing',
    name: 'existing.txt'
  }])
})
