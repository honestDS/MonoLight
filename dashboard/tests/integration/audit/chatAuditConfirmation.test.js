import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import { ref } from 'vue'
import enChat from '../../../src/i18n/locales/en/chat.js'
import enCommon from '../../../src/i18n/locales/en/common.js'
import zhChat from '../../../src/i18n/locales/zh/chat.js'
import zhCommon from '../../../src/i18n/locales/zh/common.js'
import { isAuditConfirmationActionable } from '../../../src/utils/auditConfirmation.js'
import { loadUseConfirmDialog } from '../../confirmDialogHarness.js'

const auditConfirmationSource = readFileSync(
  new URL('../../../src/composables/chat/useChatAuditConfirmation.js', import.meta.url),
  'utf8',
)
  .replace(/^[ \t]*import[^\r\n]*(?:\r?\n|$)/gm, '')
  .replace(/^export\s+function\s+/m, 'function ')

const loadUseChatAuditConfirmation = ({ clockDate, window, onUnmounted, useConfirmDialog }) => (
  vm.runInNewContext(
    `${auditConfirmationSource}\nuseChatAuditConfirmation`,
    {
      Date: clockDate,
      window,
      onUnmounted,
      ref,
      useConfirmDialog,
      isAuditConfirmationActionable,
    },
    { filename: 'useChatAuditConfirmation.js' },
  )
)

const toHost = value => value === undefined ? value : JSON.parse(JSON.stringify(value))

const localeMessages = {
  en: { chat: enChat, common: enCommon },
  zh: { chat: zhChat, common: zhCommon },
}

const createTranslate = (locale = 'en') => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: localeMessages,
  })

  const t = (...args) => i18n.global.t(
    ...args.map((value, index) => (
      index > 0 && value && typeof value === 'object' && !Array.isArray(value)
        ? Object.fromEntries(Object.entries(value))
        : value
    )),
  )
  return { t }
}

const createAuditMessage = ({
  auditRecordId = 42,
  status = 'pending',
  confirmationMode = 'high_risk_override',
  confirmationKind,
  risk = 95,
  expiresAt = '2026-07-29T00:01:00.000Z',
  ...overrides
} = {}) => ({
  id: `audit-message-${auditRecordId}`,
  request_id: 'request-1',
  work_id: 'work-1',
  role: 'assistant',
  type: 'audit_confirmation',
  content: JSON.stringify({
    type: 'audit_confirmation',
    audit_record_id: auditRecordId,
    summary: 'Delete production records',
    risk,
    status,
    confirmation_mode: confirmationMode,
    confirmation_kind: confirmationKind,
    expires_at: expiresAt,
    plain_text: 'Audit confirmation for deleting production records',
  }),
  ...overrides,
})

const setAuditStatus = (message, status) => {
  const content = JSON.parse(message.content)
  message.content = JSON.stringify({ ...content, status })
}

const createHarness = ({ message = createAuditMessage(), locale = 'en' } = {}) => {
  let now = Date.parse('2026-07-29T00:00:00.000Z')
  const timerCalls = []
  const clearedTimerIds = []
  const unmountedCallbacks = []
  const confirmations = []
  const pendingDialogs = []
  const emitted = []
  const props = {
    messages: [message],
    currentSessionReadOnly: false,
    confirmationBusy: false,
  }
  const window = {
    setInterval(callback, delay) {
      const id = timerCalls.length + 1
      timerCalls.push({ id, callback, delay })
      return id
    },
    clearInterval(id) {
      clearedTimerIds.push(id)
    },
  }
  const clockDate = {
    now: () => now,
    parse: value => Date.parse(value),
  }
  const { t } = createTranslate(locale)
  const ElMessageBox = {
    confirm(dialogMessage, title, options) {
      confirmations.push({
        message: dialogMessage,
        title,
        options: Object.fromEntries(Object.entries(options)),
      })
      return new Promise((resolve, reject) => {
        let settled = false
        pendingDialogs.push({
          get settled() {
            return settled
          },
          resolve(value) {
            if (settled) return
            settled = true
            resolve(value)
          },
          reject(reason) {
            if (settled) return
            settled = true
            reject(reason)
          },
        })
      })
    },
  }
  const actualUseConfirmDialog = loadUseConfirmDialog({ t, ElMessageBox })
  const emit = (...args) => emitted.push(toHost(args))
  const useChatAuditConfirmation = loadUseChatAuditConfirmation({
    clockDate,
    window,
    onUnmounted: callback => unmountedCallbacks.push(callback),
    useConfirmDialog: actualUseConfirmDialog,
  })
  const api = useChatAuditConfirmation({ props, emit, t })
  let cleaned = false

  return {
    api,
    message,
    props,
    confirmations,
    emitted,
    timerCalls,
    clearedTimerIds,
    setNow(value) {
      now = typeof value === 'number' ? value : Date.parse(value)
      for (const { callback } of timerCalls) callback()
    },
    settleDialog(index, action) {
      const dialog = pendingDialogs[index]
      assert.ok(dialog, `dialog ${index} must exist`)
      if (action === 'confirm') dialog.resolve()
      else dialog.reject(action)
    },
    cleanup() {
      if (cleaned) return
      cleaned = true
      for (const dialog of pendingDialogs) {
        if (!dialog.settled) dialog.reject('close')
      }
      for (const callback of unmountedCallbacks) callback()
    },
  }
}

const withHarness = async (callback, options = {}) => {
  const harness = createHarness(options)
  try {
    return await callback(harness)
  } finally {
    harness.cleanup()
    assert.deepEqual(
      harness.clearedTimerIds,
      harness.timerCalls.map(({ id }) => id),
    )
  }
}

test('confirms high-risk ignore once through the real warning dialog', async () => {
  await withHarness(async harness => {
    const { api, message } = harness
    assert.equal(api.isHighRiskAuditConfirmation(message), true)
    assert.equal(api.getAuditConfirmation(message).status, 'pending')
    assert.equal(api.isAuditConfirmationExpired(message), false)
    assert.equal(
      isAuditConfirmationActionable(JSON.parse(message.content), Date.parse('2026-07-29T00:00:00.000Z')),
      true,
    )

    const firstDecision = api.submitAuditDecision(message, 'ignore')
    assert.equal(harness.confirmations.length, 1)
    assert.equal(harness.emitted.length, 0)
    assert.equal(api.isAuditDecisionPending(message), true)
    assert.equal(api.getAuditStatusLabel(message), 'Pending')

    const warning = harness.confirmations[0]
    assert.equal(warning.title, enChat.audit_high_risk_warning_title)
    assert.equal(warning.options.type, 'error')
    assert.equal(warning.options.confirmButtonText, enChat.audit_ignore_and_allow)
    assert.equal(warning.options.cancelButtonText, enCommon.cancel)
    assert.equal(warning.options.dangerouslyUseHTMLString, false)
    assert.equal(warning.options.customClass, 'app-confirm-dialog')
    const html = await renderToString(warning.message)
    assert.ok(html.includes(enChat.audit_high_risk_warning))
    assert.match(html, /confirm-dialog-content__message/)
    assert.doesNotMatch(html, /<(?:script|img)\b/i)

    const duplicateDecision = api.submitAuditDecision(message, 'ignore')
    await duplicateDecision
    assert.equal(harness.confirmations.length, 1)
    assert.equal(harness.emitted.length, 0)

    harness.settleDialog(0, 'confirm')
    await firstDecision
    assert.equal(harness.emitted.length, 1)
    assert.deepEqual(harness.emitted[0], [
      'audit-decision',
      { auditRecordId: 42, decision: 'ignore' },
    ])
    assert.equal(api.isAuditDecisionPending(message), true)
    assert.equal(api.getAuditConfirmation(message).status, 'executing')
    assert.equal(api.getAuditStatusLabel(message), 'Executing')
  })
})

test('cancel, close, and dialog errors clear pending and allow retry', async () => {
  for (const action of ['cancel', 'close', 'error']) {
    await withHarness(async harness => {
      const { api, message } = harness
      const firstDecision = api.submitAuditDecision(message, 'ignore')
      assert.equal(api.isAuditDecisionPending(message), true, action)
      harness.settleDialog(0, action === 'error' ? new Error('dialog failed') : action)
      await firstDecision

      assert.equal(harness.emitted.length, 0, action)
      assert.equal(api.isAuditDecisionPending(message), false, action)
      assert.equal(api.getAuditConfirmation(message).status, 'pending', action)
      assert.equal(api.getAuditStatusLabel(message), 'Pending', action)

      const retryDecision = api.submitAuditDecision(message, 'ignore')
      assert.equal(harness.confirmations.length, 2, action)
      harness.settleDialog(1, 'confirm')
      await retryDecision
      assert.deepEqual(harness.emitted, [[
        'audit-decision',
        { auditRecordId: 42, decision: 'ignore' },
      ]], action)
      assert.equal(api.getAuditConfirmation(message).status, 'executing', action)
    })
  }
})

const staleCases = [
  {
    name: 'status becomes expired',
    apply: ({ message }) => setAuditStatus(message, 'expired'),
    expectedStatus: 'expired',
  },
  {
    name: 'status becomes executing',
    apply: ({ message }) => setAuditStatus(message, 'executing'),
    expectedStatus: 'executing',
  },
  {
    name: 'status becomes rejected',
    apply: ({ message }) => setAuditStatus(message, 'rejected'),
    expectedStatus: 'rejected',
  },
  {
    name: 'expiry reaches now',
    apply: ({ harness, message }) => harness.setNow(JSON.parse(message.content).expires_at),
    expectedStatus: 'pending',
    expectedLabel: 'Expired',
  },
  {
    name: 'record is removed',
    apply: ({ harness }) => { harness.props.messages = [] },
    expectedStatus: 'pending',
  },
  {
    name: 'record is replaced by another id',
    apply: ({ harness }) => { harness.props.messages = [createAuditMessage({ auditRecordId: 43 })] },
    expectedStatus: 'pending',
  },
  {
    name: 'session becomes read-only',
    apply: ({ harness }) => { harness.props.currentSessionReadOnly = true },
    expectedStatus: 'pending',
  },
]

test('does not emit when the audit record becomes stale while confirmation is open', async () => {
  for (const staleCase of staleCases) {
    await withHarness(async harness => {
      const { api, message } = harness
      const firstDecision = api.submitAuditDecision(message, 'ignore')
      assert.equal(api.isAuditDecisionPending(message), true, staleCase.name)
      staleCase.apply({ harness, message })
      harness.settleDialog(0, 'confirm')
      await firstDecision

      assert.deepEqual(harness.emitted, [], staleCase.name)
      assert.equal(api.isAuditDecisionPending(message), false, staleCase.name)
      assert.equal(api.getAuditConfirmation(message).status, staleCase.expectedStatus, staleCase.name)
      if (staleCase.expectedLabel) {
        assert.equal(api.getAuditStatusLabel(message), staleCase.expectedLabel, staleCase.name)
      }
    })
  }
})

test('approve and reject keep their direct non-ignore behavior', async () => {
  for (const [decision, expectedStatus, expectedLabel] of [
    ['approve', 'executing', 'Executing'],
    ['reject', 'rejected', 'Rejected'],
  ]) {
    await withHarness(async harness => {
      const { api, message } = harness
      await api.submitAuditDecision(message, decision)
      assert.equal(harness.confirmations.length, 0, decision)
      assert.deepEqual(harness.emitted, [[
        'audit-decision',
        { auditRecordId: 42, decision },
      ]], decision)
      assert.equal(api.isAuditDecisionPending(message), true, decision)
      assert.equal(api.getAuditConfirmation(message).status, expectedStatus, decision)
      assert.equal(api.getAuditStatusLabel(message), expectedLabel, decision)
    })
  }
})

test('renders pure system confirmations in both locales and sends each button decision once', async () => {
  for (const locale of ['en', 'zh']) {
    for (const decision of ['approve', 'reject']) {
      await withHarness(async harness => {
        const { api, message } = harness
        const expectedChat = localeMessages[locale].chat

        assert.equal(api.isSystemConfirmation(message), true, locale)
        assert.equal(api.getAuditConfirmation(message).confirmation_mode, 'standard', locale)
        assert.equal(api.getAuditConfirmation(message).risk, null, locale)
        assert.equal(api.getConfirmationTitle(message), expectedChat.system_confirmation_title, locale)
        assert.equal(api.getConfirmationApproveLabel(message), expectedChat.system_confirmation_approve, locale)
        assert.equal(api.getConfirmationRejectLabel(message), expectedChat.system_confirmation_reject, locale)

        const firstDecision = api.submitAuditDecision(message, decision)
        const duplicateDecision = api.submitAuditDecision(message, decision)
        await Promise.all([firstDecision, duplicateDecision])

        assert.equal(harness.confirmations.length, 0, `${locale} ${decision}`)
        assert.deepEqual(harness.emitted, [[
          'audit-decision',
          { auditRecordId: 42, decision },
        ]], `${locale} ${decision}`)
      }, {
        locale,
        message: createAuditMessage({
          confirmationKind: 'system',
          confirmationMode: 'standard',
          risk: null,
        }),
      })
    }
  }
})

test('does not allow ignore for pure system confirmations', async () => {
  await withHarness(async harness => {
    const { api, message } = harness
    await api.submitAuditDecision(message, 'ignore')

    assert.equal(harness.confirmations.length, 0)
    assert.deepEqual(harness.emitted, [])
  }, {
    message: createAuditMessage({
      confirmationKind: 'system',
      confirmationMode: 'standard',
      risk: null,
    }),
  })
})

test('does not emit system button decisions after expiry, read-only changes, or cancellation', async () => {
  const staleSystemCases = [
    {
      name: 'expiry reaches now',
      apply: ({ harness, message }) => harness.setNow(JSON.parse(message.content).expires_at),
    },
    {
      name: 'session becomes read-only',
      apply: ({ harness }) => { harness.props.currentSessionReadOnly = true },
    },
    {
      name: 'status becomes cancelled',
      apply: ({ message }) => setAuditStatus(message, 'cancelled'),
    },
  ]

  for (const staleCase of staleSystemCases) {
    await withHarness(async harness => {
      const { api, message } = harness
      staleCase.apply({ harness, message })

      await Promise.all([
        api.submitAuditDecision(message, 'approve'),
        api.submitAuditDecision(message, 'reject'),
      ])

      assert.deepEqual(harness.emitted, [], staleCase.name)
      assert.equal(harness.confirmations.length, 0, staleCase.name)
    }, {
      message: createAuditMessage({
        confirmationKind: 'system',
        confirmationMode: 'standard',
        risk: null,
      }),
    })
  }
})

test('keeps audit presentation and approve behavior for standard mixed confirmations', async () => {
  await withHarness(async harness => {
    const { api, message } = harness

    assert.equal(api.isSystemConfirmation(message), false)
    assert.equal(api.getConfirmationTitle(message), enChat.mixed_confirmation_title)
    assert.equal(api.getConfirmationApproveLabel(message), enChat.audit_approve)
    assert.equal(api.getConfirmationRejectLabel(message), enChat.audit_reject)

    await api.submitAuditDecision(message, 'approve')
    assert.equal(harness.confirmations.length, 0)
    assert.deepEqual(harness.emitted, [[
      'audit-decision',
      { auditRecordId: 42, decision: 'approve' },
    ]])
  }, {
    message: createAuditMessage({
      confirmationKind: 'mixed',
      confirmationMode: 'standard',
      risk: 5,
    }),
  })
})

test('uses the high-risk ignore dialog for high-risk mixed confirmations', async () => {
  await withHarness(async harness => {
    const { api, message } = harness
    const decision = api.submitAuditDecision(message, 'ignore')

    assert.equal(api.getConfirmationTitle(message), enChat.audit_high_risk_warning_title)
    assert.equal(api.getConfirmationApproveLabel(message), enChat.audit_ignore_and_allow)
    assert.equal(harness.confirmations.length, 1)
    assert.equal(harness.confirmations[0].title, enChat.audit_high_risk_warning_title)
    assert.equal(harness.confirmations[0].options.confirmButtonText, enChat.audit_ignore_and_allow)
    assert.equal(harness.confirmations[0].options.cancelButtonText, enCommon.cancel)

    harness.settleDialog(0, 'confirm')
    await decision
    assert.deepEqual(harness.emitted, [[
      'audit-decision',
      { auditRecordId: 42, decision: 'ignore' },
    ]])
  }, {
    message: createAuditMessage({
      confirmationKind: 'mixed',
      confirmationMode: 'high_risk_override',
      risk: 95,
    }),
  })
})

test('blocks standard button decisions while confirmation is busy and retries after it clears', async () => {
  const standardCases = [
    { name: 'system', confirmationKind: 'system', risk: null },
    { name: 'mixed', confirmationKind: 'mixed', risk: 5 },
    { name: 'audit', confirmationKind: 'audit', risk: 5 },
  ]

  for (const standardCase of standardCases) {
    for (const [decision, expectedStatus] of [
      ['approve', 'executing'],
      ['reject', 'rejected'],
    ]) {
      await withHarness(async harness => {
        const { api, message } = harness
        const caseName = `${standardCase.name} ${decision}`
        harness.props.confirmationBusy = true

        await api.submitAuditDecision(message, decision)
        assert.deepEqual(harness.emitted, [], `${caseName} while busy`)
        assert.equal(harness.confirmations.length, 0, `${caseName} while busy`)
        assert.equal(api.isAuditDecisionPending(message), false, `${caseName} while busy`)
        assert.equal(api.getAuditConfirmation(message).status, 'pending', `${caseName} while busy`)

        harness.props.confirmationBusy = false
        await api.submitAuditDecision(message, decision)
        assert.deepEqual(harness.emitted, [[
          'audit-decision',
          { auditRecordId: 42, decision },
        ]], caseName)
        assert.equal(api.isAuditDecisionPending(message), true, caseName)
        assert.equal(api.getAuditConfirmation(message).status, expectedStatus, caseName)
      }, {
        message: createAuditMessage({
          confirmationKind: standardCase.confirmationKind,
          confirmationMode: 'standard',
          risk: standardCase.risk,
        }),
      })
    }
  }
})

test('rechecks confirmation busy state before emitting a mixed high-risk ignore decision', async () => {
  await withHarness(async harness => {
    const { api, message } = harness
    const firstDecision = api.submitAuditDecision(message, 'ignore')

    assert.equal(harness.confirmations.length, 1)
    assert.equal(api.isAuditDecisionPending(message), true)

    harness.props.confirmationBusy = true
    harness.settleDialog(0, 'confirm')
    await firstDecision

    assert.deepEqual(harness.emitted, [])
    assert.equal(api.isAuditDecisionPending(message), false)
    assert.equal(api.getAuditConfirmation(message).status, 'pending')

    harness.props.confirmationBusy = false
    const retryDecision = api.submitAuditDecision(message, 'ignore')
    assert.equal(harness.confirmations.length, 2)
    assert.equal(harness.confirmations[1].title, enChat.audit_high_risk_warning_title)
    assert.equal(api.isAuditDecisionPending(message), true)

    harness.settleDialog(1, 'confirm')
    await retryDecision
    assert.deepEqual(harness.emitted, [[
      'audit-decision',
      { auditRecordId: 42, decision: 'ignore' },
    ]])
    assert.equal(api.isAuditDecisionPending(message), true)
    assert.equal(api.getAuditConfirmation(message).status, 'executing')
  }, {
    message: createAuditMessage({
      confirmationKind: 'mixed',
      confirmationMode: 'high_risk_override',
      risk: 95,
    }),
  })
})
