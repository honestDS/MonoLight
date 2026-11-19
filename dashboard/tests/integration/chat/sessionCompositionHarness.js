const selectSession = async (harness, id) => {
  const session = harness.chat.sessions.value.find(item => item.session_id === id)
  await harness.module.handleSelectSession(session)
  await harness.flush()
}

const receiveWs = async (harness, event) => {
  await harness.receiveWs(event)
  await harness.flush()
}

const snapshotMetadata = value => value == null ? value : JSON.parse(JSON.stringify(value))

const createMainMetadata = ({
  responseId = 'main-response',
  turn = 2,
  workSequenceNo = 2,
  eventSequenceNo = 4
} = {}) => ({
  input_tokens: 50000,
  input_tokens_source: 'provider',
  context_window_tokens: 131072,
  max_output_tokens: 8192,
  output_tokens: 180,
  cached_tokens: 200,
  response_id: responseId,
  turn,
  work_sequence_no: workSequenceNo,
  event_sequence_no: eventSequenceNo,
  request_purpose: 'main_dialogue',
  total_input_tokens: 50000,
  total_cached_tokens: 250,
  total_output_tokens: 200,
  cache_hit_rate: 250 / 50000
})

const createContextMetadata = ({
  inputTokens = 123,
  inputTokensSource = 'provider',
  summaryTriggerTokens = 200,
  channelId = 7,
  modelId = 'model-a',
  protocol = 'responses',
  contextSummaryRevision = 2,
  contextContentRevision = 4,
  responseId = 'context-response',
  workSequenceNo = 2,
  eventSequenceNo = 5
} = {}) => ({
  ...createMainMetadata({ responseId, workSequenceNo, eventSequenceNo }),
  input_tokens: inputTokens,
  input_tokens_source: inputTokensSource,
  channel_id: channelId,
  model_id: modelId,
  protocol,
  context_summary_revision: contextSummaryRevision,
  context_content_revision: contextContentRevision,
  summary_trigger_tokens: summaryTriggerTokens
})

const createTaskMetadata = (source, workSequenceNo, totals = null) => ({
  input_tokens: 120,
  input_tokens_source: 'provider',
  context_window_tokens: 8192,
  max_output_tokens: 1024,
  output_tokens: 7,
  cached_tokens: 17,
  response_id: `${source}-response-${workSequenceNo}`,
  turn: 11,
  work_sequence_no: workSequenceNo,
  event_sequence_no: 1,
  request_purpose: 'main_dialogue',
  ...(totals || {})
})

const createProactiveReply = ({
  source,
  sessionId = 'A',
  workSequenceNo,
  eventSequenceNo = 1,
  requestId = `${source}-request-${workSequenceNo}-${eventSequenceNo}`,
  workId = `${source}-work-${workSequenceNo}-${eventSequenceNo}`,
  llmRequestMetadata
}) => ({
  type: 'proactive_reply',
  session_id: sessionId,
  source,
  request_id: requestId,
  request_ids: [requestId],
  work_id: workId,
  work_sequence_no: workSequenceNo,
  event_sequence_no: eventSequenceNo,
  llm_request_metadata: llmRequestMetadata
})

const getSessionMetadata = chat => chat.sessions.value.find(item => item.session_id === 'A').llm_request_metadata

export {
  selectSession,
  receiveWs,
  snapshotMetadata,
  createMainMetadata,
  createContextMetadata,
  createTaskMetadata,
  createProactiveReply,
  getSessionMetadata
}
