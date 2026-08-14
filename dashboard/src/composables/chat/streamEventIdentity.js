const hasIdentity = value => value !== null && value !== undefined && value !== ''

export const getStreamEventIdentity = (event) => {
  if (hasIdentity(event?.event_id)) return String(event.event_id)

  const workId = event?.work_id
  const sequence = event?.event_sequence_no
  if (hasIdentity(workId) && Number.isInteger(sequence) && sequence >= 0) {
    return `work:${String(workId)}:event:${sequence}`
  }

  return null
}
