const path = require('node:path')
const { loadEnvironment, resolveBackendTarget } = require('./devServerProxy.cjs')

loadEnvironment(path.resolve(__dirname, '../.env'))

module.exports = {
  productionSourceMap: false,
  devServer: {
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
