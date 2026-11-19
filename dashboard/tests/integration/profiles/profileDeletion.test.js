import assert from 'node:assert/strict'
import test from 'node:test'
import { renderToString } from 'vue/server-renderer'
import enCommon from '../../../src/i18n/locales/en/common.js'
import enProfiles from '../../../src/i18n/locales/en/profiles.js'
import zhCommon from '../../../src/i18n/locales/zh/common.js'
import zhProfiles from '../../../src/i18n/locales/zh/profiles.js'
import {
  createHarness,
  createProfilesTranslate,
  dataResponse,
} from './profilePermissionsHarness.js'

test('profile deletion impact renders every localized group and confirms with its impact token', async t => {
  for (const locale of ['zh', 'en']) {
    const profiles = locale === 'zh' ? zhProfiles : enProfiles
    const common = locale === 'zh' ? zhCommon : enCommon
    const profileName = 'Dangerous <script>alert(1)</script>'
    const sessionName = '<img src=x>'
    const longManagedName = `managed-${'x'.repeat(512)}`
    const impact = {
      requires_confirmation: true,
      impact_token: `impact-token-${locale}`,
      profile: { name: profileName },
      sessions: {
        count: 2,
        message_count: 7,
        items: [
          { title: sessionName, session_id: 'session-with-title' },
          { session_id: 'session-without-title' },
        ],
        omitted_count: 3,
      },
      managed_knowledge_base: {
        count: 1,
        items: [{ name: longManagedName, knowledge_count: 4 }],
      },
      scheduled_tasks: {
        count: 2,
        items: [{ name: 'Nightly backup' }, { name: 'Morning report' }],
      },
      message_platforms: {
        count: 2,
        items: [{ name: 'WeChat' }, { name: 'Slack' }],
      },
      user_knowledge_base_bindings: {
        count: 2,
        items: [{ name: 'Personal docs' }, { name: 'Team docs' }],
      },
    }
    const harness = await createHarness(t, {
      translate: createProfilesTranslate(locale),
      deleteRequest: (id, params) => Promise.resolve(dataResponse(
        params?.confirm_impact ? {} : impact,
      )),
    })

    await harness.module.handleDelete('profile-42')

    assert.equal(harness.confirmations.length, 1, locale)
    const confirmation = harness.confirmations[0]
    const html = await renderToString(confirmation.message)
    const countTags = tag => (html.match(new RegExp(`<${tag}\\b`, 'g')) || []).length
    assert.equal(countTags('section'), 5, locale)
    assert.equal(countTags('ul'), 0, locale)
    assert.equal(countTags('li'), 0, locale)
    const detailBlocks = html.match(
      /<div class="confirm-dialog-content__items">\s*(?:<!--\[-->\s*)?(?:<div>[^<]*<\/div>\s*)+(?:<!--\]-->\s*)?<\/div>/g,
    ) || []
    assert.equal(detailBlocks.length, 4, locale)
    assert.deepEqual(
      detailBlocks.map(block => (block.match(/<div>/g) || []).length),
      [2, 2, 2, 2],
      locale,
    )
    assert.ok(html.includes(profiles.delete_impact_intro.replace('{name}', 'Dangerous &lt;script&gt;alert(1)&lt;/script&gt;')), locale)
    assert.ok(html.includes(profiles.delete_impact_sessions.replace('{count}', '2').replace('{messages}', '7')), locale)
    assert.ok(html.includes(profiles.delete_impact_managed_kb.replace('{name}', longManagedName).replace('{count}', '4')), locale)
    assert.ok(html.includes(profiles.delete_impact_scheduled_tasks.replace('{count}', '2')), locale)
    assert.ok(html.includes(profiles.delete_impact_message_platforms.replace('{count}', '2')), locale)
    assert.ok(html.includes(profiles.delete_impact_user_kbs.replace('{count}', '2')), locale)
    assert.ok(html.includes(profiles.delete_impact_session_item.replace('{name}', '&lt;img src=x&gt;')), locale)
    assert.ok(html.includes(profiles.delete_impact_session_item.replace('{name}', 'session-without-title')), locale)
    for (const name of ['Nightly backup', 'Morning report', 'WeChat', 'Slack', 'Personal docs', 'Team docs']) {
      assert.ok(html.includes(name), `${locale}: ${name}`)
    }
    assert.ok(html.includes(longManagedName), locale)
    assert.match(html, new RegExp(profiles.delete_impact_more.replace('{count}', '3')), locale)
    assert.doesNotMatch(html, /<img\b/i, locale)
    assert.doesNotMatch(html, /<script\b/i, locale)
    assert.doesNotMatch(html, /innerHTML/i, locale)
    assert.doesNotMatch(html, /\b(?:undefined|NaN)\b/, locale)

    assert.equal(confirmation.title, profiles.delete_impact_title)
    assert.equal(confirmation.options.confirmButtonText, profiles.delete_impact_confirm)
    assert.equal(confirmation.options.cancelButtonText, common.cancel)
    assert.equal(confirmation.options.confirmButtonType, undefined)
    assert.equal(confirmation.options.confirmButtonClass, undefined)
    assert.equal(confirmation.options.cancelButtonClass, undefined)
    assert.equal(confirmation.options.customClass, 'app-confirm-dialog')
    assert.equal(confirmation.options.dangerouslyUseHTMLString, false)
    assert.deepEqual(harness.api.deletes, [
      { id: 'profile-42', params: undefined },
      {
        id: 'profile-42',
        params: { confirm_impact: true, impact_token: impact.impact_token },
      },
    ], locale)
    assert.equal(harness.api.profileList.length, 2, locale)
    assert.deepEqual(
      harness.messages.filter(message => message.type === 'success').map(message => message.args[0]),
      [common.delete_success],
      locale,
    )
  }
})

test('unexpected confirmation errors stop profile deletion without submitting impact or refreshing', async t => {
  const impact = {
    requires_confirmation: true,
    impact_token: 'confirmation-error-token',
    profile: { name: 'confirmation error profile' },
    sessions: { count: 1, message_count: 0, items: [] },
  }
  const error = new Error('confirmation dialog failed')
  const harness = await createHarness(t, {
    deleteRequest: (_id, params) => Promise.resolve(dataResponse(
      params?.confirm_impact ? {} : impact,
    )),
    confirmDelete: () => {
      throw error
    },
  })
  const listCountBeforeDelete = harness.api.profileList.length

  await harness.module.handleDelete('profile-confirmation-error')

  assert.equal(harness.confirmations.length, 1)
  assert.deepEqual(harness.api.deletes, [
    { id: 'profile-confirmation-error', params: undefined },
  ])
  assert.equal(harness.api.profileList.length, listCountBeforeDelete)
  assert.deepEqual(harness.messages.filter(message => message.type === 'success'), [])
  assert.deepEqual(
    harness.messages.filter(message => message.type === 'error').map(message => message.args[0]),
    [error.message],
  )
})

test('empty deletion impact groups ask directly in both locales and handle confirmation or cancellation', async t => {
  const profileName = 'zero-impact profile'
  const expectedPrompts = {
    zh: '确定要删除配置“zero-impact profile”吗？',
    en: 'Are you sure you want to delete profile “zero-impact profile”?',
  }

  for (const locale of ['zh', 'en']) {
    for (const action of ['cancel', 'confirm']) {
      const impact = {
        requires_confirmation: true,
        impact_token: `${locale}-${action}-impact-token`,
        profile: { name: profileName },
        sessions: { count: 0, message_count: 0, items: [] },
        managed_knowledge_base: { count: 0, items: [] },
        scheduled_tasks: { count: 0 },
        message_platforms: {},
      }
      const harness = await createHarness(t, {
        translate: createProfilesTranslate(locale),
        deleteRequest: (_id, params) => Promise.resolve(dataResponse(
          params?.confirm_impact ? {} : impact,
        )),
        confirmDelete: () => action === 'confirm' ? true : 'cancel',
      })

      await harness.module.handleDelete(`profile-zero-${locale}-${action}`)

      const html = await renderToString(harness.confirmations[0].message)
      const firstParagraph = html.match(/<p\b[^>]*>([\s\S]*?)<\/p>/)?.[1]
      assert.equal(firstParagraph, expectedPrompts[locale], `${locale} ${action}`)
      assert.equal((html.match(/<section\b/g) || []).length, 0, `${locale} ${action}`)
      assert.doesNotMatch(html, /confirm-dialog-content__items/, `${locale} ${action}`)
      assert.equal((html.match(/<ul\b/g) || []).length, 0, `${locale} ${action}`)
      assert.equal((html.match(/<li\b/g) || []).length, 0, `${locale} ${action}`)
      assert.doesNotMatch(html, /执行以下操作|following actions/i, `${locale} ${action}`)
      assert.doesNotMatch(html, /\b(?:undefined|NaN)\b/, `${locale} ${action}`)

      assert.deepEqual(harness.api.deletes[0], {
        id: `profile-zero-${locale}-${action}`,
        params: undefined,
      }, `${locale} ${action}`)
      if (action === 'cancel') {
        assert.equal(harness.confirmations.length, 1, `${locale} ${action}`)
        assert.deepEqual(harness.api.deletes, [
          { id: `profile-zero-${locale}-${action}`, params: undefined },
        ], `${locale} ${action}`)
        assert.equal(harness.api.profileList.length, 1, `${locale} ${action}`)
        assert.deepEqual(harness.messages.filter(message => message.type === 'success'), [], `${locale} ${action}`)
      } else {
        assert.equal(harness.confirmations.length, 1, `${locale} ${action}`)
        assert.deepEqual(harness.api.deletes, [
          { id: `profile-zero-${locale}-${action}`, params: undefined },
          {
            id: `profile-zero-${locale}-${action}`,
            params: { confirm_impact: true, impact_token: impact.impact_token },
          },
        ], `${locale} ${action}`)
        assert.equal(harness.api.profileList.length, 2, `${locale} ${action}`)
        assert.deepEqual(
          harness.messages.filter(message => message.type === 'success').map(message => message.args[0]),
          [locale === 'zh' ? zhCommon.delete_success : enCommon.delete_success],
          `${locale} ${action}`,
        )
      }
    }
  }
})

test('cancel and close stop profile deletion after the preview request', async t => {
  for (const action of ['cancel', 'close']) {
    const impact = {
      requires_confirmation: true,
      impact_token: `${action}-impact-token`,
      profile: { name: `${action} profile` },
      sessions: { count: 1, message_count: 0, items: [] },
    }
    const harness = await createHarness(t, {
      deleteRequest: () => Promise.resolve(dataResponse(impact)),
      confirmDelete: () => action,
    })
    const listCountBeforeDelete = harness.api.profileList.length

    await harness.module.handleDelete(`profile-${action}`)

    assert.equal(harness.confirmations.length, 1, action)
    assert.deepEqual(harness.api.deletes, [{ id: `profile-${action}`, params: undefined }], action)
    assert.equal(harness.api.profileList.length, listCountBeforeDelete, action)
    assert.deepEqual(harness.messages.filter(message => message.type === 'success'), [], action)
    assert.deepEqual(harness.messages.filter(message => message.type === 'error'), [], action)
  }
})

test('reconfirmation displays fresh impact data and uses each fresh token', async t => {
  const impacts = [
    {
      requires_confirmation: true,
      impact_token: 'first-impact-token',
      profile: { name: 'first preview profile' },
      sessions: {
        count: 1,
        message_count: 2,
        items: [{ title: 'first session', session_id: 'first-session' }],
      },
    },
    {
      requires_confirmation: true,
      impact_token: 'second-impact-token',
      profile: { name: 'second preview profile' },
      scheduled_tasks: {
        count: 1,
        items: [{ name: 'second scheduled task' }],
      },
    },
  ]
  let responseIndex = 0
  const harness = await createHarness(t, {
    translate: createProfilesTranslate('en'),
    deleteRequest: () => Promise.resolve(dataResponse(
      responseIndex < impacts.length ? impacts[responseIndex++] : {},
    )),
  })

  await harness.module.handleDelete('profile-reconfirm')

  assert.equal(harness.confirmations.length, 2)
  const firstHtml = await renderToString(harness.confirmations[0].message)
  const secondHtml = await renderToString(harness.confirmations[1].message)
  assert.match(firstHtml, /first preview profile/)
  assert.match(firstHtml, /Delete 1 linked sessions and their 2 messages/)
  assert.doesNotMatch(firstHtml, /second preview profile/)
  assert.match(secondHtml, /second preview profile/)
  assert.match(secondHtml, /Delete 1 linked scheduled tasks/)
  assert.doesNotMatch(secondHtml, /first preview profile/)
  assert.deepEqual(harness.api.deletes, [
    { id: 'profile-reconfirm', params: undefined },
    {
      id: 'profile-reconfirm',
      params: { confirm_impact: true, impact_token: 'first-impact-token' },
    },
    {
      id: 'profile-reconfirm',
      params: { confirm_impact: true, impact_token: 'second-impact-token' },
    },
  ])
  assert.equal(harness.api.profileList.length, 2)
  assert.deepEqual(
    harness.messages.filter(message => message.type === 'success').map(message => message.args[0]),
    [enCommon.delete_success],
  )
})
