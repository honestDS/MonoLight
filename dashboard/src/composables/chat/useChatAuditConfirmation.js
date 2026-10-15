import { onUnmounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { isAuditConfirmationActionable } from '../../utils/auditConfirmation.js'

export function useChatAuditConfirmation({ props, emit, t }) {
  const pendingAuditDecisions = ref(new Set())
  const auditDecisionStatuses = ref(new Map())
  const auditCountdownNow = ref(Date.now())
  const auditCountdownTimer = window.setInterval(() => {
    auditCountdownNow.value = Date.now()
  }, 1000)

  onUnmounted(() => {
    window.clearInterval(auditCountdownTimer)
  })

  const parseAuditConfirmation = (content) => {
    try {
      const parsed = typeof content === 'string' ? JSON.parse(content) : content
      return parsed?.type === 'audit_confirmation' ? parsed : null
    } catch {
      return null
    }
  }
  const hasAuditRecordId = value => value !== null && value !== undefined && String(value).trim() !== ''
  const hasSameAuditRecordId = (left, right) => hasAuditRecordId(left) && hasAuditRecordId(right) && String(left) === String(right)
  const findLatestAuditConfirmation = (auditRecordId) => {
    if (!hasAuditRecordId(auditRecordId)) return null

    for (let index = props.messages.length - 1; index >= 0; index -= 1) {
      const message = props.messages[index]
      if (message?.type !== 'audit_confirmation') continue

      const confirmation = parseAuditConfirmation(message.content)
      if (hasSameAuditRecordId(confirmation?.audit_record_id, auditRecordId)) return { message, confirmation }
    }
    return null
  }
  const getAuditConfirmation = (message) => {
    const payload = parseAuditConfirmation(message?.content) || {}
    const localStatus = auditDecisionStatuses.value.get(payload.audit_record_id)
    const terminalStatuses = new Set(['rejected', 'cancelled', 'expired', 'succeeded', 'failed', 'execution_unknown'])
    if (terminalStatuses.has(payload.status)) return payload
    if (localStatus) return { ...payload, status: localStatus }
    return payload
  }
  const isHighRiskAuditConfirmation = message => getAuditConfirmation(message).confirmation_mode === 'high_risk_override'
  const getAuditRemainingSeconds = (message) => {
    const expiresAt = Date.parse(getAuditConfirmation(message).expires_at || '')
    if (!Number.isFinite(expiresAt)) return null
    return Math.max(0, Math.ceil((expiresAt - auditCountdownNow.value) / 1000))
  }
  const formatAuditCountdown = (remainingSeconds) => {
    const days = Math.floor(remainingSeconds / 86400)
    const hours = Math.floor((remainingSeconds % 86400) / 3600)
    const minutes = Math.floor((remainingSeconds % 3600) / 60)
    const seconds = remainingSeconds % 60
    if (days > 0) return t('chat.audit_countdown_days', { days, hours, minutes, seconds })
    if (hours > 0) return t('chat.audit_countdown_hours', { hours, minutes, seconds })
    if (minutes > 0) return t('chat.audit_countdown_minutes', { minutes, seconds })
    return t('chat.audit_countdown_seconds', { seconds })
  }
  const isAuditConfirmationExpired = message => getAuditRemainingSeconds(message) === 0
  const getAuditExpiryLabel = (message) => {
    const confirmation = getAuditConfirmation(message)
    const status = confirmation.status || 'pending'
    if (status === 'expired') return t('chat.audit_status_expired')
    if (status !== 'pending') return ''
    const remainingSeconds = getAuditRemainingSeconds(message)
    if (remainingSeconds === null) return '-'
    if (remainingSeconds === 0) return t('chat.audit_status_expired')
    return t('chat.audit_expires_in', { time: formatAuditCountdown(remainingSeconds) })
  }
  const isAuditDecisionPending = message => pendingAuditDecisions.value.has(getAuditConfirmation(message).audit_record_id)
  const getAuditStatusLabel = message => {
    const status = getAuditConfirmation(message).status || 'pending'
    return t(`chat.audit_status_${status === 'pending' && isAuditConfirmationExpired(message) ? 'expired' : status}`)
  }
  const submitAuditDecision = async (message, decision) => {
    const auditRecordId = getAuditConfirmation(message).audit_record_id
    if (!auditRecordId || pendingAuditDecisions.value.has(auditRecordId)) return

    if (decision === 'ignore') {
      pendingAuditDecisions.value = new Set([...pendingAuditDecisions.value, auditRecordId])
      try {
        await ElMessageBox.confirm(
          t('chat.audit_high_risk_warning'),
          t('chat.audit_high_risk_warning_title'),
          {
            type: 'error',
            confirmButtonText: t('chat.audit_ignore_and_allow'),
            cancelButtonText: t('common.cancel')
          }
        )
      } catch {
        pendingAuditDecisions.value = new Set([...pendingAuditDecisions.value].filter(id => id !== auditRecordId))
        return
      }

      const latestAuditConfirmation = findLatestAuditConfirmation(auditRecordId)
      const now = Date.now()
      if (
        props.currentSessionReadOnly ||
        !latestAuditConfirmation ||
        !hasSameAuditRecordId(latestAuditConfirmation.confirmation.audit_record_id, auditRecordId) ||
        !isAuditConfirmationActionable(latestAuditConfirmation.confirmation, now)
      ) {
        pendingAuditDecisions.value = new Set([...pendingAuditDecisions.value].filter(id => id !== auditRecordId))
        return
      }
    }

    pendingAuditDecisions.value = new Set([...pendingAuditDecisions.value, auditRecordId])
    auditDecisionStatuses.value = new Map([
      ...auditDecisionStatuses.value,
      [auditRecordId, ['approve', 'ignore'].includes(decision) ? 'executing' : 'rejected']
    ])
    emit('audit-decision', { auditRecordId, decision })
  }

  return {
    getAuditConfirmation,
    isHighRiskAuditConfirmation,
    getAuditStatusLabel,
    getAuditExpiryLabel,
    isAuditDecisionPending,
    isAuditConfirmationExpired,
    submitAuditDecision
  }
}
