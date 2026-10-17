import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { knowledgeBaseApi } from '../../api/index.js'

export function useKnowledgeBaseQueryTest({ t, selectedKb, scope }) {
  const queryTestDialogVisible = ref(false)
  const queryTesting = ref(false)
  const queryTested = ref(false)
  const queryTestFormRef = ref(null)
  const queryTestResults = ref([])
  const retrievalMode = ref(null)
  const rerankError = ref(null)
  const queryTestForm = reactive({
    query: '',
    top_k: 5
  })
  const queryTestRules = computed(() => ({
    query: [{ required: true, message: t('knowledgeBase.input_query_err'), trigger: 'blur' }],
    top_k: [{ required: true, message: t('knowledgeBase.set_top_k'), trigger: 'blur' }]
  }))

  let dialogGeneration = 0
  let requestSeq = 0
  let submittingSeq = null

  const resetQueryTestState = () => {
    dialogGeneration += 1
    requestSeq += 1
    submittingSeq = null
    queryTestDialogVisible.value = false
    queryTesting.value = false
    queryTested.value = false
    queryTestForm.query = ''
    queryTestForm.top_k = 5
    queryTestResults.value = []
    retrievalMode.value = null
    rerankError.value = null
    if (queryTestFormRef.value) queryTestFormRef.value.clearValidate()
  }

  const showQueryTestDialog = (row) => {
    if (!scope.capture()()) return

    resetQueryTestState()
    selectedKb.value = row
    queryTestDialogVisible.value = true
  }

  const submitQueryTest = async () => {
    const formRef = queryTestFormRef.value
    const scopeGuard = scope.capture()
    const generation = dialogGeneration
    const selectedId = selectedKb.value?.id
    if (
      !formRef ||
      selectedId === null ||
      selectedId === undefined ||
      !queryTestDialogVisible.value ||
      submittingSeq !== null ||
      queryTesting.value ||
      !scopeGuard()
    ) return

    const currentRequestSeq = ++requestSeq
    const params = { query: queryTestForm.query, top_k: queryTestForm.top_k }
    submittingSeq = currentRequestSeq
    const isCurrent = () => (
      currentRequestSeq === requestSeq &&
      generation === dialogGeneration &&
      submittingSeq === currentRequestSeq &&
      queryTestDialogVisible.value &&
      selectedKb.value?.id === selectedId &&
      scopeGuard()
    )

    try {
      await formRef.validate(async (valid) => {
        if (!valid || !isCurrent()) return

        queryTesting.value = true
        queryTested.value = false
        try {
          const res = await knowledgeBaseApi.queryTest(selectedId, params)
          if (!isCurrent()) return
          const data = res.data.data
          queryTestResults.value = data.items || []
          retrievalMode.value = data.retrieval_mode || null
          rerankError.value = data.rerank_error || null
          queryTested.value = true
        } catch (error) {
          if (isCurrent()) {
            ElMessage.error(t('knowledgeBase.query_test_failed') + error.message)
          }
        }
      })
    } finally {
      if (
        generation === dialogGeneration &&
        currentRequestSeq === requestSeq &&
        submittingSeq === currentRequestSeq
      ) {
        submittingSeq = null
        if (scopeGuard()) queryTesting.value = false
      }
    }
  }

  return {
    queryTestDialogVisible,
    queryTesting,
    queryTested,
    queryTestFormRef,
    queryTestResults,
    retrievalMode,
    rerankError,
    queryTestForm,
    queryTestRules,
    showQueryTestDialog,
    submitQueryTest,
    resetQueryTestState
  }
}
