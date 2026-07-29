export function createHistoryMergeTracker() {
  let latestRequestId = 0

  return {
    begin() {
      latestRequestId += 1
      return latestRequestId
    },
    invalidate() {
      latestRequestId += 1
    },
    isLatest(requestId) {
      return requestId === latestRequestId
    }
  }
}
