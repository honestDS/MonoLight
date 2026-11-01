import assert from 'node:assert/strict'
import test from 'node:test'
import { createI18n } from 'vue-i18n'
import { renderToString } from 'vue/server-renderer'
import enCommon from '../src/i18n/locales/en/common.js'
import zhCommon from '../src/i18n/locales/zh/common.js'
import { loadUseConfirmDialog } from './confirmDialogHarness.js'

const toHostObject = value => Object.fromEntries(Object.entries(value))

const createHarness = ({ locale = 'zh', actions = ['confirm'] } = {}) => {
  const i18n = createI18n({
    legacy: false,
    locale,
    fallbackLocale: 'en',
    messages: {
      zh: { common: zhCommon },
      en: { common: enCommon }
    }
  })
  const calls = []
  const pendingActions = [...actions]
  const ElMessageBox = {
    confirm(message, title, options) {
      calls.push({
        message,
        title,
        options: toHostObject(options)
      })
      const action = pendingActions.length ? pendingActions.shift() : 'confirm'
      return action === 'confirm' ? Promise.resolve() : Promise.reject(action)
    }
  }
  const useConfirmDialog = loadUseConfirmDialog({
    t: (...args) => i18n.global.t(
      ...args.map(value => (
        value && typeof value === 'object' && !Array.isArray(value)
          ? toHostObject(value)
          : value
      ))
    ),
    ElMessageBox
  })

  return { confirm: useConfirmDialog().confirm, calls }
}

const renderCall = call => renderToString(call.message)

test('renders a plain explanation without empty sections or lists', async () => {
  const harness = createHarness()

  assert.equal(await harness.confirm({ message: 'Delete this profile' }), true)

  const html = await renderCall(harness.calls[0])
  assert.match(html, /confirm-dialog-content__message[^>]*>Delete this profile<\/p>/)
  assert.doesNotMatch(html, /<section\b/)
  assert.doesNotMatch(html, /<(?:ul|li)\b/)
})

test('uses real localized defaults and keeps safe warning dialog options', async () => {
  for (const [locale, common] of [['zh', zhCommon], ['en', enCommon]]) {
    const harness = createHarness({ locale })

    assert.equal(await harness.confirm({ message: 'Continue?' }), true)

    const { title, options } = harness.calls[0]
    assert.equal(title, common.warning, locale)
    assert.equal(options.confirmButtonText, common.confirm, locale)
    assert.equal(options.cancelButtonText, common.cancel, locale)
    assert.equal(options.type, 'warning', locale)
    assert.equal(options.showCancelButton, true, locale)
    assert.equal(options.dangerouslyUseHTMLString, false, locale)
    assert.equal(options.customClass, 'app-confirm-dialog', locale)
    assert.equal(options.closeOnClickModal, true, locale)
    for (const key of ['confirmButtonType', 'confirmButtonClass', 'cancelButtonClass', 'width']) {
      assert.equal(key in options, false, `${locale}: unexpected ${key}`)
    }
  }
})

test('renders grouped content with escaped values and no empty optional details', async () => {
  const longName = `managed-${'x'.repeat(2048)}`
  const harness = createHarness()

  assert.equal(await harness.confirm({
    sections: [
      {
        summary: '<script>alert(1)</script>',
        items: ['<img src=x>', longName],
        more: '<b>Additional details</b>'
      },
      { summary: 'Summary without items or more' }
    ]
  }), true)

  const html = await renderCall(harness.calls[0])
  assert.doesNotMatch(html, /confirm-dialog-content__message/)
  const sections = [...html.matchAll(/<section\b[^>]*>[\s\S]*?<\/section>/g)].map(
    match => match[0]
  )
  assert.equal(sections.length, 2)

  const [fullSection, summaryOnlySection] = sections
  const orderedLines = [
    '<p class="confirm-dialog-content__summary">&lt;script&gt;alert(1)&lt;/script&gt;</p>',
    '<div class="confirm-dialog-content__items">',
    '<div>&lt;img src=x&gt;</div>',
    `<div>${longName}</div>`,
    '<p class="confirm-dialog-content__more">&lt;b&gt;Additional details&lt;/b&gt;</p>'
  ]
  let offset = 0
  for (const line of orderedLines) {
    const lineOffset = fullSection.indexOf(line, offset)
    assert.notEqual(lineOffset, -1, `missing ordered content: ${line.slice(0, 80)}`)
    offset = lineOffset + line.length
  }
  assert.match(
    summaryOnlySection,
    /<p class="confirm-dialog-content__summary">Summary without items or more<\/p>/
  )
  assert.doesNotMatch(summaryOnlySection, /confirm-dialog-content__items/)
  assert.doesNotMatch(html, /<(?:ul|li)\b/)
  assert.doesNotMatch(html, /<script\b/i)
  assert.doesNotMatch(html, /<img\b/i)
})

test('returns action results, rethrows other reasons, and isolates consecutive calls', async () => {
  for (const [action, expected] of [['confirm', true], ['cancel', false], ['close', false]]) {
    const harness = createHarness({ actions: [action] })
    assert.equal(await harness.confirm(), expected, action)
  }

  for (const reason of [new Error('unexpected failure'), 'unexpected action', null]) {
    const harness = createHarness({ actions: [reason] })
    await assert.rejects(harness.confirm(), rejected => {
      assert.strictEqual(rejected, reason)
      return true
    })
  }

  const harness = createHarness()
  await harness.confirm({
    message: 'First body',
    sections: [{ summary: 'First section', items: ['First item'] }]
  })
  await harness.confirm({
    message: 'Second body',
    title: 'Custom title',
    confirmButtonText: 'Proceed',
    cancelButtonText: 'Back',
    type: 'info',
    closeOnClickModal: false
  })

  const firstHtml = await renderCall(harness.calls[0])
  const secondHtml = await renderCall(harness.calls[1])
  assert.match(firstHtml, /First body/)
  assert.match(firstHtml, /First section/)
  assert.doesNotMatch(firstHtml, /Second body|Custom title/)
  assert.match(secondHtml, /Second body/)
  assert.doesNotMatch(secondHtml, /First body|First section|First item|<section\b|<ul\b/)
  assert.equal(harness.calls[1].title, 'Custom title')
  assert.equal(harness.calls[1].options.confirmButtonText, 'Proceed')
  assert.equal(harness.calls[1].options.cancelButtonText, 'Back')
  assert.equal(harness.calls[1].options.type, 'info')
  assert.equal(harness.calls[1].options.closeOnClickModal, false)
})
