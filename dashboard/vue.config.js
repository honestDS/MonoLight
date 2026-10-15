const path = require('node:path')
const { loadEnvironment, resolveBackendTarget } = require('./devServerProxy.cjs')

function shouldDisplayRuntimeError(error) {
  return (
    error.message !== 'ResizeObserver loop completed with undelivered notifications.' &&
    error.message !== 'ResizeObserver loop limit exceeded'
  )
}

loadEnvironment(path.resolve(__dirname, '../.env'))

module.exports = {
  productionSourceMap: false,
  devServer: {
    client: {
      overlay: {
        errors: true,
        warnings: true,
        runtimeErrors: shouldDisplayRuntimeError,
      },
    },
    proxy: {
      '/api': {
        target: resolveBackendTarget(),
        changeOrigin: true,
        ws: true,
        xfwd: true,
      },
    },
  },
}
