export const MAX_ERROR_MESSAGE_LENGTH = 500

export const truncateErrorMessage = (message, maxLength = MAX_ERROR_MESSAGE_LENGTH) => {
  const text = message === null || message === undefined ? '' : String(message)
  const numericMaxLength = Number(maxLength)
  const limit = Number.isFinite(numericMaxLength)
    ? Math.max(0, Math.floor(numericMaxLength))
    : MAX_ERROR_MESSAGE_LENGTH

  if (text.length <= limit) return text
  if (limit > 3) return `${text.slice(0, limit - 3)}...`
  return text.slice(0, limit)
}
