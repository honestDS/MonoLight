import { Blob as NodeBlob } from 'node:buffer'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

import { createSetupStatusState } from '../../../src/router/setupGuard.js'
import { truncateErrorMessage } from '../../../src/utils/errorMessage.js'
import { resolveAuthenticatedDownloadUrl } from '../../../src/utils/fileDownloads.js'
import {
  resolveApiBaseUrl,
  resolveWebSocketBaseUrl
} from '../../../src/utils/runtimeEndpoints.js'

const stripImports = source => {
  const keptLines = []
  let inImport = false

  for (const line of source.split('\n')) {
    if (!inImport && /^\s*import\b/.test(line)) {
      inImport = !/\bfrom\s+["'][^"']+["']\s*$/.test(line)
      continue
    }
    if (inImport) {
      if (/\bfrom\s+["'][^"']+["']\s*$/.test(line)) inImport = false
      continue
    }
    keptLines.push(line)
  }

  return keptLines.join('\n')
}

const apiSource = stripImports(readFileSync(
  new URL('../../../src/api/index.js', import.meta.url),
  'utf8'
))
  .replace(/\bexport\s+const\b/g, 'const')
  .replace(/\bexport\s+default\s+request\b/g, '')

const createStorage = entries => {
  const values = new Map(Object.entries(entries))
  const removedKeys = []

  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => {
      removedKeys.push(key)
      values.delete(key)
    },
    removedKeys
  }
}

const createAxiosStub = ({ respond = async () => ({ data: new NodeBlob(['download']) }) } = {}) => {
  const requestInterceptors = []
  const responseInterceptors = []
  const calls = []
  const createInterceptorManager = handlers => ({
    use: (onFulfilled, onRejected) => {
      handlers.push({ onFulfilled, onRejected })
      return handlers.length - 1
    }
  })

  const request = {
    interceptors: {
      request: createInterceptorManager(requestInterceptors),
      response: createInterceptorManager(responseInterceptors)
    },
    get: (url, config = {}) => {
      let chain = Promise.resolve({
        ...config,
        url,
        method: 'get',
        headers: { ...(config.headers || {}) }
      })

      for (const { onFulfilled, onRejected } of requestInterceptors) {
        chain = chain.then(onFulfilled, onRejected)
      }

      chain = chain.then(async requestConfig => {
        calls.push(requestConfig)
        return respond(requestConfig)
      })

      for (const { onFulfilled, onRejected } of responseInterceptors) {
        chain = chain.then(onFulfilled, onRejected)
      }

      return chain
    },
    post: async () => ({ data: null })
  }

  return {
    axios: {
      create: () => request,
      get: async () => ({ data: null })
    },
    calls,
    requestInterceptors,
    responseInterceptors
  }
}

const loadApi = ({ storage, location, axiosStub }) => {
  const context = vm.createContext({
    axios: axiosStub.axios,
    Blob: NodeBlob,
    FormData: class FormDataStub {},
    URL,
    i18n: {
      global: {
        t: key => ({
          'common.network_request_failed': 'network request failed',
          'common.unknown_error': 'unknown error'
        }[key] || key)
      }
    },
    localStorage: storage,
    process: {
      env: {
        VUE_APP_API_BASE_URL: '/api/v1',
        VUE_APP_WS_BASE_URL: ''
      }
    },
    window: { location },
    resolveAuthenticatedDownloadUrl,
    resolveApiBaseUrl,
    resolveWebSocketBaseUrl,
    truncateErrorMessage
  })

  vm.runInContext(
    `${apiSource}\nglobalThis.__module = { fileApi, knowledgeBaseApi, profileApi, setupApi }`,
    context,
    { filename: 'api/index.js' }
  )

  return context.__module
}

const createFixture = ({
  respond,
  token = 'current-token',
  locale = 'zh-CN',
  trackHash = false,
  initialHash = '#/chat'
} = {}) => {
  const storage = createStorage({ token, locale })
  const hashAssignments = []
  let hash = initialHash
  const location = trackHash
    ? {
        origin: 'https://dashboard.example.test',
        get hash() { return hash },
        set hash(value) {
          hashAssignments.push(value)
          hash = value.startsWith('#') ? value : `#${value}`
        }
      }
    : {
        origin: 'https://dashboard.example.test',
        hash: initialHash
      }
  const axiosStub = createAxiosStub({ respond })
  const setupStatusState = createSetupStatusState()
  setupStatusState.setReady(false)
  const api = loadApi({ storage, location, axiosStub })

  return {
    fileApi: api.fileApi,
    knowledgeBaseApi: api.knowledgeBaseApi,
    profileApi: api.profileApi,
    setupApi: api.setupApi,
    storage,
    location,
    axiosStub,
    setupStatusState,
    hashAssignments
  }
}

export { createFixture }
