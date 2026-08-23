const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL ?? 'http://localhost:8000'

export type Job = {
  id: string
  filename: string
  status: string
  total_rows: number
  processed_rows: number
  successful_rows?: number
  failed_rows?: number
  needs_review_count: number
  cancel_requested?: boolean
  logs: string[]
}

export type ProductResult = Record<string, any>

export type UserProfile = {
  username: string
  email?: string
  role?: string
  created_at?: number
}

export type ComplianceDetail = {
  passed: number
  failed: number
  total: number
  rate: number
}

export type MetricsResponse = {
  total_processed: number
  attribute_accuracy_rate: number
  human_review_count: number
  human_review_rate: number
  lov_compliance_rate: number
  uom_compliance_rate: number
  description_limit_rate: number
  missing_field_rate: number
  evidence_backed_rate: number
  compliance?: {
    lov: ComplianceDetail
    uom: ComplianceDetail
    source: ComplianceDetail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  const token = typeof window !== 'undefined' ? window.localStorage.getItem('unilog.accessToken') : null
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  const data = await response.json().catch(() => null) as { detail?: string } | T | null
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : undefined
    throw new Error(detail ?? `Backend request failed (${response.status})`)
  }
  return data as T
}

export function login(username: string, password: string) {
  return request<{ access_token: string; token_type: string; user?: UserProfile }>('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }),
  })
}

export function signup(username: string, email: string, password: string) {
  return request<{ access_token: string; token_type: string; user?: UserProfile }>('/api/auth/signup', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, email, password }),
  })
}

export function getCurrentUser() {
  return request<UserProfile>('/api/auth/me')
}

export function logout() {
  return request<{ status: string }>('/api/auth/logout', { method: 'POST' })
}

export function getHealth() {
  return request<{ status: string }>('/health')
}

export function uploadCatalog(file: File) {
  const form = new FormData()
  form.append('file', file)
  return request<{ job_id: string; total_rows: number }>('/api/pipeline/upload', { method: 'POST', body: form })
}

export function getJob(jobId: string) {
  return request<Job>(`/api/pipeline/jobs/${jobId}`)
}

export function cancelJob(jobId: string) {
  return request<{ status: string; job_id: string }>(`/api/pipeline/jobs/${jobId}/cancel`, { method: 'POST' })
}

export function getJobs() {
  return request<Job[]>('/api/pipeline/jobs')
}

export function getResults(jobId: string) {
  return request<ProductResult[]>(`/api/pipeline/results/${jobId}`)
}

export function getReviewQueue() {
  return request<Record<string, unknown>[]>('/api/pipeline/review/queue')
}

export function searchProducts(query: string, jobId?: string) {
  const params = new URLSearchParams({ query })
  if (jobId) params.set('job_id', jobId)
  return request<{ job_id: string; product: ProductResult }[]>(`/api/pipeline/search?${params.toString()}`)
}

export function scanSearch(file: File) {
  const form = new FormData()
  form.append('file', file)
  return request<{ detected_code: string; filename: string; matches: { job_id: string; product: ProductResult }[] }>('/api/pipeline/scan-search', {
    method: 'POST',
    body: form,
  })
}

export type AttributeOverride = {
  value: string
  confidence: number
  reason: string
}

export function approveReview(productRowId: string, overrides: Record<number, AttributeOverride>) {
  return request<{ status: string }>('/api/pipeline/review/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      product_row_id: productRowId,
      overrides,
    }),
  })
}

export type EvidenceChunk = {
  text: string
  page_num: number
  source: string
  element_type?: string
  score?: number
  hybrid_score?: number
}

export function getProductEvidence(mfgPartNum: string, query = 'product specifications') {
  return request<EvidenceChunk[]>(`/api/pipeline/evidence/${encodeURIComponent(mfgPartNum)}?query=${encodeURIComponent(query)}`)
}

export function getMetrics() {
  return request<MetricsResponse>('/api/pipeline/metrics')
}

export function getChatAnswer(query: string, chatHistory: { role: string; content: string }[] = []) {
  return request<{ answer: string }>('/api/chatbot/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, chat_history: chatHistory }),
  })
}

export function exportUrl(jobId: string) {
  return `${API_BASE}/api/pipeline/export/${jobId}`
}

export function evidencePdfUrl(mfgPartNum: string) {
  return `${API_BASE}/api/pipeline/evidence/${encodeURIComponent(mfgPartNum)}/pdf`
}
