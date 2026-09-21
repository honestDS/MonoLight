export const resolveHistoryRequest = (currentPage, pageCount, pageSize) => {
  const logicalPage = Math.max(1, Math.trunc(currentPage))
  const logicalPageCount = Math.max(1, Math.trunc(pageCount))
  const logicalPageSize = Math.max(1, Math.trunc(pageSize))
  const size = logicalPageSize * logicalPageCount
  const offset = (logicalPage - 1) * logicalPageSize

  return {
    page: Math.floor(offset / size) + 1,
    size,
    nextPage: logicalPage + logicalPageCount
  }
}
