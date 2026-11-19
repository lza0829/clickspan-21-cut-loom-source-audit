<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const edges = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await api.proofs()
    edges.value = await api.idleCutEdges()
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>会话核对</h1>
    <p class="muted">会话击点集合与起止须与织机汇总可核。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>

    <h2 style="margin-top:1.4rem">切窗核对（空闲超时切分边）</h2>
    <p class="muted">每行即一次持久切分：旧会话先关闭、新会话承接触发击点，与收流装载后写入。</p>
    <p v-if="!edges.length" class="muted">暂无切分边。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>访客</th><th>旧会话</th><th>新会话</th><th>间隙秒</th><th>超时秒</th><th>触发键</th><th>触发时刻</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in edges" :key="e.id">
          <td>{{ e.visitor_id }}</td>
          <td class="mono">{{ e.old_session_id }}</td>
          <td class="mono">{{ e.new_session_id }}</td>
          <td class="mono cut-yes">{{ e.gap_sec }}</td>
          <td class="mono">{{ e.idle_timeout_sec }}</td>
          <td class="mono">{{ e.trigger_hit_id }}</td>
          <td class="mono">{{ e.trigger_ts }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
