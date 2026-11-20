async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

/**
 * 核证类只读接口：409 表示「分段/审计分叉须失败」，响应体仍带完整投影，
 * 原样返回由页面显红，不吞成成功。
 */
async function reqAllowDiverged(path) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' } })
  if (res.status === 409) {
    const body = await res.json()
    const detail = body.detail || {}
    return {
      ok: false,
      aligned: false,
      status: 409,
      error: detail.error || '分叉',
      ...(detail.projection || {}),
    }
  }
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  visitors: () => req('/api/visitors'),
  sessions: (visitorId) =>
    req(visitorId ? `/api/sessions?visitor_id=${encodeURIComponent(visitorId)}` : '/api/sessions'),
  session: (id) => req(`/api/sessions/${encodeURIComponent(id)}`),
  quarantine: () => req('/api/quarantine'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (sessionId) =>
    req(sessionId ? `/api/session-proofs?session_id=${encodeURIComponent(sessionId)}` : '/api/session-proofs'),
  idleCutEdges: (visitorId) =>
    req(visitorId ? `/api/idle-cut-edges?visitor_id=${encodeURIComponent(visitorId)}` : '/api/idle-cut-edges'),
  loomSourceAudit: (params = {}) => {
    const qs = new URLSearchParams()
    if (params.visitorId) qs.set('visitor_id', params.visitorId)
    if (params.cutEdgeId != null) qs.set('cut_edge_id', String(params.cutEdgeId))
    const q = qs.toString()
    return req(q ? `/api/loom-source-audit?${q}` : '/api/loom-source-audit')
  },
  strands: (visitorId) =>
    reqAllowDiverged(
      visitorId ? `/api/strands?visitor_id=${encodeURIComponent(visitorId)}` : '/api/strands',
    ),
  settings: () => req('/api/settings'),
}
