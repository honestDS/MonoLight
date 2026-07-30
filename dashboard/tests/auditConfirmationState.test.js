import assert from 'node:assert/strict'
import test from 'node:test'
import {
  applyAuditConfirmationStatusToMessages,
  applyAuditToolResultsUpdateToMessages
} from '../src/composables/chat/auditConfirmationState.js'

const createMessage = (overrides = {}) => ({
  id: 'local-message-id',
  request_id: 'request-1',
  work_id: 'work-1',
  type: 'audit_confirmation',
  content: JSON.stringify({
    type: 'audit_confirmation',
    audit_record_id: 'audit-1',
    status: 'pending',
    summary: 'Review deployment',
    risk: 'high',
    expires_at: '2026-08-01T00:00:00.000Z'
  }),
  ...overrides
})

test('server confirmation status merges content, prioritizes data status, and stores its database id', () => {
  const message = createMessage()
  const result = applyAuditConfirmationStatusToMessages([message], {
    audit_record_id: 'audit-1',
    message_id: 42,
    content: JSON.stringify({
      plain_text: 'Approval is being executed',
      server_field: 'server value',
      status: 'pending'
    }),
    status: 'succeeded'
  })
  const confirmation = JSON.parse(result.messages[0].content)

  assert.equal(result.updated, true)
  assert.equal(result.messages[0].id, 'local-message-id')
  assert.equal(result.messages[0].request_id, 'request-1')
  assert.equal(result.messages[0].db_id, 42)
  assert.equal(confirmation.summary, 'Review deployment')
  assert.equal(confirmation.plain_text, 'Approval is being executed')
  assert.equal(confirmation.server_field, 'server value')
  assert.equal(confirmation.status, 'succeeded')
})

test('invalid server confirmation content still applies a valid status without losing card fields', () => {
  const message = createMessage()
  const messages = [message]
  const result = applyAuditConfirmationStatusToMessages(messages, {
    audit_record_id: 'audit-1',
    content: '{invalid json',
    status: 'failed'
  })
  const confirmation = JSON.parse(result.messages[0].content)

  assert.equal(result.updated, true)
  assert.notEqual(result.messages, messages)
  assert.notEqual(result.messages[0], message)
  assert.equal(confirmation.status, 'failed')
  assert.equal(confirmation.type, 'audit_confirmation')
  assert.equal(confirmation.audit_record_id, 'audit-1')
  assert.equal(confirmation.summary, 'Review deployment')
  assert.equal(confirmation.risk, 'high')
  assert.equal(confirmation.expires_at, '2026-08-01T00:00:00.000Z')
})

test('matches numeric and string audit record ids', () => {
  const message = createMessage({
    content: JSON.stringify({
      type: 'audit_confirmation',
      audit_record_id: 101,
      status: 'pending'
    })
  })

  const result = applyAuditConfirmationStatusToMessages([message], {
    audit_record_id: '101',
    status: 'executing'
  })

  assert.equal(result.updated, true)
  assert.equal(JSON.parse(result.messages[0].content).status, 'executing')
})

test('leaves messages unchanged for unrelated audit records and invalid server status', () => {
  const messages = [createMessage()]
  const unrelated = applyAuditConfirmationStatusToMessages(messages, {
    audit_record_id: 'other-audit',
    status: 'executing'
  })
  const invalid = applyAuditConfirmationStatusToMessages(messages, {
    audit_record_id: 'audit-1',
    status: ''
  })

  assert.equal(unrelated.updated, false)
  assert.equal(unrelated.messages, messages)
  assert.equal(invalid.updated, false)
  assert.equal(invalid.messages, messages)
})

test('updates only the target confirmation card', () => {
  const target = createMessage()
  const other = createMessage({
    id: 'local-message-id-2',
    content: JSON.stringify({
      type: 'audit_confirmation',
      audit_record_id: 'audit-2',
      status: 'pending'
    })
  })
  const messages = [target, other]

  const result = applyAuditConfirmationStatusToMessages(messages, {
    audit_record_id: 'audit-1',
    status: 'rejected'
  })

  assert.equal(JSON.parse(result.messages[0].content).status, 'rejected')
  assert.equal(result.messages[1], other)
  assert.equal(JSON.parse(result.messages[1].content).status, 'pending')
})

const createToolResultMessage = (overrides = {}) => ({
  id: 'local-tool-result-id',
  request_id: 'request-1',
  work_id: 'work-1',
  role: 'tool',
  content: JSON.stringify({
    role: 'tool',
    tool_call_id: 'tool-call-1',
    content: JSON.stringify({
      status: 'pending',
      result: 'Awaiting approval'
    })
  }),
  ...overrides
})

const createRemoteToolResult = (overrides = {}) => ({
  id: 42,
  type: 'tool_result',
  role: 'tool',
  content: JSON.stringify({
    role: 'tool',
    tool_call_id: 'tool-call-1',
    content: JSON.stringify({
      status: 'rejected',
      result: 'Rejected by user'
    })
  }),
  ...overrides
})

test('merges a remote rejected tool result into a serialized local InternalMessage', () => {
  const local = createToolResultMessage()
  const messages = [local]
  const result = applyAuditToolResultsUpdateToMessages(messages, {
    messages: [createRemoteToolResult()]
  })
  const content = JSON.parse(result.messages[0].content)

  assert.equal(result.updated, true)
  assert.equal(result.messages.length, 1)
  assert.equal(result.messages[0].id, 'local-tool-result-id')
  assert.equal(result.messages[0].request_id, 'request-1')
  assert.equal(result.messages[0].work_id, 'work-1')
  assert.equal(result.messages[0].db_id, 42)
  assert.equal(result.messages[0].type, 'tool_result')
  assert.equal(JSON.parse(content.content).status, 'rejected')
  assert.equal(JSON.parse(content.content).result, 'Rejected by user')
})

test('updates only the targeted tool result and accepts a succeeded server status', () => {
  const target = createToolResultMessage()
  const other = createToolResultMessage({
    id: 'local-tool-result-id-2',
    content: JSON.stringify({
      role: 'tool',
      tool_call_id: 'tool-call-2',
      content: JSON.stringify({ status: 'executing' })
    })
  })
  const result = applyAuditToolResultsUpdateToMessages([target, other], {
    messages: [createRemoteToolResult({
      id: 43,
      content: JSON.stringify({
        role: 'tool',
        tool_call_id: 'tool-call-1',
        content: JSON.stringify({ status: 'succeeded' })
      })
    })]
  })

  assert.equal(result.messages.length, 2)
  assert.equal(result.messages[0].db_id, 43)
  assert.equal(JSON.parse(JSON.parse(result.messages[0].content).content).status, 'succeeded')
  assert.equal(result.messages[1], other)
  assert.equal(JSON.parse(JSON.parse(result.messages[1].content).content).status, 'executing')
})

test('does not apply hidden tool results when tool output is disabled', () => {
  const messages = [createToolResultMessage()]
  const result = applyAuditToolResultsUpdateToMessages(messages, {
    messages: [createRemoteToolResult()]
  }, false)

  assert.equal(result.updated, false)
  assert.equal(result.messages, messages)
})

test('leaves the original array unchanged when tool result messages are invalid', () => {
  const messages = [createToolResultMessage()]
  const result = applyAuditToolResultsUpdateToMessages(messages, {
    messages: [null, 'invalid', [], {}]
  })

  assert.equal(result.updated, false)
  assert.equal(result.messages, messages)
})
