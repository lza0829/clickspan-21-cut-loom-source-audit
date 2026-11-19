<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const visitors = ref([])
const details = ref({})
const edges = ref([])
const err = ref('')

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    visitors.value = await api.visitors()
    sessions.value = await api.sessions()
    edges.value = await api.idleCutEdges()
    const map = {}
    for (const s of sessions.value) {
      map[s.id] = await api.session(s.id)
    }
    details.value = map
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
  })),
)

function edgeBetween(oldSid, newSid) {
  return edges.value.find((e) => e.old_session_id === oldSid && e.new_session_id === newSid)
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
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">
        同一访客击点按时间串成一段珠；切窗边仅作参考，顶栏源会话取该访客最早 open/closed。
      </p>
      <div class="lane-list">
        <article v-for="lane in lanes" :key="lane.visitor.id" class="lane">
          <div class="lane-visitor">
            <strong>{{ lane.visitor.label }}</strong>
            <span class="mono muted">{{ lane.visitor.id }}</span>
            <span class="muted">· {{ lane.sessions.length }} 段会话</span>
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
                </template>
              </div>
              <div class="segment" :class="s.status">
                <div class="lane-head">
                  <div>
                    <strong>{{ s.id }}</strong>
                    <span class="badge" :class="s.status">{{ s.status === 'closed' ? '已关' : 'open' }}</span>
                  </div>
                  <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
                </div>
                <div class="beads" v-if="details[s.id]">
                  <template v-for="(h, hi) in details[s.id].hits" :key="h.id">
                    <span v-if="hi" class="connector" aria-hidden="true"></span>
                    <span class="hit-bead">
                      <strong>{{ h.event_name }}</strong>
                      <span>{{ h.page_path }}</span>
                    </span>
                  </template>
                </div>
              </div>
            </template>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
