// API 客户端：统一 fetch 封装（Bearer 注入 / 401 跳登录 / JSON 序列化）
const BASE = ''

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('token')
  if (token) headers['Authorization'] = `Bearer ${token}`

  const resp = await fetch(BASE + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (resp.status === 401 && location.pathname !== '/login') {
    localStorage.removeItem('token')
    location.href = '/login'
    throw new ApiError(401, '未登录或登录已过期')
  }
  // RBAC 接口守卫：管理接口 403 时提示并退回非管理视图（§5.8）
  if (resp.status === 403 && location.pathname.startsWith('/admin')) {
    alert('无权限访问')
    location.href = '/'
    throw new ApiError(403, '无权限访问')
  }
  if (!resp.ok) {
    let msg = `请求失败 (${resp.status})`
    try {
      const data = await resp.json()
      msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail ?? data)
    } catch { /* ignore */ }
    throw new ApiError(resp.status, msg)
  }
  return resp.json() as Promise<T>
}

export const api = {
  get: <T = any>(path: string) => request<T>('GET', path),
  post: <T = any>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T = any>(path: string, body?: unknown) => request<T>('PUT', path, body),
  patch: <T = any>(path: string, body?: unknown) => request<T>('PATCH', path, body),
  delete: <T = any>(path: string) => request<T>('DELETE', path),
  /** 二进制资源（如论文 PDF）需带鉴权头，返回 Blob 供页面内嵌或下载 */
  getBlob: async (path: string) => {
    const headers: Record<string, string> = {}
    const token = localStorage.getItem('token')
    if (token) headers['Authorization'] = `Bearer ${token}`
    const resp = await fetch(BASE + path, { headers })
    if (resp.status === 401 && location.pathname !== '/login') {
      localStorage.removeItem('token')
      location.href = '/login'
      throw new ApiError(401, '未登录或登录已过期')
    }
    if (!resp.ok) {
      let msg = `请求失败 (${resp.status})`
      try {
        const data = await resp.json()
        msg = typeof data.detail === 'string' ? data.detail : msg
      } catch { /* ignore */ }
      throw new ApiError(resp.status, msg)
    }
    return resp.blob()
  },
}

/** 综述导出 Word（F-REV-05）：提交编辑后大纲 → 以 blob 触发 .docx 下载（文件名取自 Content-Disposition） */
export async function downloadReviewDocx(payload: {
  title: string
  outline: OutlineNode[]
  citations: QaCitation[]
}): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = localStorage.getItem('token')
  if (token) headers['Authorization'] = `Bearer ${token}`
  const resp = await fetch(BASE + '/api/qa/review/export', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  })
  if (!resp.ok) throw new ApiError(resp.status, `导出失败 (${resp.status})`)
  const blob = await resp.blob()
  const cd = resp.headers.get('Content-Disposition') || ''
  const m = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  const filename = m ? decodeURIComponent(m[1]) : '文献综述.docx'
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// ---------- 业务类型 ----------

export interface UnreadItem { subscription_id: number; name: string; unread: number }

export interface Subscription {
  id: number
  name: string
  keywords: string[]
  sources: string[]
  threshold: number
  pub_window: string
  active: boolean
  last_run_at: string | null
  created_at: string | null
  paper_count: number
  unread_count: number
}

export interface Paper {
  id: number
  source: string
  title: string
  authors: string[]
  abstract: string
  journal: string
  published_at: string | null
  url: string | null
  score: number
  is_read: boolean
  journal_level: string
  ccf: string
  cas_zone: string
  arxiv_cat: string
}

export interface FollowLog {
  id: number
  kind: string | null
  direction_name: string | null
  pulled: number
  added: number
  deduped: number
  duration: number | null
  status: string | null
  brief: string
  source_errors: string[]
  started_at: string | null
  finished_at: string | null
}

export interface QaStep {
  key: string
  label: string
  status: string
  detail: string
  thinking?: string                    // 该步骤的模型思考/输出文本（前端打字机+折叠展示）
}

/** 工具调用记录（思考过程面板数据源，v4 §3） */
export interface QaToolCall { tool: string; args: Record<string, unknown>; result_brief: string }

/** 复审意见（Reviewer 固定 JSON，v4 §2.2） */
export interface QaReviewNotes { passed: boolean; defects: string[]; suggestions: string[] }

/** 引用来源（可回溯；paper_id 为库内文献主键，供点击跳内置阅读页） */
export interface QaCitation {
  title: string; source: string; url: string; year: string
  paper_id?: number | null
}

/** 综述大纲节点（可编辑大纲，F-REV-04）：citations 为引用序号（指向 citations[] 下标 +1） */
export interface OutlineNode {
  id: string
  depth: number
  title: string
  content: string
  citations: number[]
}

export interface QaSteps {
  session_id: string
  query: string
  mode: string
  complexity?: 'quick' | 'normal' | 'deep'
  steps: QaStep[]
  finished: boolean
  loop_counter?: number                 // 迭代轮次（第 N/3 轮）
  tool_calls?: QaToolCall[]             // 工具调用记录
  review_notes?: QaReviewNotes | null   // 复审意见
}

export interface QaResult {
  session_id: string
  query: string
  rewritten_query: string
  rewritten: boolean
  complexity?: 'quick' | 'normal' | 'deep'
  points: string[]
  citations: QaCitation[]
  answer: string
  finished: boolean
  error: string | null
  planner_notes?: string[]              // 主 Agent 规划决策记录（v4）
  iteration_count?: number              // 迭代次数（v4）
  sections?: { title: string; content: string }[]
  outline?: OutlineNode[]               // 综述可编辑大纲（F-REV-04）
  direction_name?: string
  paper_count?: number
  subscription_id?: number
}

export const ALL_SOURCES = ['arXiv', 'Semantic Scholar', '万方']
export const FREQ_OPTIONS = [
  { value: 'hourly', label: '每小时' },
  { value: '6h', label: '每 6 小时' },
  { value: 'daily', label: '每日' },
  { value: 'weekly', label: '每周' },
]

// ---------- 管理端类型 ----------

export interface AdminUser {
  id: number
  username: string
  role: 'user' | 'admin'
  status: 'active' | 'disabled'
  created_at: string | null
  last_login_at: string | null
}

export interface ModelItem {
  id: number
  name: string
  model_id: string
  vendor: string
  base_url: string
  capabilities: string[]
  is_default: boolean
  status: 'active' | 'disabled'
  api_key_configured: boolean
  updated_at: string | null
}

export interface AgentBinding {
  agent_key: string
  label: string
  required_capability: string
  model_id: number | null
  model_name: string | null
  updated_at: string | null
}
