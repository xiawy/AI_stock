<template>
  <div ref="chartEl" class="kline-chart" :style="{ height: height + 'px' }"></div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts'
import { stocksApi } from '../api/stocks'

const props = defineProps({
  code: { type: String, required: true },
  days: { type: Number, default: 120 },
  height: { type: Number, default: 380 },
})

const chartEl = ref()
let chart = null

async function render() {
  if (!props.code) return
  let items = []
  let name = ''
  try {
    const { data } = await stocksApi.kline(props.code, props.days)
    items = data.items || []
    name = data.name ? ` ${data.name}` : ''
  } catch {
    if (chart) chart.clear()
    return
  }
  if (!chart) chart = echarts.init(chartEl.value, 'dark')

  const up = '#ef4444' // A-share convention: red = up
  const down = '#22c55e'

  chart.setOption(
    {
      backgroundColor: 'transparent',
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: '#161616',
        borderColor: '#2a2a2a',
        textStyle: { color: '#f5f1eb', fontSize: 12 },
      },
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
      },
      grid: [
        { left: 56, right: 20, top: 30, height: '58%' },
        { left: 56, right: 20, top: '74%', height: '14%' },
      ],
      xAxis: [
        {
          type: 'category',
          data: items.map((d) => d.date),
          boundaryGap: true,
          axisLine: { lineStyle: { color: '#2a2a2a' } },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: items.map((d) => d.date),
          axisLabel: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          splitLine: { lineStyle: { color: '#1c1c1c' } },
        },
        {
          gridIndex: 1,
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        {
          type: 'inside',
          xAxisIndex: [0, 1],
          start: 60,
          end: 100,
        },
        {
          type: 'slider',
          xAxisIndex: [0, 1],
          bottom: 4,
          height: 16,
          borderColor: '#2a2a2a',
        },
      ],
      series: [
        {
          name: props.code + name,
          type: 'candlestick',
          data: items.map((d) => [d.open, d.close, d.low, d.high]),
          itemStyle: {
            color: up,
            color0: down,
            borderColor: up,
            borderColor0: down,
          },
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: items.map((d) => ({
            value: d.volume,
            itemStyle: { color: d.close >= d.open ? up : down, opacity: 0.7 },
          })),
        },
      ],
    },
    true,
  )
}

function onResize() {
  chart && chart.resize()
}

onMounted(() => {
  render()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  chart && chart.dispose()
  chart = null
})

watch(() => [props.code, props.days], render)
</script>

<style scoped>
.kline-chart {
  width: 100%;
}
</style>
