const isHttpUrl = url => url.protocol === 'http:' || url.protocol === 'https:'

const invalidUrl = () => {
  throw new TypeError()
}

export const resolveAuthenticatedDownloadUrl = (url, apiBaseUrl, pageOrigin) => {
  let target
  let apiBase

  try {
    if (!url || !apiBaseUrl) return invalidUrl()
    target = new URL(url, pageOrigin)
    apiBase = new URL(apiBaseUrl, pageOrigin)
  } catch {
    return invalidUrl()
  }

  if (
    !isHttpUrl(target) ||
    !isHttpUrl(apiBase) ||
    target.origin !== apiBase.origin ||
    target.username ||
    target.password ||
    target.hash ||
    apiBase.username ||
    apiBase.password ||
    apiBase.hash
  ) {
    return invalidUrl()
  }

  const basePath = apiBase.pathname.replace(/\/+$/, '')
  if (target.pathname !== `${basePath}/download` && target.pathname !== `${basePath}/download-sent`) {
    return invalidUrl()
  }

  return target.href
}

export const createFileDownloadCache = ({ fetchBlob, createObjectUrl, revokeObjectUrl }) => {
  const records = new Map()
  let disposed = false

  const get = url => {
    const record = records.get(url)
    return record?.status === 'ready' ? record.objectUrl : ''
  }

  const load = url => {
    if (!url || disposed) return Promise.resolve(null)

    const existing = records.get(url)
    if (existing) return existing.promise

    const record = { status: 'loading', promise: null, objectUrl: null }
    record.promise = (async () => {
      const blob = await fetchBlob(url)
      if (disposed || records.get(url) !== record) return null

      const objectUrl = await createObjectUrl(blob)
      if (disposed || records.get(url) !== record) {
        if (objectUrl) revokeObjectUrl(objectUrl)
        return null
      }

      record.status = 'ready'
      record.objectUrl = objectUrl
      return objectUrl
    })().catch(error => {
      const active = !disposed && records.get(url) === record
      if (active) records.delete(url)
      return active ? Promise.reject(error) : null
    })

    records.set(url, record)
    return record.promise
  }

  const prune = urls => {
    const retained = new Set(urls || [])
    for (const [url, record] of records) {
      if (retained.has(url)) continue
      records.delete(url)
      if (record.status === 'ready' && record.objectUrl) revokeObjectUrl(record.objectUrl)
    }
  }

  const clear = () => {
    const current = [...records.values()]
    records.clear()
    for (const record of current) {
      if (record.status === 'ready' && record.objectUrl) revokeObjectUrl(record.objectUrl)
    }
  }

  const dispose = () => {
    if (!disposed) {
      disposed = true
      clear()
    }
    return Promise.resolve(null)
  }

  return { get, load, prune, clear, dispose }
}
