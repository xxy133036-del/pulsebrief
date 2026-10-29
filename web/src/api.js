const API_ROOT = '/api/v1'

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

export function createApi(token) {
  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {})
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (options.body && !(options.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json')
    }
    const response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      headers,
      body: options.body instanceof FormData ? options.body : options.body,
    })
    if (response.status === 204) return null
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const message = typeof payload.detail === 'string'
        ? payload.detail
        : payload.detail?.message || '请求暂时无法完成，请稍后重试。'
      throw new ApiError(message, response.status)
    }
    return payload
  }

  return {
    request,
    health: () => request('/health'),
    publicDemoStories: (limit = 3) => request(`/public/demo-stories?limit=${limit}`),
    demoLogin: () => request('/auth/demo', { method: 'POST' }),
    sendCode: (email) => request('/auth/send-code', {
      method: 'POST', body: JSON.stringify({ email }),
    }),
    verifyCode: (email, code, display_name) => request('/auth/verify-code', {
      method: 'POST', body: JSON.stringify({ email, code, display_name }),
    }),
    me: () => request('/me'),
    myFeedback: () => request('/me/feedback'),
    adminFeedback: (status = '') => request(`/admin/feedback${status ? `?status=${encodeURIComponent(status)}` : ''}`),
    updateFeedback: (id, body) => request(`/admin/feedback/${encodeURIComponent(id)}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
    subscription: () => request('/subscription'),
    saveSubscription: (body) => request('/subscription', { method: 'PUT', body: JSON.stringify(body) }),
    createTask: (kind = 'preview') => request('/briefing-tasks', {
      method: 'POST',
      body: JSON.stringify({ kind }),
    }),
    task: (id) => request(`/briefing-tasks/${encodeURIComponent(id)}`),
    briefings: (limit = 30, options = {}) => {
      const params = new URLSearchParams({ limit: String(limit) })
      if (options.cursor) params.set('cursor', options.cursor)
      if (options.q) params.set('q', options.q)
      if (options.bookmarked) params.set('bookmarked', 'true')
      if (typeof options.isRead === 'boolean') params.set('is_read', String(options.isRead))
      return request(`/briefings?${params}`)
    },
    briefing: (id) => request(`/briefings/${encodeURIComponent(id)}`),
    updateBriefingState: (id, body) => request(`/briefings/${encodeURIComponent(id)}/state`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
    traces: () => request('/agent/tool-traces?limit=10'),
    deleteAccount: () => request('/me', { method: 'DELETE' }),
  }
}
