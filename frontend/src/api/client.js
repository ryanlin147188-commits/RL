import axios from 'axios'

/**
 * Axios 實例設定
 * baseURL 指向 FastAPI。開發時透過 vite.config.js proxy 轉發，
 * 正式部署需在 .env 設定 VITE_API_BASE_URL。
 */
const http = axios.create({
  // 後端 routers 全部掛在 /api（見 backend/app/main.py）；
  // 若部署環境有版本前綴，請覆寫 VITE_API_BASE_URL=/api/v1
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api',
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

// ── Request 攔截器（保留 Auth Token 擴充空間）────────
http.interceptors.request.use((config) => {
  // const token = localStorage.getItem('access_token')
  // if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// ── Response 攔截器：統一錯誤格式 ────────────────────
http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const detail = err.response?.data?.detail
    const msg = Array.isArray(detail)
      ? detail.map((d) => d.msg).join('; ')
      : (detail ?? err.message ?? '未知錯誤')
    return Promise.reject(new Error(msg))
  },
)

// ══════════════════════════════════════════════════════
// Projects
// ══════════════════════════════════════════════════════
export const getProjects = () => http.get('/projects')
export const createProject = (data) => http.post('/projects', data)
export const getProjectTree = (projectId) => http.get(`/projects/${projectId}/tree`)

// ══════════════════════════════════════════════════════
// Tree Nodes
// ══════════════════════════════════════════════════════
export const createNode = (data) => http.post('/nodes', data)
export const patchNode = (nodeId, data) => http.patch(`/nodes/${nodeId}`, data)
export const deleteNode = (nodeId) => http.delete(`/nodes/${nodeId}`)

// ══════════════════════════════════════════════════════
// Testcases
// ══════════════════════════════════════════════════════
export const getTestcase = (nodeId) => http.get(`/testcases/${nodeId}`)
export const saveTestcase = (nodeId, data) => http.put(`/testcases/${nodeId}`, data)
export const importDdtJson = (nodeId, ddtJson) =>
  http.post(`/testcases/${nodeId}/import-json`, { ddt_json: ddtJson })

// ══════════════════════════════════════════════════════
// Executions
// ══════════════════════════════════════════════════════
/** 觸發執行，後端立即回傳 task_id（Celery 非同步）*/
export const triggerExecution = (data) => http.post('/executions', data)
/** 輪詢執行進度 */
export const getExecutionStatus = (taskId) => http.get(`/executions/${taskId}/status`)

/**
 * 組合即時日誌 WebSocket URL
 * - 開發：透過 Vite proxy → ws://localhost:3000/ws/v1/...
 * - 生產：透過 Nginx 反代 → wss://<host>/ws/v1/...
 * 可由 VITE_WS_BASE_URL 覆寫（例如指定獨立 ws domain）
 */
export const buildExecutionLogsWsUrl = (taskId) => {
  const override = import.meta.env.VITE_WS_BASE_URL
  if (override) return `${override.replace(/\/$/, '')}/executions/${taskId}/logs`
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  // 後端 ws_router 掛在 /ws（見 backend/app/main.py）
  return `${proto}//${window.location.host}/ws/executions/${taskId}/logs`
}

// ══════════════════════════════════════════════════════
// Reports
// ══════════════════════════════════════════════════════
export const getReports = (projectId, page = 1, limit = 10) =>
  http.get('/reports', { params: { project_id: projectId, page, limit } })
export const getReportDetail = (reportId) => http.get(`/reports/${reportId}`)
export const getReportSteps = (reportId) => http.get(`/reports/${reportId}/steps`)

// ══════════════════════════════════════════════════════
// Dashboard
// ══════════════════════════════════════════════════════
export const getDashboardMetrics = (projectId) =>
  http.get('/dashboard/metrics', { params: { projectId } })
export const getDashboardCharts = (projectId) =>
  http.get('/dashboard/charts', { params: { projectId } })

// ══════════════════════════════════════════════════════
// Upload
// ══════════════════════════════════════════════════════
export const uploadScreenshot = (file) => {
  const form = new FormData()
  form.append('file', file)
  return http.post('/upload/screenshot', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}
