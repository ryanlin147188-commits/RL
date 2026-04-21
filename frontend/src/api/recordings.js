/**
 * 錄製功能 API helpers
 * 使用原生 fetch 直接打 /api/recordings/...，
 * 避免與 client.js 的 baseURL（/api/v1）衝突。
 */

const BASE = '/api/recordings'

async function jsonOrThrow(res) {
  if (!res.ok) {
    const txt = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText} ${txt}`)
  }
  if (res.status === 204) return null
  const ct = res.headers.get('content-type') || ''
  return ct.includes('application/json') ? res.json() : res.text()
}

export const createRecording = (data) =>
  fetch(BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  }).then(jsonOrThrow)

export const getRecording = (id) => fetch(`${BASE}/${id}`).then(jsonOrThrow)

export const getRecordingCommands = (id) =>
  fetch(`${BASE}/${id}/commands`).then(jsonOrThrow)

export const deleteRecording = (id) =>
  fetch(`${BASE}/${id}`, { method: 'DELETE' }).then(jsonOrThrow)

export const uploadRecording = (id, { script, trace }) => {
  const fd = new FormData()
  if (script) fd.append('script', script, script.name || 'recorded.py')
  if (trace) fd.append('trace', trace, trace.name || 'trace.zip')
  return fetch(`${BASE}/${id}/upload`, { method: 'POST', body: fd }).then(jsonOrThrow)
}

export const convertRecording = (id) =>
  fetch(`${BASE}/${id}/convert`, { method: 'POST' }).then(jsonOrThrow)

export const traceDownloadUrl = (id) => `${BASE}/${id}/trace`
