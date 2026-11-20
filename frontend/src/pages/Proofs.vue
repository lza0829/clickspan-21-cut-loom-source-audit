<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const audit = ref(null)
const strands = ref(null)
const err = ref('')

const auditRows = computed(() => audit.value?.rows || [])
const divergences = computed(() => audit.value?.divergences || [])
const verdict = computed(() => {
  if (!audit.value) return null
  return audit.value.ok === true
})
const roster = computed(() => {
  const seen = new Set()
  const out = []
  for (const r of auditRows.value) {
    if (!seen.has(r.visitor_id)) {
      seen.add(r.visitor_id)
      out.push(r.visitor_id)
    }
  }
  for (const e of audit.value?.cut_edges || []) {
    if (!seen.has(e.visitor_id)) {
      seen.add(e.visitor_id)
      out.push(e.visitor_id)
    }
  }
  return out
})
const activeVisitor = ref('')

async function loadStrands(visitorId) {
  activeVisitor.value = visitorId
  strands.value = await api.strands(visitorId)
}

onMounted(async () => {
  try {
    rows.value = await api.proofs()
    audit.value = await api.loomSourceAudit()
    const first = roster.value[0]
    if (first) await loadStrands(first)
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

    <h2 style="margin-top:1.4rem">织机源审计核证</h2>
    <p class="muted">
      每次切窗在同一事务内落一行源审计：访客、切边键、旧源 / 新源、两侧成员哈希。下表逐行对撞切边与会话成员物化；缺行或分叉即判失败。
    </p>
    <p v-if="audit" class="mono" :class="verdict ? 'ok' : 'err'" data-testid="loom-audit-verdict">
      {{ verdict ? '通过' : '失败' }}
      · 当前串珠源 {{ audit.loom_source_session_id || '—' }}
      · 记录形态 {{ audit.record_form }}
    </p>
    <p v-for="(d, i) in divergences" :key="`div-${i}`" class="err mono">
      分叉 {{ d.kind }}<template v-if="d.cut_edge_id"> · 切边 #{{ d.cut_edge_id }}</template>
      <template v-if="d.detail"> · {{ d.detail }}</template>
    </p>
    <p v-if="!auditRows.length" class="muted">暂无切窗源审计行。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>审计</th><th>切边</th><th>访客</th><th>旧源</th><th>新源</th>
          <th>旧成员哈希</th><th>新成员哈希</th><th>触发键</th><th>对齐</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in auditRows" :key="a.id">
          <td class="mono">#{{ a.id }}</td>
          <td class="mono">#{{ a.cut_edge_id }}</td>
          <td>{{ a.visitor_id }}</td>
          <td class="mono">{{ a.old_session_id }}</td>
          <td class="mono">{{ a.new_session_id }}</td>
          <td class="mono">{{ a.old_members_hash.slice(0, 20) }}… ({{ a.old_member_count }})</td>
          <td class="mono">{{ a.new_members_hash.slice(0, 20) }}… ({{ a.new_member_count }})</td>
          <td class="mono">{{ a.trigger_hit_id }}</td>
          <td class="mono" :class="a.aligned ? 'ok' : 'err'">
            {{ a.aligned ? '对齐' : a.reasons.map((r) => r.kind).join('、') }}
          </td>
        </tr>
      </tbody>
    </table>

    <h2 style="margin-top:1.4rem">珠串分段投影</h2>
    <p class="muted">按会话分段返回：一段会话一段珠，跨切边不混段；分叉时接口 409 而非成功。</p>
    <p v-if="roster.length">
      <button
        v-for="v in roster"
        :key="`picker-${v}`"
        class="btn"
        :class="{ active: v === activeVisitor }"
        style="margin-right:0.4rem"
        @click="loadStrands(v)"
      >
        {{ v }}
      </button>
    </p>
    <p v-if="!strands" class="muted">暂无分段投影。</p>
    <template v-else>
      <p class="mono" :class="strands.aligned ? 'ok' : 'err'">
        {{ strands.aligned ? '分段一致' : '分段分叉' }}
        · 访客 {{ strands.visitor_id }}
        · 成员 {{ strands.members_total }}
      </p>
      <table class="data">
        <thead>
          <tr><th>段</th><th>会话</th><th>状态</th><th>成员</th><th>成员哈希</th><th>核证一致</th></tr>
        </thead>
        <tbody>
          <tr v-for="(s, i) in strands.segments" :key="s.session_id">
            <td class="mono">{{ i + 1 }}</td>
            <td class="mono">{{ s.session_id }}</td>
            <td>{{ s.status }}</td>
            <td class="mono">{{ s.member_ids.join(', ') }}</td>
            <td class="mono">{{ s.members_hash.slice(0, 20) }}…</td>
            <td class="mono" :class="s.proof_aligned ? 'ok' : 'err'">{{ s.proof_aligned ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <h2 style="margin-top:1.4rem">切窗核对（空闲超时切分边）</h2>
    <p class="muted">每行即一次持久切分：旧会话先关闭、新会话承接触发击点，并与源审计同事务写入。</p>
    <p v-if="!audit?.cut_edges?.length" class="muted">暂无切分边。</p>
    <table v-else class="data">
      <thead>
        <tr>
          <th>切边</th><th>访客</th><th>旧会话</th><th>新会话</th><th>间隙秒</th><th>超时秒</th><th>触发键</th><th>触发时刻</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in audit.cut_edges" :key="e.id">
          <td class="mono">#{{ e.id }}</td>
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
