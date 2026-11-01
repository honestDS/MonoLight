import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'

const confirmDialogContentSource = readFileSync(
  new URL('../src/components/ConfirmDialogContent.vue', import.meta.url),
  'utf8'
)
const { descriptor } = parse(confirmDialogContentSource)
const compiledConfirmDialogContent = compileScript(descriptor, {
  id: 'confirm-dialog-content-test',
  inlineTemplate: true
})
const confirmDialogContentCode = compiledConfirmDialogContent.content
  .replace(
    /import\s*\{([\s\S]*?)\}\s*from\s*['"]vue['"]\s*;?/g,
    (_match, specifiers) => `const {${specifiers.replace(
      /\b([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)\b/g,
      '$1: $2'
    )}} = Vue;\n`
  )
  .replace(/export\s+default\s+/, 'const ConfirmDialogContent = ')
const ConfirmDialogContent = vm.runInNewContext(
  `${confirmDialogContentCode}\nConfirmDialogContent`,
  { Vue },
  { filename: 'ConfirmDialogContent.vue' }
)

const useConfirmDialogSource = readFileSync(
  new URL('../src/composables/useConfirmDialog.js', import.meta.url),
  'utf8'
)

export const loadUseConfirmDialog = ({ t, ElMessageBox }) => {
  const source = useConfirmDialogSource
    .replace(/^[ \t]*import[^\r\n]*(?:\r?\n|$)/gm, '')
    .replace(/^export\s+function\s+/m, 'function ')

  return vm.runInNewContext(
    `${source}\nuseConfirmDialog`,
    {
      h: Vue.h,
      ConfirmDialogContent,
      useI18n: () => ({ t }),
      ElMessageBox
    },
    { filename: 'useConfirmDialog.js' }
  )
}
