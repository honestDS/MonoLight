export const shouldDeferChatContent = ({ wasWelcome, deferActive, sessionId }) =>
  Boolean(sessionId && (wasWelcome || deferActive))

export const shouldExposeChatContent = ({ deferredSessionId, currentSessionId }) =>
  !currentSessionId || deferredSessionId !== currentSessionId

export const shouldReleaseChatContent = ({
  deferredSessionId,
  currentSessionId,
  propertyName
}) => Boolean(
  deferredSessionId
  && deferredSessionId === currentSessionId
  && propertyName === 'transform'
)
