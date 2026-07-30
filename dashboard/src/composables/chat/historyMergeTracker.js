export function createHistoryMergeTracker() {
  let generation = 0
  let latestRequestId = 0
  let latestToken = null

  return {
    begin() {
      latestRequestId += 1
      latestToken = Object.freeze({ generation, requestId: latestRequestId })
      return latestToken
    },
    invalidate() {
      generation += 1
      latestToken = null
    },
    isLatest(token) {
      return token === latestToken && token?.generation === generation
    }
  }
}
