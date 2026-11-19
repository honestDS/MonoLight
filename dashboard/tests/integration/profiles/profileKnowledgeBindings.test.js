import assert from 'node:assert/strict'
import test from 'node:test'
import {
  assertFormScenario,
  assertOmitsKnowledgeBaseIds,
  createHarness,
  flush,
  normalize,
  prepareForm,
} from './profilePermissionsHarness.js'

test('does not request knowledge bases when the current user is unknown', async t => {
  const harness = await createHarness(t)

  await harness.module.fetchKnowledgeBases()
  await flush()

  assert.equal(harness.api.knowledgeBaseList.length, 0)
  assert.equal(harness.module.knowledgeBasesReady.value, false)
  assert.deepEqual(normalize(harness.module.knowledgeBases.value), [])
})

test('ordinary create and edit submissions fetch and preserve own bindings', async t => {
  const knowledgeBases = [
    {
      id: 'kb-user-a',
      uid: 'user-a',
      name: 'Own user knowledge base',
      description: 'Own description',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-user-b',
      uid: 'user-b',
      name: 'Other user knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-managed-a',
      uid: 'user-a',
      name: 'Managed knowledge base',
      description: '',
      knowledge_base_type: 'llm_managed',
    },
  ]
  const cases = [
    { type: 'create', id: null, name: 'ordinary create' },
    { type: 'edit', id: 41, name: 'ordinary edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: false,
      currentUid: 'user-a',
      knowledgeBases,
    })
    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-user-a'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')

    await harness.module.fetchKnowledgeBases()
    await flush()
    const optionsAfterFirstFetch = normalize(harness.module.knowledgeBaseOptions.value)
    const idsAfterFirstFetch = normalize(harness.module.form.knowledge_base_ids)
    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.deepEqual(normalize(harness.api.knowledgeBaseList), [
      { page: 1, size: 1000 },
      { page: 1, size: 1000 },
    ], scenario.type)
    assert.equal(harness.module.knowledgeBasesReady.value, true, scenario.type)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, false, scenario.type)
    assert.deepEqual(optionsAfterFirstFetch, [{
      value: 'kb-user-a',
      label: 'Own user knowledge base',
      description: 'Own description',
    }], scenario.type)
    assert.deepEqual(normalize(harness.module.knowledgeBaseOptions.value), optionsAfterFirstFetch, scenario.type)
    assert.deepEqual(normalize(harness.module.form.knowledge_base_ids), idsAfterFirstFetch, scenario.type)
    assert.deepEqual(idsAfterFirstFetch, ['kb-user-a'], scenario.type)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assert.deepEqual(payload.knowledge_base_ids, ['kb-user-a'], scenario.type)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-user-a'], scenario.type)
  }
})

test('ordinary create dialog uses the current user and own knowledge base options', async t => {
  const harness = await createHarness(t, {
    superuser: false,
    currentUid: 'user-a',
    knowledgeBases: [
      {
        id: 'kb-user-a',
        uid: 'user-a',
        name: 'Own user knowledge base',
        description: 'Own description',
        knowledge_base_type: 'user',
      },
      {
        id: 'kb-user-b',
        uid: 'user-b',
        name: 'Other user knowledge base',
        description: '',
        knowledge_base_type: 'user',
      },
      {
        id: 'kb-managed-a',
        uid: 'user-a',
        name: 'Managed knowledge base',
        description: '',
        knowledge_base_type: 'llm_managed',
      },
    ],
  })

  harness.module.showDialog('create')
  await flush()
  assert.equal(harness.module.form.uid, 'user-a')
  assert.deepEqual(normalize(harness.api.memorySettings), [{ uid: 'user-a' }])

  await harness.module.fetchKnowledgeBases()
  await flush()

  assert.deepEqual(normalize(harness.module.knowledgeBaseOptions.value), [{
    value: 'kb-user-a',
    label: 'Own user knowledge base',
    description: 'Own description',
  }])
})

test('superuser fetches owner knowledge bases and submits selected ids', async t => {
  const knowledgeBases = [
    {
      id: 'kb-user-a-1',
      uid: 'user-a',
      name: 'First knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
    {
      id: 'kb-user-a-2',
      uid: 'user-a',
      name: 'Second knowledge base',
      description: '',
      knowledge_base_type: 'user',
    },
  ]
  const cases = [
    { type: 'create', id: null, name: 'superuser create' },
    { type: 'edit', id: 52, name: 'superuser edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: true,
      knowledgeBases,
    })
    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.deepEqual(normalize(harness.api.knowledgeBaseList), [{ page: 1, size: 1000 }], scenario.type)
    assert.equal(harness.module.knowledgeBasesReady.value, true, scenario.type)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, false, scenario.type)
    assert.deepEqual(harness.module.knowledgeBases.value.map(item => item.id), [
      'kb-user-a-1',
      'kb-user-a-2',
    ], scenario.type)

    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-user-a-1', 'kb-user-a-2'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')
    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.type)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a')
    else assert.equal(harness.api.updates[0].id, scenario.id)
    assert.deepEqual(payload.knowledge_base_ids, ['kb-user-a-1', 'kb-user-a-2'], scenario.type)
  }
})

test('fetch failure keeps cached ids but omits bindings from the save payload', async t => {
  const cachedKnowledgeBases = [{
    id: 'kb-preserved',
    uid: 'user-a',
    name: 'Preserved knowledge base',
    description: '',
    knowledge_base_type: 'user',
  }]
  const cases = [
    { role: 'ordinary', superuser: false, type: 'create', id: null, name: 'ordinary failed create' },
    { role: 'ordinary', superuser: false, type: 'edit', id: 63, name: 'ordinary failed edit' },
    { role: 'superuser', superuser: true, type: 'create', id: null, name: 'superuser failed create' },
    { role: 'superuser', superuser: true, type: 'edit', id: 63, name: 'superuser failed edit' },
  ]

  for (const scenario of cases) {
    const harness = await createHarness(t, {
      superuser: scenario.superuser,
      knowledgeBases: cachedKnowledgeBases,
      knowledgeBaseFailure: true,
    })
    harness.module.knowledgeBases.value = cachedKnowledgeBases.map(item => ({ ...item }))
    harness.module.knowledgeBasesReady.value = true
    prepareForm(harness.module, {
      ...scenario,
      ownerUid: 'user-a',
      ids: ['kb-preserved'],
    })
    await flush()
    assertFormScenario(harness.module, scenario, 'user-a')

    await harness.module.fetchKnowledgeBases()
    await flush()

    assert.equal(harness.api.knowledgeBaseList.length, 1, scenario.role)
    assert.equal(harness.module.knowledgeBasesReady.value, false, scenario.role)
    assert.equal(harness.module.knowledgeBasesUnavailable.value, true, scenario.role)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.role)

    await harness.module.submitForm()
    await flush()

    const payload = scenario.type === 'create'
      ? harness.api.creates[0]
      : harness.api.updates[0]?.payload
    assert.ok(payload, scenario.role)
    if (scenario.type === 'create') assert.equal(payload.uid, 'user-a', scenario.role)
    else assert.equal(harness.api.updates[0].id, scenario.id, scenario.role)
    assertOmitsKnowledgeBaseIds(payload)
    assert.deepEqual(harness.module.form.knowledge_base_ids, ['kb-preserved'], scenario.role)
  }
})
