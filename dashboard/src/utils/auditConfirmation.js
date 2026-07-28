export const isAuditConfirmationActionable = (confirmation, now = Date.now()) => {
  if (!confirmation || typeof confirmation !== 'object' || Array.isArray(confirmation)) return false
  if (confirmation.status !== 'pending' || !Number.isFinite(now)) return false

  const expiresAt = Date.parse(confirmation.expires_at)
  return Number.isFinite(expiresAt) && expiresAt > now
}
