import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { fileApi } from '../../api/index.js'
import { createFileDownloadCache } from '../../utils/fileDownloads.js'

export function useChatMessageFiles({ props, t, getMessageFiles }) {
  let messageListUnmounted = false
  const fileObjectUrls = ref(new Map())
  const fileDownloadCache = createFileDownloadCache({
    fetchBlob: fileApi.fetchDownloadBlob,
    createObjectUrl: URL.createObjectURL,
    revokeObjectUrl: URL.revokeObjectURL
  })
  const pendingFileDownloads = new Set()
  const reportedImageErrors = new Set()
  let fileDownloadGeneration = 0

  const clearFileDownloadState = () => {
    fileDownloadGeneration += 1
    fileDownloadCache.clear()
    fileObjectUrls.value.clear()
    pendingFileDownloads.clear()
    reportedImageErrors.clear()
  }

  watch(() => props.currentSessionId, () => {
    clearFileDownloadState()
  }, { flush: 'sync' })

  const isImageFile = (path) => ['png', 'jpg', 'jpeg', 'gif', 'webp'].includes((path || '').split('.').pop().toLowerCase())
  const getFilename = (path) => {
    if (!path) return t('chat.unknown_file')
    const name = path.split(/[\/\\]/).pop()
    return name.length > 9 && name[8] === '_' ? name.substring(9) : name
  }
  const isPreviewImage = file => file?.previewable && file?.mime_type?.startsWith('image/')
  const getAttachmentSourceUrl = path => path ? fileApi.getDownloadUrl(path, props.currentSessionId) : ''
  const getSentFileUrl = file => fileApi.resolveDownloadUrl(file?.download_url)
  const getFileObjectUrl = sourceUrl => fileObjectUrls.value.get(sourceUrl) || ''
  const getAttachmentImageUrls = message => (message.attachments || [])
    .filter(isImageFile)
    .map(getAttachmentSourceUrl)
    .map(getFileObjectUrl)
    .filter(Boolean)
  const getSentFileImageUrls = message => getMessageFiles(message)
    .filter(isPreviewImage)
    .map(getSentFileUrl)
    .map(getFileObjectUrl)
    .filter(Boolean)
  const fileSourceUrls = computed(() => {
    const urls = new Set()
    props.messages.forEach(message => {
      const attachments = message.attachments || []
      attachments.forEach(path => urls.add(getAttachmentSourceUrl(path)))
      getMessageFiles(message).forEach(file => urls.add(getSentFileUrl(file)))
    })
    return [...urls].filter(url => url && url !== '#')
  })
  const imageSourceUrls = computed(() => {
    const urls = new Set()
    props.messages.forEach(message => {
      const attachments = message.attachments || []
      attachments.filter(isImageFile).forEach(path => urls.add(getAttachmentSourceUrl(path)))
      getMessageFiles(message).filter(isPreviewImage).forEach(file => urls.add(getSentFileUrl(file)))
    })
    return [...urls].filter(url => url && url !== '#')
  })
  const isActiveFileSource = (sourceUrl, generation) => (
    !messageListUnmounted &&
    generation === fileDownloadGeneration &&
    fileSourceUrls.value.includes(sourceUrl)
  )
  const reportFileLoadError = (error, sourceUrl, generation, isImage = false) => {
    if (!isActiveFileSource(sourceUrl, generation)) return
    if (isImage && reportedImageErrors.has(sourceUrl)) return
    if (isImage) reportedImageErrors.add(sourceUrl)
    ElMessage.error(error?.message || t('common.network_request_failed'))
  }
  const loadFileObjectUrl = async (sourceUrl, generation = fileDownloadGeneration) => {
    if (!isActiveFileSource(sourceUrl, generation)) return ''

    const cachedUrl = fileDownloadCache.get(sourceUrl)
    if (cachedUrl) {
      fileObjectUrls.value.set(sourceUrl, cachedUrl)
      return cachedUrl
    }

    const objectUrl = await fileDownloadCache.load(sourceUrl)
    if (!objectUrl || !isActiveFileSource(sourceUrl, generation)) return ''
    fileObjectUrls.value.set(sourceUrl, objectUrl)
    return objectUrl
  }
  const preloadImage = async (sourceUrl, generation) => {
    try {
      await loadFileObjectUrl(sourceUrl, generation)
    } catch (error) {
      reportFileLoadError(error, sourceUrl, generation, true)
    }
  }
  watch([fileSourceUrls, imageSourceUrls], ([sourceUrls, imageUrls]) => {
    const retainedUrls = new Set(sourceUrls)
    fileDownloadCache.prune(sourceUrls)
    for (const sourceUrl of fileObjectUrls.value.keys()) {
      if (!retainedUrls.has(sourceUrl)) fileObjectUrls.value.delete(sourceUrl)
    }
    for (const sourceUrl of reportedImageErrors) {
      if (!retainedUrls.has(sourceUrl)) reportedImageErrors.delete(sourceUrl)
    }

    const generation = fileDownloadGeneration
    new Set(imageUrls).forEach(sourceUrl => { void preloadImage(sourceUrl, generation) })
  }, { immediate: true, flush: 'sync' })
  const handleFileDownload = async (sourceUrl, filename) => {
    if (!sourceUrl || sourceUrl === '#' || messageListUnmounted) return
    const generation = fileDownloadGeneration
    const downloadKey = `${generation}:${sourceUrl}`
    if (pendingFileDownloads.has(downloadKey)) return
    pendingFileDownloads.add(downloadKey)

    try {
      let objectUrl
      try {
        objectUrl = await loadFileObjectUrl(sourceUrl, generation)
      } catch (error) {
        reportFileLoadError(error, sourceUrl, generation)
        return
      }
      if (!objectUrl || !isActiveFileSource(sourceUrl, generation)) return

      const anchor = document.createElement('a')
      anchor.href = objectUrl
      anchor.download = filename || t('chat.unknown_file')
      anchor.style.display = 'none'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
    } finally {
      pendingFileDownloads.delete(downloadKey)
    }
  }

  onUnmounted(() => {
    messageListUnmounted = true
    fileDownloadGeneration += 1
    fileDownloadCache.dispose()
    fileObjectUrls.value.clear()
    pendingFileDownloads.clear()
    reportedImageErrors.clear()
  })

  return {
    isImageFile,
    getFilename,
    isPreviewImage,
    getAttachmentSourceUrl,
    getSentFileUrl,
    getFileObjectUrl,
    getAttachmentImageUrls,
    getSentFileImageUrls,
    handleFileDownload
  }
}
