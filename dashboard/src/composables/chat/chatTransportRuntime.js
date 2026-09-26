export const sendHttpNonStream = async ({
  api,
  message,
  sessionId,
  attachments,
  requestId,
  profileOverrideId,
  showToolCalls,
  showReasoning
}) => {
  const payload = {
    message,
    session_id: sessionId || null,
    attachments: attachments || null,
    request_id: requestId
  }
  if (profileOverrideId !== null && profileOverrideId !== undefined) {
    payload.profile_override_id = profileOverrideId
  }
  if (!sessionId && showToolCalls === false) {
    payload.show_tool_calls = false
  }
  if (!sessionId && showReasoning === false) {
    payload.show_reasoning = false
  }

  const response = await api.completions({ ...payload, stream: false })
  return response.data?.data ?? response.data
}
