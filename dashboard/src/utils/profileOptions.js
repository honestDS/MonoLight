export const filterProfilesByUid = (profiles, uid) => {
  if (!Array.isArray(profiles) || !uid) return []
  return profiles.filter(profile => profile?.uid === uid)
}

export const filterUserKnowledgeBasesForOwner = (knowledgeBases, uid) => {
  if (!Array.isArray(knowledgeBases) || !uid) return []
  return knowledgeBases.filter(item => (
    item?.uid === uid && item?.knowledge_base_type === 'user'
  ))
}

export const filterKnowledgeBaseIdsForOwner = (ids, knowledgeBases, uid) => {
  if (!Array.isArray(ids) || !Array.isArray(knowledgeBases) || !uid) return []
  const availableUserKnowledgeBases = filterUserKnowledgeBasesForOwner(knowledgeBases, uid)
  return ids.filter(id => availableUserKnowledgeBases.some(item => item?.id === id))
}

export const buildKnowledgeBaseBindingPayload = (ids, ready) => {
  if (ready !== true) return undefined
  if (!Array.isArray(ids)) return []

  return [...new Set(ids.filter(id => id !== null && id !== undefined && id !== ''))]
}

export const formatProfileOptionLabel = (profile, defaultSuffix = '') => {
  if (typeof profile?.name !== 'string') return ''
  return profile.is_default === true ? profile.name + defaultSuffix : profile.name
}

export const getNewSessionProfileOverrideId = (sessionId, draftProfileId) => {
  if (sessionId) return null
  return typeof draftProfileId === 'number' && Number.isInteger(draftProfileId) && draftProfileId > 0
    ? draftProfileId
    : null
}

export const resolveSessionProfileDisplayId = (currentSession, draftProfileId) => {
  const isValidProfileId = profileId => typeof profileId === 'number' &&
    Number.isInteger(profileId) &&
    profileId > 0

  if (!currentSession) {
    return isValidProfileId(draftProfileId) ? draftProfileId : null
  }
  if (isValidProfileId(currentSession.profile_override_id)) {
    return currentSession.profile_override_id
  }
  if (!['http', 'ws'].includes(currentSession.source) && isValidProfileId(currentSession.profile_id)) {
    return currentSession.profile_id
  }
  return null
}

export const resolveDefaultProfileLabel = (profiles, defaultSuffix = '', fallback = '') => {
  if (!Array.isArray(profiles)) return fallback

  const defaultProfile = profiles.find(profile => (
    profile?.is_default === true &&
    typeof profile.id === 'number' &&
    Number.isInteger(profile.id) &&
    profile.id > 0 &&
    typeof profile.name === 'string'
  ))
  return defaultProfile ? formatProfileOptionLabel(defaultProfile, defaultSuffix) : fallback
}

export const resolveSessionProfilePlaceholder = (
  profiles,
  isExternalSession,
  defaultSuffix = '',
  fallback = ''
) => isExternalSession
  ? fallback
  : resolveDefaultProfileLabel(profiles, defaultSuffix, fallback)

const getNonEmptyString = value => typeof value === 'string' && value.length > 0 ? value : null

export const resolveProfileOwnerUid = (currentSession, currentUid) => (
  getNonEmptyString(currentSession?.uid) || getNonEmptyString(currentUid)
)
