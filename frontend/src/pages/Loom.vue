<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const visitors = ref([])
const edges = ref([])
const audits = ref({})
const projections = ref({})
const segments = ref({})
const err = ref('')

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    visitors.value = await api.visitors()
    sessions.value = await api.sessions()
    edges.value = await api.idleCutEdges()
    const audit = await api.loomSourceAudit()
    const byEdge = {}
    for (const row of audit.rows || []) byEdge[row.cut_edge_id] = row
    audits.value = { rows: byEdge, ok: audit.ok, divergences: audit.divergences || [] }

    const perVisitor = {}
    const segs = {}
    for (const v of visitors.value) {
      // 珠串分段投影：一段会话一段珠，跨切边不混段。
      const projection = await api.strands(v.id)
      perVisitor[v.id] = projection
      for (const s of projection.segments || []) segs[s.session_id] = s
    }
    projections.value = perVisitor
    segments.value = segs
  } catch (e) {
    err.value = String(e.message || e)
  }
}

const lanes = computed(() =>
  visitors.value.map((v) => ({
    visitor: v,
    sessions: sessions.value
      .filter((s) => s.visitor_id === v.id)
      .sort((a, b) => a.started_at.localeCompare(b.started_at)),
    projection: projections.value[v.id] || null,
  })),
)

const sourceSession = computed(() => overview.value?.loom_source_session_id || null)
const sourceVisitor = computed(() => overview.value?.loom_source_visitor_id || null)
const auditOk = computed(() => audits.value.ok === true)
const misalignedLanes = computed(() =>
  lanes.value.filter((l) => l.projection && !l.projection.aligned),
)

function edgeBetween(oldSid, newSid) {
  return edges.value.find((e) => e.old_session_id === oldSid && e.new_session_id === newSid)
}

function auditForEdge(edge) {
  return edge ? audits.value.rows?.[edge.id] : null
}

function strandOf(sid) {
  return segments.value[sid] || null
}

onMounted(load)
</script>

<template>
  <!-- plant-ui -->

  <div>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.visitors }}</b><span>访客</span></div>
      <div class="stat"><b>{{ overview.sessions }}</b><span>会话</span></div>
      <div class="stat"><b>{{ overview.hits_primary }}</b><span>正式击点</span></div>
      <div class="stat"><b>{{ overview.idle_cut_edges }}</b><span>切窗边</span></div>
      <div class="stat"><b>{{ overview.loom_source_audit_rows }}</b><span>源审计行</span></div>
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
    </div>

    <section class="panel source-bar" v-if="overview">
      <div>
        <span class="muted">当前串珠源</span>
        <strong class="mono" data-testid="loom-source">{{ sourceSession || '—' }}</strong>
      </div>
      <div class="muted mono">
        访客 {{ sourceVisitor || '—' }}
        <template v-if="overview.loom_source_cut_edge_id">
          · 切边 #{{ overview.loom_source_cut_edge_id }}
        </template>
        · 取自{{ overview.loom_source_from_audit ? '源审计' : '切边兜底' }}
      </div>
      <span class="badge" :class="auditOk ? 'open' : 'closed'" data-testid="audit-verdict">
        {{ auditOk ? '源审计互证通过' : '源审计分叉' }}
      </span>
    </section>

    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="audits.divergences.length" class="err">
      源审计与切边分叉：{{ audits.divergences.map((d) => d.kind).join('、') }}
    </p>
    <p v-for="lane in misalignedLanes" :key="`bad-${lane.visitor.id}`" class="err">
      {{ lane.visitor.label }} 珠串分段与切边不一致，禁止跨切边混段。
    </p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">
        同一访客击点按会话分段成珠：顶栏源取切后承接会话，珠串一段一会话，切边两侧不混段。
      </p>
      <div class="lane-list">
        <article v-for="lane in lanes" :key="lane.visitor.id" class="lane">
          <div class="lane-visitor">
            <strong>{{ lane.visitor.label }}</strong>
            <span class="mono muted">{{ lane.visitor.id }}</span>
            <span class="muted">· {{ lane.sessions.length }} 段会话</span>
            <span v-if="lane.projection" class="mono" :class="lane.projection.aligned ? 'ok' : 'err'">
              {{ lane.projection.aligned ? '分段一致' : '分段分叉' }}
            </span>
          </div>
          <div class="segments">
            <template v-for="(s, i) in lane.sessions" :key="s.id">
              <div v-if="i" class="cut-mark" :title="`切分边 #${edgeBetween(lane.sessions[i-1].id, s.id)?.id ?? ''}`">
                <template v-if="edgeBetween(lane.sessions[i-1].id, s.id)">
                  <span class="scissor" aria-hidden="true">✂</span>
                  <span class="mono">
                    间隙 {{ edgeBetween(lane.sessions[i-1].id, s.id).gap_sec }}s
                    ＞ {{ edgeBetween(lane.sessions[i-1].id, s.id).idle_timeout_sec }}s
                  </span>
                  <span class="mono muted">
                    触发 {{ edgeBetween(lane.sessions[i-1].id, s.id).trigger_hit_id }}
                  </span>
                  <span
                    v-if="auditForEdge(edgeBetween(lane.sessions[i-1].id, s.id))"
                    class="mono"
                    :class="auditForEdge(edgeBetween(lane.sessions[i-1].id, s.id)).aligned ? 'ok' : 'err'"
                  >
                    源审计 {{ auditForEdge(edgeBetween(lane.sessions[i-1].id, s.id)).aligned ? '对齐' : '分叉' }}
                    · 新源 {{ auditForEdge(edgeBetween(lane.sessions[i-1].id, s.id)).new_session_id }}
                  </span>
                </template>
              </div>
              <div class="segment" :class="[s.status, { source: s.id === sourceSession }]">
                <div class="lane-head">
                  <div>
                    <strong>{{ s.id }}</strong>
                    <span class="badge" :class="s.status">{{ s.status === 'closed' ? '已关' : 'open' }}</span>
                    <span v-if="s.id === sourceSession" class="badge source">当前串珠源</span>
                  </div>
                  <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
                </div>
                <div class="beads" v-if="strandOf(s.id)">
                  <template v-for="(h, hi) in strandOf(s.id).hits" :key="h.id">
                    <span v-if="hi" class="connector" aria-hidden="true"></span>
                    <span class="hit-bead">
                      <strong>{{ h.event_name }}</strong>
                      <span>{{ h.page_path }}</span>
                      <span class="mono muted">{{ h.id }}</span>
                    </span>
                  </template>
                  <span v-if="!strandOf(s.id).hits.length" class="muted">本段暂无击点</span>
                </div>
              </div>
            </template>
          </div>
          <div v-if="lane.projection" class="mono muted lane-hash">
            分段成员哈希
            <template v-for="(seg, si) in lane.projection.segments" :key="seg.session_id">
              <span v-if="si"> · </span>{{ seg.session_id.slice(-6) }} {{ seg.members_hash.slice(0, 14) }}…
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
