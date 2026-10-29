<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ApiError, createApi } from './api'
import { createTaskMonitor } from './task-monitor'
import { stopReasonLabel } from './task-labels'

const tokenKey = 'ai-news-session'
const token = ref(sessionStorage.getItem(tokenKey) || '')
const api = computed(() => createApi(token.value))
const user = ref(null)
const serviceMode = ref('unknown')
const localLoginEnabled = ref(false)
const emailConfigured = ref(false)
const page = ref('today')
const briefs = ref([])
const selectedBrief = ref(null)
const traces = ref([])
const feedbackItems = ref([])
const feedbackReplies = reactive({})
const feedbackStatuses = reactive({})
const historyItems = ref([])
const historyCursor = ref(null)
const historyHasMore = ref(false)
const historyLoading = ref(false)
const historyQuery = ref('')
const historyBookmarked = ref(false)
const historyReadFilter = ref('all')
let historyRequestId = 0
let historyAppliedFilters = {}
const publicStories = ref([])
const publicStoriesDisclosure = ref('')
const showPublicStories = ref(false)
const busy = ref(false)
const message = ref('')
const error = ref('')
const loginEmail = ref('')
const loginName = ref('')
const loginCode = ref('')
const codeSent = ref(false)
const taskPollError = ref('')
let forceTaskRefresh = false
const activeTaskStates = new Set(['queued', 'enqueued', 'running', 'validating', 'retry_wait'])
function emptySubscription() {
  return {
    configured: false, keywordText: '', excludedText: '',
    topics: [], keywords: [], excluded_keywords: [], match_all: false,
    item_limit: 5, detail_level: 'concise', send_time: '08:00',
    enabled: true, notify_enabled: false, version: 0,
  }
}
const subscription = reactive(emptySubscription())
let sessionEpoch = 0
const topicChoices = [
  { label: '大模型', value: '模型', icon: '◈' },
  { label: '智能体', value: '工具', icon: '⌘' },
  { label: '开源生态', value: '开源', icon: '◉' },
  { label: '研究论文', value: '论文', icon: '⌁' },
  { label: '行业动态', value: '行业', icon: '↗' },
]
const pageMeta = {
  today: ['每日精选', '今日简报'],
  subscription: ['你的关注', '订阅偏好'],
  history: ['过往阅读', '简报历史'],
  agent: ['生成过程', 'Agent 运行记录'],
  account: ['账户与隐私', '个人设置'],
  feedback: ['反馈处理', '举报与更正'],
}
const greetingDate = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
}).format(new Date())
const latestBrief = computed(() => briefs.value[0] || null)
const currentPage = computed(() => pageMeta[page.value] || pageMeta.today)
const topicText = computed(() => subscription.topics.map((value) => topicChoices.find((item) => item.value === value)?.label || value))
const activeTasks = computed(() => traces.value.filter((task) => activeTaskStates.has(task.status)))
const isGenerating = computed(() => activeTasks.value.length > 0)
const taskMonitor = createTaskMonitor({
  load: () => api.value.traces(),
  update: applyTaskUpdate,
  hasActiveTasks: () => Boolean(token.value) && activeTasks.value.length > 0,
  failed: (err) => {
    if (err instanceof ApiError && err.status === 401) handleError(err)
    else taskPollError.value = '暂时无法更新任务状态，将自动重试。后台任务仍由服务器处理。'
  },
})

function clearNotices() {
  error.value = ''
  message.value = ''
}

function handleError(err) {
  if (err instanceof ApiError && err.status === 401) logout()
  error.value = err.message || '请求失败，请稍后重试。'
}

function resetWorkspace() {
  taskMonitor.stop()
  taskPollError.value = ''
  forceTaskRefresh = false
  user.value = null
  page.value = 'today'
  briefs.value = []
  selectedBrief.value = null
  traces.value = []
  feedbackItems.value = []
  for (const key of Object.keys(feedbackReplies)) delete feedbackReplies[key]
  for (const key of Object.keys(feedbackStatuses)) delete feedbackStatuses[key]
  historyRequestId += 1
  historyAppliedFilters = {}
  historyItems.value = []
  historyCursor.value = null
  historyHasMore.value = false
  historyLoading.value = false
  historyQuery.value = ''
  historyBookmarked.value = false
  historyReadFilter.value = 'all'
  for (const key of Object.keys(subscription)) delete subscription[key]
  Object.assign(subscription, emptySubscription())
  busy.value = false
  clearNotices()
}

function isCurrentSession(epoch) {
  return epoch === sessionEpoch && Boolean(token.value)
}

function acceptSession(result) {
  sessionEpoch += 1
  resetWorkspace()
  token.value = result.access_token
  sessionStorage.setItem(tokenKey, result.access_token)
  user.value = result.user
}

function logout() {
  sessionEpoch += 1
  sessionStorage.removeItem(tokenKey)
  token.value = ''
  resetWorkspace()
  loginEmail.value = ''
  loginName.value = ''
  loginCode.value = ''
  codeSent.value = false
}

async function signIn() {
  let epoch = sessionEpoch
  busy.value = true
  clearNotices()
  try {
    const result = await createApi('').demoLogin()
    if (epoch !== sessionEpoch) return
    acceptSession(result)
    epoch = sessionEpoch
    await loadWorkspace()
  } catch (err) {
    if (epoch === sessionEpoch) handleError(err)
  } finally {
    if (epoch === sessionEpoch) busy.value = false
  }
}

async function loadWorkspace() {
  if (!token.value) return
  taskMonitor.stop()
  const epoch = sessionEpoch
  const client = api.value
  busy.value = true
  clearNotices()
  try {
    const [profile, prefs, list, traceList] = await Promise.all([
      client.me(), client.subscription(), client.briefings(), client.traces(),
    ])
    if (!isCurrentSession(epoch)) return
    user.value = profile
    if (prefs.configured) Object.assign(subscription, {
      ...prefs,
      keywordText: (prefs.keywords || []).join(', '),
      excludedText: (prefs.excluded_keywords || []).join(', '),
    })
    else {
      for (const key of Object.keys(subscription)) delete subscription[key]
      Object.assign(subscription, emptySubscription())
    }
    briefs.value = list.items || []
    traces.value = traceList.items || []
    if (page.value === 'history') await loadHistory(true)
    if (isCurrentSession(epoch) && activeTasks.value.length && document.visibilityState === 'visible') taskMonitor.start()
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  } finally {
    if (isCurrentSession(epoch)) busy.value = false
  }
}

async function requestLoginCode() {
  const epoch = sessionEpoch
  busy.value = true
  clearNotices()
  try {
    await createApi('').sendCode(loginEmail.value)
    if (epoch !== sessionEpoch) return
    codeSent.value = true
    message.value = '验证码已发送，请查看邮箱。'
  } catch (err) {
    if (epoch === sessionEpoch) handleError(err)
  } finally {
    if (epoch === sessionEpoch) busy.value = false
  }
}

async function verifyLoginCode() {
  let epoch = sessionEpoch
  busy.value = true
  clearNotices()
  try {
    const result = await createApi('').verifyCode(loginEmail.value, loginCode.value, loginName.value)
    if (epoch !== sessionEpoch) return
    acceptSession(result)
    epoch = sessionEpoch
    await loadWorkspace()
  } catch (err) {
    if (epoch === sessionEpoch) handleError(err)
  } finally {
    if (epoch === sessionEpoch) busy.value = false
  }
}

function readList(raw) {
  return raw.split(/[，,\n]/).map((word) => word.trim()).filter(Boolean)
}

async function savePreferences() {
  const epoch = sessionEpoch
  const client = api.value
  busy.value = true
  clearNotices()
  try {
    const result = await client.saveSubscription({
      topics: subscription.topics,
      keywords: readList(subscription.keywordText || ''),
      excluded_keywords: readList(subscription.excludedText || ''),
      match_all: subscription.match_all,
      semantic_expansion: false,
      item_limit: Number(subscription.item_limit),
      detail_level: subscription.detail_level,
      send_time: subscription.send_time,
      enabled: subscription.enabled,
      notify_enabled: subscription.notify_enabled,
      version: subscription.version || 0,
    })
    if (!isCurrentSession(epoch)) return
    Object.assign(subscription, result)
    message.value = '订阅已保存，下一期会按这组偏好生成。'
    await refreshBriefings()
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  } finally {
    if (isCurrentSession(epoch)) busy.value = false
  }
}

async function refreshBriefings(stillCurrent = () => true) {
  const epoch = sessionEpoch
  const result = await api.value.briefings()
  if (isCurrentSession(epoch) && stillCurrent()) briefs.value = result.items || []
}

async function showHistory() {
  page.value = 'history'
  selectedBrief.value = null
  await loadHistory(true)
}

async function loadHistory(reset = false) {
  if (!token.value || (historyLoading.value && !reset)) return
  const epoch = sessionEpoch
  const requestId = ++historyRequestId
  const client = api.value
  const filters = reset ? {
    q: historyQuery.value.trim(), bookmarked: historyBookmarked.value,
    isRead: historyReadFilter.value === 'all' ? undefined : historyReadFilter.value === 'read',
  } : historyAppliedFilters
  const options = { ...filters, cursor: reset ? undefined : historyCursor.value }
  historyLoading.value = true
  clearNotices()
  try {
    const result = await client.briefings(30, options)
    if (!isCurrentSession(epoch) || requestId !== historyRequestId) return
    const items = result.items || []
    if (reset) historyAppliedFilters = filters
    historyItems.value = reset ? items : [...new Map([...historyItems.value, ...items].map((item) => [item.id, item])).values()]
    historyCursor.value = result.next_cursor || result.next_before || null
    historyHasMore.value = Boolean(result.has_more && historyCursor.value)
  } catch (err) {
    if (isCurrentSession(epoch) && requestId === historyRequestId) handleError(err)
  } finally {
    if (isCurrentSession(epoch) && requestId === historyRequestId) historyLoading.value = false
  }
}

function syncBriefState(id, state) {
  for (const list of [briefs.value, historyItems.value]) {
    const row = list.find((item) => item.id === id)
    if (row) Object.assign(row, state)
  }
  if (selectedBrief.value?.id === id) Object.assign(selectedBrief.value, state)
}

async function generatePreview() {
  if (busy.value || isGenerating.value) return
  const epoch = sessionEpoch
  const client = api.value
  busy.value = true
  clearNotices()
  message.value = '正在创建任务…'
  try {
    const task = await client.createTask('preview')
    if (!isCurrentSession(epoch)) return
    traces.value = [task, ...traces.value.filter((item) => item.id !== task.id)]
    message.value = '任务已创建，Agent 会在后台整理，完成后自动更新简报。'
    taskMonitor.start(true)
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  } finally {
    if (isCurrentSession(epoch)) busy.value = false
  }
}

async function applyTaskUpdate(result, stillCurrent) {
  const epoch = sessionEpoch
  const previousActive = new Set(activeTasks.value.map((task) => task.id))
  const updated = result.items || []
  const completed = updated.filter((task) => previousActive.has(task.id) && !activeTaskStates.has(task.status))
  const missingPublished = updated.some((task) => task.status === 'published' && !briefs.value.some((brief) => brief.task_id === task.id))
  const refresh = forceTaskRefresh || completed.length > 0 || missingPublished
  forceTaskRefresh = false
  taskPollError.value = ''
  if (refresh) {
    await refreshBriefings(stillCurrent)
    if (!isCurrentSession(epoch) || !stillCurrent()) return
    if (page.value === 'history') await loadHistory(true)
    if (!isCurrentSession(epoch) || !stillCurrent()) return
  }
  traces.value = updated
  if (completed.length) {
    const task = completed[0]
    if (task.status === 'failed') error.value = task.error_message || '简报生成失败，请查看 Agent 记录。'
    else message.value = task.status === 'published' ? '简报已更新。' : `任务状态：${statusLabel(task.status)}`
  }
}

function refreshTaskState() {
  if (!token.value) return
  forceTaskRefresh = true
  taskMonitor.start(true)
}

async function openFeedbackQueue() {
  page.value = 'feedback'
  const epoch = sessionEpoch
  busy.value = true
  clearNotices()
  try {
    const result = await api.value.adminFeedback()
    if (!isCurrentSession(epoch)) return
    feedbackItems.value = result.items || []
    for (const item of feedbackItems.value) {
      feedbackReplies[item.id] = item.reply || ''
      feedbackStatuses[item.id] = item.status
    }
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  } finally {
    if (isCurrentSession(epoch)) busy.value = false
  }
}

async function saveFeedback(item) {
  if (['resolved', 'dismissed'].includes(feedbackStatuses[item.id]) && !feedbackReplies[item.id]?.trim()) {
    error.value = '标记为已解决或不采纳时，请填写给用户的回复。'
    return
  }
  const epoch = sessionEpoch
  busy.value = true
  clearNotices()
  try {
    const updated = await api.value.updateFeedback(item.id, {
      status: feedbackStatuses[item.id], reply: feedbackReplies[item.id] || '',
    })
    if (!isCurrentSession(epoch)) return
    const index = feedbackItems.value.findIndex((row) => row.id === item.id)
    if (index >= 0) feedbackItems.value[index] = updated
    message.value = '处理结果已保存，用户可在 App 中查看。'
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  } finally {
    if (isCurrentSession(epoch)) busy.value = false
  }
}

function feedbackStatusLabel(value) {
  return ({ open: '待处理', in_review: '处理中', resolved: '已解决', dismissed: '不采纳' })[value] || value
}

function feedbackKindLabel(value) {
  return ({ incorrect: '内容不准确 / 更正', source_issue: '来源或链接问题', irrelevant: '与主题无关', other: '其他问题' })[value] || value
}

function handleVisibilityChange() {
  if (document.visibilityState === 'hidden') taskMonitor.stop()
  else refreshTaskState()
}

function formatTokenUsage(usage) {
  const prompt = Number(usage?.prompt_tokens || 0)
  const completion = Number(usage?.completion_tokens || 0)
  if (!prompt && !completion) return ''
  const number = new Intl.NumberFormat('zh-CN')
  return '模型用量：输入 ' + number.format(prompt) + ' · 输出 ' + number.format(completion) + ' Token'
}

function statusLabel(value) {
  return ({ queued: '排队中', enqueued: '已入队', retry_wait: '等待重试', running: '生成中', validating: '核验中', published: '已完成', no_match: '暂无匹配', insufficient_data: '材料不足', failed: '失败', cancelled: '已取消' })[value] || value || '未知'
}

async function openBrief(brief) {
  const epoch = sessionEpoch
  const client = api.value
  selectedBrief.value = brief
  page.value = 'today'
  try {
    const detail = await client.briefing(brief.id)
    if (!isCurrentSession(epoch)) return
    selectedBrief.value = detail
    if (!detail.is_read) {
      await client.updateBriefingState(detail.id, { is_read: true })
      if (!isCurrentSession(epoch)) return
      detail.is_read = true
      syncBriefState(detail.id, { is_read: true })
    }
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  }
}

async function toggleBookmark(brief) {
  const epoch = sessionEpoch
  const client = api.value
  try {
    const updated = await client.updateBriefingState(brief.id, { is_bookmarked: !brief.is_bookmarked })
    if (!isCurrentSession(epoch)) return
    syncBriefState(brief.id, { is_bookmarked: updated.is_bookmarked })
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  }
}

async function deleteAccount() {
  if (!window.confirm('确定注销当前账户并删除账户数据吗？')) return
  const epoch = sessionEpoch
  const client = api.value
  try {
    await client.deleteAccount()
    if (isCurrentSession(epoch)) logout()
  } catch (err) {
    if (isCurrentSession(epoch)) handleError(err)
  }
}

onMounted(async () => {
  document.addEventListener('visibilitychange', handleVisibilityChange)
  try {
    const health = await createApi('').health()
    serviceMode.value = health.mode
    localLoginEnabled.value = health.local_login_enabled
    emailConfigured.value = health.email_configured
  } catch {
    serviceMode.value = 'unavailable'
  }
  try {
    const demo = await createApi('').publicDemoStories()
    publicStories.value = demo.items || []
    publicStoriesDisclosure.value = demo.disclosure || '以下均为明确标注的虚构演示素材。'
  } catch {
    publicStories.value = []
  }
  if (token.value) await loadWorkspace()
})

onUnmounted(() => {
  taskMonitor.stop()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <main v-if="!user" class="welcome-shell">
    <section class="welcome-card">
      <div class="brand-lockup"><span class="brand-mark">P</span><span>PulseBrief</span></div>
      <div class="welcome-copy">
        <p class="eyebrow">YOUR DAILY SIGNAL</p>
        <h1>把每天的<br /><em>重要新闻</em>读明白。</h1>
        <p class="welcome-subtitle">依据你的关注，每天整理一份有来源、可追溯的新闻简报。</p>
        <button v-if="localLoginEnabled" class="button button-primary welcome-button" :disabled="busy" @click="signIn">
          {{ busy ? '正在连接…' : serviceMode === 'live' ? '进入简报工作台' : '进入演示工作台' }} <span aria-hidden="true">↗</span>
        </button>
        <p class="demo-note"><span class="status-dot"></span>{{ serviceMode === 'live' ? (localLoginEnabled ? '真实模型 · 使用本地体验账户' : '真实模型服务') : serviceMode === 'unavailable' ? '服务暂时无法连接' : '本地演示 · 新闻数据为虚构样例' }}</p>
        <button class="guest-preview-button" :disabled="!publicStories.length" :aria-expanded="showPublicStories" aria-controls="public-demo-stories" @click="showPublicStories = !showPublicStories">{{ showPublicStories ? '收起公开演示' : '先浏览公开演示' }} <span aria-hidden="true">{{ showPublicStories ? '↑' : '↓' }}</span></button>
        <details v-if="emailConfigured" class="login-details" :open="!localLoginEnabled"><summary>邮箱验证码登录</summary><div class="login-form"><input v-model="loginEmail" class="text-input" type="email" autocomplete="email" aria-label="邮箱地址" placeholder="邮箱地址" /><input v-model="loginName" class="text-input" autocomplete="name" aria-label="显示名称" placeholder="显示名称（首次登录填写）" /><div v-if="codeSent" class="code-row"><input v-model="loginCode" class="text-input" inputmode="numeric" autocomplete="one-time-code" maxlength="6" aria-label="6 位邮箱验证码" placeholder="6 位验证码" /><button class="button button-light" :disabled="busy" @click="verifyLoginCode">登录</button></div><button v-else class="button button-light" :disabled="busy || !loginEmail" @click="requestLoginCode">发送验证码</button></div></details>
        <p v-else-if="!localLoginEnabled" class="demo-note">邮箱登录暂未配置，请联系服务运营方。</p>
        <p v-if="message" class="notice notice-success" role="status" aria-live="polite">{{ message }}</p>
        <p v-if="error" class="notice notice-error" role="alert" aria-live="assertive">{{ error }}</p>
      </div>
      <div class="welcome-art" aria-hidden="true">
        <div class="orbit orbit-one"></div><div class="orbit orbit-two"></div>
        <div class="orbit-core"><span>AI</span><i>✳</i></div>
        <span class="orbit-label label-one">模型</span><span class="orbit-label label-two">研究</span><span class="orbit-label label-three">开源</span>
      </div>
      <div class="welcome-footer"><span>LESS NOISE, MORE SIGNAL</span><span>VOL. 01 / 2026</span></div>
    </section>
    <section v-if="showPublicStories" id="public-demo-stories" class="public-demo-panel" aria-labelledby="public-demo-title">
      <div class="public-demo-heading"><div><p class="eyebrow">无需登录</p><h2 id="public-demo-title">公开浏览演示简报</h2></div><button class="icon-button" aria-label="收起公开演示" @click="showPublicStories = false">×</button></div>
      <p class="public-demo-disclosure">{{ publicStoriesDisclosure }}</p>
      <article v-for="story in publicStories" :key="story.title" class="public-demo-story">
        <span class="pill pill-demo">虚构演示素材</span><h3>{{ story.title }}</h3><p>{{ story.summary }}</p>
      </article>
      <p class="public-demo-privacy">只读预览，不创建账户、不保存订阅偏好，也不会发送邮件提醒。</p>
    </section>
  </main>

  <div v-else class="app-frame">
    <a class="skip-link" href="#main-content">跳至主要内容</a>
    <aside class="sidebar">
      <a class="brand-lockup" href="#" @click.prevent="page = 'today'; selectedBrief = null"><span class="brand-mark">P</span><span>PulseBrief</span></a>
      <div class="workspace-label">工作台</div>
      <nav class="nav-list" aria-label="主导航">
        <button :class="['nav-item', { active: page === 'today' }]" :aria-current="page === 'today' ? 'page' : undefined" @click="page = 'today'; selectedBrief = null"><span class="nav-icon" aria-hidden="true">◷</span><span>今日简报</span><span v-if="latestBrief && !latestBrief.is_read" class="nav-unread" role="img" aria-label="有未读简报"></span></button>
        <button :class="['nav-item', { active: page === 'subscription' }]" :aria-current="page === 'subscription' ? 'page' : undefined" @click="page = 'subscription'"><span class="nav-icon" aria-hidden="true">⌘</span><span>订阅偏好</span></button>
        <button :class="['nav-item', { active: page === 'history' }]" :aria-current="page === 'history' ? 'page' : undefined" @click="showHistory"><span class="nav-icon" aria-hidden="true">▤</span><span>历史简报</span></button>
        <button :class="['nav-item', { active: page === 'agent' }]" :aria-current="page === 'agent' ? 'page' : undefined" @click="page = 'agent'"><span class="nav-icon" aria-hidden="true">✳</span><span>Agent 记录</span></button>
        <button v-if="user.is_admin" :class="['nav-item', { active: page === 'feedback' }]" :aria-current="page === 'feedback' ? 'page' : undefined" @click="openFeedbackQueue"><span class="nav-icon" aria-hidden="true">✉</span><span>反馈处理</span></button>
      </nav>
      <div class="sidebar-bottom">
        <div class="tip-card"><span class="tip-icon">✳</span><p>每天一份，<br />只读你关心的。</p><span class="tip-lines">━━━━━</span></div>
        <button :class="['nav-item', { active: page === 'account' }]" :aria-current="page === 'account' ? 'page' : undefined" @click="page = 'account'"><span class="nav-icon" aria-hidden="true">◉</span><span>账户设置</span></button>
        <div class="user-chip"><div class="avatar">{{ (user.display_name || '读').slice(0, 1) }}</div><div class="user-label"><strong>{{ user.display_name || '访客' }}</strong><small>{{ user.is_demo ? (serviceMode === 'live' ? '本地体验账户' : '演示账户') : user.email }}</small></div><button class="icon-button logout-button" aria-label="退出登录" @click="logout">↗</button></div>
      </div>
    </aside>

    <main class="main-column" id="main-content" tabindex="-1">
      <header class="topbar"><div class="breadcrumb"><span>PulseBrief</span><i aria-hidden="true">/</i><strong>{{ currentPage[0] }}</strong></div><div class="topbar-right"><span class="server-status"><i aria-hidden="true"></i> 服务运行中</span><button class="icon-button" aria-label="刷新工作台" :disabled="busy" @click="loadWorkspace">↻</button></div></header>
      <div class="page-content">
        <div v-if="error" class="notice notice-error" role="alert" aria-live="assertive"><span>{{ error }}</span><button class="notice-close" aria-label="关闭错误提示" @click="error = ''">×</button></div>
        <div v-if="message" class="notice notice-success" role="status" aria-live="polite"><span>{{ message }}</span><button class="notice-close" aria-label="关闭操作提示" @click="message = ''">×</button></div>
        <div v-if="activeTasks.length || taskPollError" class="task-progress" role="status" aria-live="polite"><div><strong>{{ activeTasks.length ? `${activeTasks.length} 个任务处理中 · ${statusLabel(activeTasks[0].status)}` : '任务状态更新暂不可用' }}</strong><p>{{ taskPollError || '结果由服务器保存；可以继续浏览，返回网页后也能查看。' }}</p></div><button class="button button-light" type="button" @click="refreshTaskState">刷新状态</button></div>

        <template v-if="page === 'today'">
          <div class="page-heading heading-row"><div><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ selectedBrief ? selectedBrief.title : '早上好，' + (user.display_name || '读者') }}<span v-if="!selectedBrief" class="heading-spark" aria-hidden="true">✳</span></h1><p class="heading-subtitle">{{ selectedBrief ? `${selectedBrief.business_date} · ${selectedBrief.items?.length || 0} 条精选` : greetingDate }}</p></div><button v-if="!selectedBrief" class="button button-primary" :disabled="busy || isGenerating" @click="generatePreview"><span aria-hidden="true">{{ busy || isGenerating ? '◌' : '✳' }}</span> {{ busy || isGenerating ? '正在整理' : '生成预览' }}</button><button v-else class="button button-light" @click="selectedBrief = null">返回今日</button></div>

          <template v-if="selectedBrief">
            <section class="brief-detail-card">
              <div class="brief-detail-head"><div><span class="pill pill-soft">{{ selectedBrief.is_demo ? '演示素材' : '每日简报' }}</span><span class="detail-date">{{ selectedBrief.business_date }}</span></div><button :class="['bookmark-button', { saved: selectedBrief.is_bookmarked }]" :aria-pressed="selectedBrief.is_bookmarked" @click="toggleBookmark(selectedBrief)">{{ selectedBrief.is_bookmarked ? '★ 已收藏' : '☆ 收藏' }}</button></div>
              <p class="brief-lead">{{ selectedBrief.summary }}</p>
              <div class="article-list detail-list">
                <article v-for="(item, index) in selectedBrief.items || []" :key="item.article_id || index" class="article-row">
                  <div class="article-index">{{ String(index + 1).padStart(2, '0') }}</div>
                  <div class="article-body">
                    <div class="article-meta"><span>{{ item.source_name || item.source_id || '内容来源' }}</span><i aria-hidden="true">·</i><span>{{ item.content_type === 'news' ? '新闻' : '专题文章' }}</span><i aria-hidden="true">·</i><span>{{ item.published_at ? new Date(item.published_at).toLocaleDateString('zh-CN') : '今日' }}</span><span v-if="item.is_demo || item.certainty === 'demo'" class="demo-tag">演示</span></div>
                    <h3>{{ item.title || `精选内容 ${index + 1}` }}</h3>
                    <small v-if="item.original_title && item.original_title !== item.title" class="original-title">原文：{{ item.original_title }}</small>
                    <p>{{ item.summary || item.reason }}</p>
                    <div v-if="item.reason" class="why-row"><span>与你相关</span>{{ item.reason }}</div>
                    <a v-if="item.source_url" class="source-link" :href="item.source_url" target="_blank" rel="noopener noreferrer" aria-label="打开来源原文，将在新标签页打开">阅读原文 <span aria-hidden="true">↗</span></a>
                    <div v-if="item.republish_credit || item.license_name || item.change_notice" class="rights-note" aria-label="来源与许可说明">
                      <p v-if="item.republish_credit">{{ item.republish_credit }}</p>
                      <a v-if="item.license_name && item.license_url" :href="item.license_url" target="_blank" rel="noopener noreferrer">许可：{{ item.license_name }}</a>
                      <p v-if="item.change_notice">{{ item.change_notice }}</p>
                    </div>
                  </div>
                </article>
                <div v-if="!selectedBrief.items?.length" class="empty-state compact"><span class="empty-icon">⌁</span><h3>这期没有匹配内容</h3><p>可以调整订阅偏好，或稍后再试。</p></div>
              </div>
              <div class="ai-disclosure">✳ <span>本简报由 AI 根据所列来源整理，请点击原文核实重要信息。</span></div>
            </section>
          </template>

          <template v-else>
            <section class="hero-card">
              <div class="hero-copy"><div class="hero-kicker"><span class="live-pulse"></span> PERSONALIZED BRIEFING <span class="hero-date">{{ latestBrief?.business_date || '今日' }}</span></div><h2>{{ latestBrief?.title || '今天有哪些值得关注的新闻？' }}</h2><p>{{ latestBrief?.summary || '根据你的订阅偏好，从当前已接入且获准使用的来源中整理近期内容；覆盖范围会受来源许可与可达性影响。' }}</p><button class="hero-link" :disabled="!latestBrief" @click="latestBrief && openBrief(latestBrief)">{{ latestBrief ? '阅读今日简报' : '等待今日内容' }} <span>→</span></button></div>
              <div class="hero-visual" aria-hidden="true"><div class="hero-sun"></div><div class="hero-ring ring-a"></div><div class="hero-ring ring-b"></div><span class="hero-star star-a">✳</span><span class="hero-star star-b">✦</span><span class="hero-coordinate">31°13′ N<br />121°28′ E</span><span class="hero-word">SIGNAL<br />FOUND</span></div>
              <div class="hero-foot"><span>{{ latestBrief ? `${latestBrief.items?.length || 0} 条精选 · 约 3 分钟` : '有来源 · 可追溯 · 按偏好整理' }}</span><span class="hero-foot-line"></span><span>PULSEBRIEF / {{ latestBrief?.business_date || 'TODAY' }}</span></div>
            </section>

            <div class="section-heading"><div><p class="eyebrow">YOUR READING LIST</p><h2>最近简报</h2></div><button class="text-button" @click="showHistory">查看历史 <span>→</span></button></div>
            <div v-if="briefs.length" class="recent-grid">
              <button v-for="brief in briefs.slice(0, 3)" :key="brief.id" class="brief-card" @click="openBrief(brief)"><div class="brief-card-top"><span class="pill" :class="brief.is_demo ? 'pill-demo' : 'pill-soft'">{{ brief.is_demo ? '演示' : '已发布' }}</span><span>{{ brief.business_date }}</span></div><h3>{{ brief.title }}</h3><p>{{ brief.summary }}</p><div class="brief-card-foot"><span>{{ brief.items?.length || 0 }} 条内容</span><span>→</span></div></button>
            </div>
            <div v-else class="empty-state"><span class="empty-icon">⌁</span><h3>你的简报会出现在这里</h3><p>先设置感兴趣的话题，再生成一份预览简报。</p><button class="button button-light" @click="page = 'subscription'">设置我的订阅</button></div>
            <div class="bottom-note"><span>✳</span> 当前只展示已接入且获准使用的来源，国内新闻覆盖尚不完整。没有匹配结果不代表当天没有相关新闻；重要信息请查看原文。</div>
          </template>
        </template>

        <template v-else-if="page === 'subscription'">
          <div class="page-heading"><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ currentPage[1] }}</h1><p class="heading-subtitle">选好你关注的方向，简报会按此筛选与整理。</p></div>
          <section class="settings-card"><div class="settings-section"><div class="settings-title"><div><h2>关注话题</h2><p>选择你希望持续追踪的 AI 领域。</p></div><span class="step-number" aria-hidden="true">01</span></div><div class="topic-grid"><button v-for="topic in topicChoices" :key="topic.value" :class="['topic-option', { selected: subscription.topics.includes(topic.value) }]" :aria-pressed="subscription.topics.includes(topic.value)" @click="subscription.topics = subscription.topics.includes(topic.value) ? subscription.topics.filter((item) => item !== topic.value) : [...subscription.topics, topic.value]"><span class="topic-icon" aria-hidden="true">{{ topic.icon }}</span><span>{{ topic.label }}</span><span class="check-mark" aria-hidden="true">{{ subscription.topics.includes(topic.value) ? '✓' : '+' }}</span></button></div></div>
            <div class="settings-section"><div class="settings-title"><div><h2>关键词</h2><p>用逗号分隔，帮助我们更精确地找到相关内容。</p></div><span class="step-number">02</span></div><label class="field-label" for="keywords">包含关键词</label><input id="keywords" v-model="subscription.keywordText" class="text-input" placeholder="例如：多模态、开源模型、推理" /><label class="field-label field-spaced" for="exclude">排除关键词 <span>可选</span></label><input id="exclude" v-model="subscription.excludedText" class="text-input" placeholder="例如：招聘、课程广告" /></div>
            <div class="settings-section setting-row"><div class="settings-title"><div><h2>每日简报</h2><p>设置简报条数与推送时间。按北京时间执行。</p></div><span class="step-number">03</span></div><div class="control-grid"><label><span class="field-label">每天发送时间</span><input v-model="subscription.send_time" class="text-input" type="time" /></label><label><span class="field-label">每期条数</span><select v-model.number="subscription.item_limit" class="text-input"><option :value="5">5 条</option><option :value="10">10 条</option><option :value="15">15 条</option></select></label><label><span class="field-label">摘要风格</span><select v-model="subscription.detail_level" class="text-input"><option value="concise">简洁速读</option><option value="detailed">详细解读</option></select></label></div><label class="toggle-row"><span><strong>启用每日订阅</strong><small>关闭后停止生成新一期，已保存简报仍可查看</small></span><input v-model="subscription.enabled" type="checkbox" /><i></i></label><label class="toggle-row"><span><strong>邮件提醒</strong><small v-if="!emailConfigured">当前服务未配置邮件发送；简报仍会保存在网站</small><small v-else-if="!user?.email_verified">验证邮箱后即可开启提醒</small><small v-else>简报生成后发送一封只含访问链接的提醒邮件</small></span><input v-model="subscription.notify_enabled" type="checkbox" :disabled="busy || !emailConfigured || !user?.email_verified || user?.is_demo" /><i></i></label></div>
            <div class="settings-footer"><span v-if="subscription.next_run_at">下一次计划：{{ new Date(subscription.next_run_at).toLocaleString('zh-CN') }}</span><span v-else>偏好保存后可随时预览</span><button class="button button-primary" :disabled="busy" @click="savePreferences">{{ busy ? '正在保存…' : '保存订阅偏好' }} <span>→</span></button></div>
          </section>
        </template>

        <template v-else-if="page === 'history'">
          <div class="page-heading"><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ currentPage[1] }}</h1><p class="heading-subtitle">所有简报都保存在这里，方便随时回看。</p></div>
          <form class="history-filters" @submit.prevent="loadHistory(true)">
            <label class="history-search"><span class="field-label">按标题搜索</span><input v-model="historyQuery" class="text-input" type="search" maxlength="200" placeholder="输入简报标题" /></label>
            <label><span class="field-label">阅读状态</span><select v-model="historyReadFilter" class="text-input"><option value="all">全部</option><option value="unread">未读</option><option value="read">已读</option></select></label>
            <label class="history-bookmarked"><input v-model="historyBookmarked" type="checkbox" />只看收藏</label>
            <button class="button button-light" type="submit" :disabled="historyLoading">筛选</button>
          </form>
          <p v-if="historyLoading && !historyItems.length" role="status">正在加载历史简报…</p>
          <div v-else-if="historyItems.length" class="history-list"><button v-for="brief in historyItems" :key="brief.id" class="history-row" @click="openBrief(brief)"><div class="history-date"><strong>{{ brief.business_date?.slice(8) || '—' }}</strong><span>{{ brief.business_date?.slice(0, 7) }}</span></div><div class="history-copy"><div class="article-meta"><span>{{ brief.is_demo ? '演示简报' : '每日简报' }}</span><i>·</i><span>{{ brief.items?.length || 0 }} 条</span><span v-if="!brief.is_read" class="new-label">未读</span><span v-if="brief.is_bookmarked">★ 收藏</span></div><h3>{{ brief.title }}</h3><p>{{ brief.summary }}</p></div><span class="history-arrow">↗</span></button></div>
          <div v-else class="empty-state"><span class="empty-icon">▤</span><h3>没有符合条件的历史简报</h3><p>可以调整筛选条件，或生成一份新的简报。</p></div>
          <div v-if="historyItems.length" class="history-pagination"><span aria-live="polite">已加载 {{ historyItems.length }} 份</span><button v-if="historyHasMore" class="button button-light" type="button" :disabled="historyLoading" @click="loadHistory(false)">{{ historyLoading ? '正在加载…' : '加载更多' }}</button></div>
        </template>

        <template v-else-if="page === 'agent'">
          <div class="page-heading"><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ currentPage[1] }}</h1><p class="heading-subtitle">查看任务状态、工具调用和停止原因，不展示模型内部思维链。</p></div>
          <div v-if="traces.length" class="trace-list"><section v-for="trace in traces" :key="trace.id" class="trace-card"><div class="trace-head"><div><span class="pill" :class="trace.provider === 'demo' ? 'pill-demo' : 'pill-soft'">{{ trace.provider === 'demo' ? '演示 Agent' : '真实模型' }}</span><span class="trace-date">{{ new Date(trace.created_at).toLocaleString('zh-CN') }}</span></div><span class="trace-status">{{ statusLabel(trace.status) }}</span></div><div v-if="trace.tools?.length" class="tool-steps"><div v-for="(tool, index) in trace.tools" :key="index" class="tool-step"><span class="tool-step-index">{{ String(index + 1).padStart(2, '0') }}</span><div><strong>{{ tool.tool || '工具调用' }}</strong><p>{{ tool.ok ? '执行完成' : '执行未通过' }}<span v-if="tool.elapsed_ms"> · {{ tool.elapsed_ms }} ms</span></p></div><span class="tool-step-ok">{{ tool.ok ? '✓' : '!' }}</span></div></div><p v-if="formatTokenUsage(trace.token_usage)" class="token-usage">{{ formatTokenUsage(trace.token_usage) }}</p><p class="stop-reason">停止原因：{{ stopReasonLabel(trace.stop_reason) }}</p></section></div>
          <div v-else class="empty-state"><span class="empty-icon">✳</span><h3>还没有 Agent 运行记录</h3><p>生成一次预览后，可以查看模型选择的工具路径。</p><button class="button button-light" @click="page = 'today'">去生成预览</button></div>
        </template>

        <template v-else-if="page === 'account'">
          <div class="page-heading"><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ currentPage[1] }}</h1><p class="heading-subtitle">管理登录身份、邮件偏好和个人数据。</p></div>
          <section class="settings-card account-card"><div class="account-profile"><div class="avatar avatar-large">{{ (user.display_name || '读').slice(0, 1) }}</div><div><h2>{{ user.display_name || '读者' }}</h2><p>{{ user.email }} <span v-if="user.is_demo" class="pill pill-demo">演示账户</span><span v-else-if="user.email_verified" class="pill pill-soft">邮箱已验证</span></p></div></div><div class="settings-section"><h2>通知与邮件</h2><p class="section-description">{{ emailConfigured ? '邮件发送服务已配置。你可以在订阅偏好中主动开启或关闭提醒。' : '当前服务未配置邮件发送；简报仍会按计划生成并保存在网站。' }}</p><div class="account-note"><span>i</span><p>邮件仅提醒简报已生成，不包含订阅关键词或简报正文。关闭邮件提醒不会停止简报生成。</p></div><button class="button button-light" style="margin-top: 14px" @click="page = 'subscription'">管理订阅与提醒</button></div><div class="settings-section danger-zone"><div><h2>注销账户</h2><p>删除账户及关联订阅、简报和个人阅读状态。</p></div><button class="button button-danger" @click="deleteAccount">注销当前账户</button></div></section>
        </template>

        <template v-else-if="page === 'feedback' && user.is_admin">
          <div class="page-heading"><p class="eyebrow">{{ currentPage[0] }}</p><h1>{{ currentPage[1] }}</h1><p class="heading-subtitle">核查读者提交的问题，并填写用户可见的处理回复。</p></div>
          <section v-if="feedbackItems.length" class="trace-list">
            <article v-for="item in feedbackItems" :key="item.id" class="trace-card">
              <div class="trace-head"><div><strong>{{ feedbackKindLabel(item.kind) }}</strong><span class="trace-date">{{ item.business_date }} · {{ item.briefing_title }}</span></div><span class="trace-status">{{ feedbackStatusLabel(item.status) }}</span></div>
              <p v-if="item.item_index !== null" class="stop-reason">对应简报条目：{{ item.item_index + 1 }}</p>
              <p class="section-description">{{ item.detail }}</p>
              <label class="field-label" :for="`feedback-status-${item.id}`">处理状态</label>
              <select :id="`feedback-status-${item.id}`" v-model="feedbackStatuses[item.id]" class="text-input"><option value="open">待处理</option><option value="in_review">处理中</option><option value="resolved">已解决</option><option value="dismissed">不采纳</option></select>
              <label class="field-label field-spaced" :for="`feedback-reply-${item.id}`">给用户的回复</label>
              <textarea :id="`feedback-reply-${item.id}`" v-model="feedbackReplies[item.id]" class="text-input text-area-input" rows="3" maxlength="1000" placeholder="说明核查结果或更正情况；完成处理时必须填写。"></textarea>
              <button class="button button-primary" :disabled="busy" style="margin-top: 12px" @click="saveFeedback(item)">保存处理结果</button>
            </article>
          </section>
          <div v-else class="empty-state"><span class="empty-icon">✓</span><h3>目前没有待查看的反馈</h3><p>用户提交的举报和更正会出现在这里。</p></div>
        </template>

        <footer class="page-footer"><span>PULSEBRIEF · 有来源的每日新闻简报</span><span>内容由 AI 整理，请以来源原文为准</span></footer>
      </div>
    </main>
  </div>
</template>
