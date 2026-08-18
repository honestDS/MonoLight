function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function normalizeIdentifier(value) {
  return typeof value === 'string' ? value.toLowerCase() : null
}

function withoutProviderPrefix(value) {
  const separatorIndex = value.indexOf('/')
  return separatorIndex === -1 ? value : value.slice(separatorIndex + 1)
}

function deduplicateById(models) {
  const seenIds = new Set()
  return models.filter(model => {
    if (seenIds.has(model.id)) {
      return false
    }

    seenIds.add(model.id)
    return true
  })
}

/**
 * Finds OpenRouter models using exact identifiers first and provider-agnostic
 * identifiers second.
 */
export function getOpenRouterModelMatches(models, modelId) {
  if (!Array.isArray(models) || typeof modelId !== 'string' || !modelId.trim()) {
    return []
  }

  const normalizedModelId = modelId.trim().toLowerCase()
  const exactMatches = models.filter(model => {
    if (!isObject(model)) {
      return false
    }

    return (
      normalizeIdentifier(model.id) === normalizedModelId ||
      normalizeIdentifier(model.canonical_slug) === normalizedModelId
    )
  })

  if (exactMatches.length > 0) {
    return deduplicateById(exactMatches)
  }

  const providerAgnosticModelId = withoutProviderPrefix(normalizedModelId)
  const providerAgnosticMatches = models.filter(model => {
    if (!isObject(model)) {
      return false
    }

    const id = normalizeIdentifier(model.id)
    const canonicalSlug = normalizeIdentifier(model.canonical_slug)
    return (
      (id !== null && withoutProviderPrefix(id) === providerAgnosticModelId) ||
      (canonicalSlug !== null &&
        withoutProviderPrefix(canonicalSlug) === providerAgnosticModelId)
    )
  })

  return deduplicateById(providerAgnosticMatches)
}

export function toPositiveInteger(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return null
  }

  return Math.floor(value)
}

function getContextLength(model) {
  const topProviderLength = toPositiveInteger(model.top_provider?.context_length)
  if (topProviderLength !== null) {
    return topProviderLength
  }

  return toPositiveInteger(model.context_length)
}

export function applyOpenRouterModelMetadata(entry, model) {
  if (!isObject(entry) || !isObject(model)) {
    return { fields: [], model: entry }
  }

  const fields = []
  const contextLength = getContextLength(model)

  if (contextLength !== null) {
    entry.context_window_k = Math.max(1, Math.floor(contextLength / 1000))
    fields.push('context_window_k')
  }

  const inputModalities = model.architecture?.input_modalities
  if (Array.isArray(inputModalities)) {
    const normalizedInputModalities = inputModalities
      .filter(modality => typeof modality === 'string')
      .map(modality => modality.trim().toLowerCase())

    entry.image_understanding = normalizedInputModalities.includes('image')
    entry.audio_understanding = normalizedInputModalities.includes('audio')
    entry.video_understanding = normalizedInputModalities.includes('video')
    fields.push('image_understanding', 'audio_understanding', 'video_understanding')
  }

  const description = typeof model.description === 'string' ? model.description.trim() : ''
  if ((typeof entry.description !== 'string' || !entry.description.trim()) && description) {
    entry.description = description
    fields.push('description')
  }

  return { fields, model }
}
