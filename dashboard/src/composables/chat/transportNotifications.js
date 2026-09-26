const NOTICE_CONFIG = Object.freeze({
  fallback_retrying: Object.freeze({
    key: 'chat.ws_fallback_retrying',
    type: 'warning',
    duration: 4000,
    showClose: false
  }),
  fallback_blocked: Object.freeze({
    key: 'chat.ws_fallback_blocked',
    type: 'warning',
    duration: 5000,
    showClose: true
  }),
  submission_unknown: Object.freeze({
    key: 'chat.ws_submission_unknown',
    type: 'warning',
    duration: 7000,
    showClose: true
  })
})

export const createTransportNotifier = ({ translate, showMessage }) => {
  if (typeof translate !== 'function' || typeof showMessage !== 'function') {
    throw new TypeError('translate and showMessage must be functions')
  }

  let activeMessage = null

  const close = () => {
    activeMessage?.close?.()
    activeMessage = null
  }

  const show = kind => {
    const config = NOTICE_CONFIG[kind]
    if (!config) throw new RangeError(`Unknown transport notice: ${kind}`)

    close()
    activeMessage = showMessage({
      type: config.type,
      message: translate(config.key),
      duration: config.duration,
      showClose: config.showClose
    })
  }

  return { show, close }
}
