<template>
  <el-select
    :model-value="modelValue"
    filterable
    remote
    clearable
    placeholder="输入6位代码或中文全称，如 300750 / 宁德时代"
    :remote-method="doSearch"
    :loading="loading"
    style="width: 100%"
    @update:model-value="(v) => $emit('update:modelValue', v)"
    @change="(v) => $emit('resolved', options.find((o) => o.value === v) || null)"
  >
    <el-option
      v-for="opt in options"
      :key="opt.value"
      :label="opt.label"
      :value="opt.value"
    />
    <template #empty>
      <div v-if="lastError" class="search-error">{{ lastError }}</div>
      <div v-else>输入以搜索股票</div>
    </template>
  </el-select>
</template>

<script setup>
import { ref } from 'vue'
import { stocksApi } from '../api/stocks'

defineProps({
  modelValue: { type: String, default: '' },
})
defineEmits(['update:modelValue', 'resolved'])

const loading = ref(false)
const options = ref([])
const lastError = ref('')

async function doSearch(query) {
  if (!query) {
    options.value = []
    lastError.value = ''
    return
  }
  loading.value = true
  lastError.value = ''
  try {
    // Exact resolution endpoint: code or full name → single hit.
    const { data } = await stocksApi.search(query.trim())
    options.value = data.code ? [{ label: data.label, value: data.code }] : []
  } catch (err) {
    options.value = []
    lastError.value = err.response?.data?.detail || '搜索失败，请检查网络'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.search-error {
  padding: 4px 12px;
  color: #ef4444;
  font-size: 0.85rem;
}
</style>
