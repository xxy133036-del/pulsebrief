// Optional local browser regression. Uses an installed Chromium browser and Node built-ins.
// Every /api/v1 request is mocked; this probe never calls a model or sends email.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { resolve, sep } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'

const browserPath = process.env.BROWSER_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'
const baseUrl = process.env.PROBE_BASE_URL || 'http://127.0.0.1:8080'
const outputRoot = resolve(process.env.PROBE_OUTPUT_DIR || 'F:/Resources/pulsebrief/browser-checks')
await mkdir(outputRoot, { recursive: true })
const profileRoot = resolve(outputRoot, 'profiles')
await mkdir(profileRoot, { recursive: true })
const profile = await mkdtemp(resolve(profileRoot, 'session-'))
const browser = spawn(browserPath, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank',
], { windowsHide: true, stdio: 'ignore' })
let browserError
browser.on('error', (error) => { browserError = error })
let socket
let command
const pending = new Map()
let sequence = 0
let heldRequest
let holdNextSubscription = false
let loginCount = 0
let eventError
let probeTask = null
let probeTaskStatus = 'queued'
let taskCreates = 0
let taskTraceRequests = 0
const historyRows = Array.from({ length: 35 }, (_, index) => ({
  id: `history-${index}`, task_id: `task-${index}`, business_date: '2026-10-15',
  title: index === 34 ? 'History target 34' : `History briefing ${index}`,
  summary: 'Browser-only demo fixture', items: [], is_demo: true,
  is_read: index % 3 === 0, is_bookmarked: index === 34,
}))

async function waitFor(check, label) {
  const deadline = Date.now() + 15000
  while (Date.now() < deadline) {
    if (browserError) throw browserError
    if (eventError) throw eventError
    if (await check()) return
    await delay(50)
  }
  throw new Error(`Timed out: ${label}`)
}

function account(headers) {
  return Object.entries(headers).find(([key]) => key.toLowerCase() === 'authorization')?.[1] === 'Bearer browser-B' ? 'B' : 'A'
}

function user(which) {
  return { id: which, email: `${which.toLowerCase()}@example.invalid`, display_name: `浏览器${which === 'A' ? '甲' : '乙'}`, is_demo: true, email_verified: false }
}

const firstPreferences = { configured: true, topics: ['模型'], keywords: ['FIRST_PRIVATE_KEYWORD'], excluded_keywords: ['PRIVATE_EXCLUDE'], match_all: false, item_limit: 15, detail_level: 'detailed', send_time: '09:30', enabled: true, notify_enabled: false, version: 7 }

async function fulfill(requestId, payload) {
  await command('Fetch.fulfillRequest', { requestId, responseCode: 200, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }], body: Buffer.from(JSON.stringify(payload)).toString('base64') })
}

async function route({ requestId, request }) {
  const url = new URL(request.url)
  const path = url.pathname.replace('/api/v1', '')
  const which = account(request.headers)
  let payload
  if (path === '/health') payload = { mode: 'live', local_login_enabled: true, email_configured: false }
  else if (path === '/public/demo-stories') payload = { items: [], disclosure: '' }
  else if (path === '/auth/demo') {
    loginCount += 1
    assert.ok(loginCount <= 2, 'Unexpected login request')
    const selected = loginCount === 1 ? 'A' : 'B'
    payload = { access_token: `browser-${selected}`, user: user(selected), demo_mode: true }
  } else if (path === '/me') payload = user(which)
  else if (path === '/subscription') {
    payload = which === 'A' ? firstPreferences : { configured: false }
    if (which === 'A' && holdNextSubscription) {
      holdNextSubscription = false
      heldRequest = { requestId, payload }
      return
    }
  } else if (path === '/briefings') {
    let rows = which === 'B' ? historyRows : []
    if (which === 'B' && probeTaskStatus === 'published' && probeTask) rows = [{ ...historyRows[0], id: 'generated-preview', task_id: probeTask.id, title: 'Auto updated briefing' }, ...rows]
    if (url.searchParams.get('q')) rows = rows.filter((row) => row.title.toLowerCase().includes(url.searchParams.get('q').toLowerCase()))
    if (url.searchParams.get('bookmarked') === 'true') rows = rows.filter((row) => row.is_bookmarked)
    if (url.searchParams.has('is_read')) rows = rows.filter((row) => String(row.is_read) === url.searchParams.get('is_read'))
    const offset = url.searchParams.has('cursor') ? 30 : 0
    const more = rows.length > offset + 30
    payload = { items: rows.slice(offset, offset + 30), has_more: more, next_cursor: more ? 'mock-history-page-2' : null }
  }
  else if (path === '/briefing-tasks' && request.method === 'POST') {
    taskCreates += 1
    probeTaskStatus = 'queued'
    probeTask = { id: `browser-generation-${taskCreates}`, kind: 'preview-probe', status: probeTaskStatus, provider: 'demo', created_at: '2026-10-15T00:00:00Z', tools: [] }
    payload = probeTask
  }
  else if (path === '/agent/tool-traces') {
    taskTraceRequests += 1
    payload = { items: which === 'A' ? [{ id: 'private-agent-marker', provider: 'demo', status: 'published', stop_reason: 'private-agent-marker', created_at: '2026-10-15T00:00:00Z', tools: [] }] : probeTask ? [{ ...probeTask, status: probeTaskStatus }] : [] }
  }
  else throw new Error(`Unexpected API request: ${request.method} ${path}`)
  await fulfill(requestId, payload)
}

try {
  let port
  await waitFor(async () => {
    try { port = (await readFile(resolve(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; return Boolean(port) } catch { return false }
  }, 'browser debugging endpoint')
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  socket = new WebSocket(targets.find((target) => target.type === 'page').webSocketDebuggerUrl)
  await new Promise((ok, fail) => { socket.addEventListener('open', ok, { once: true }); socket.addEventListener('error', fail, { once: true }) })
  command = (method, params = {}) => new Promise((ok, fail) => {
    const id = ++sequence
    pending.set(id, { ok, fail })
    socket.send(JSON.stringify({ id, method, params }))
  })
  socket.addEventListener('message', ({ data }) => {
    const response = JSON.parse(String(data))
    if (response.id) {
      const waiter = pending.get(response.id)
      if (!waiter) return
      pending.delete(response.id)
      if (response.error) waiter.fail(new Error(response.error.message))
      else waiter.ok(response.result)
    } else if (response.method === 'Fetch.requestPaused') route(response.params).catch((error) => { eventError = error })
  })
  await command('Runtime.enable')
  await command('Page.enable')
  await command('Fetch.enable', { patterns: [{ urlPattern: '*/api/v1/*', requestStage: 'Request' }] })
  await command('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
  const evaluate = async (expression) => {
    const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text)
    return result.result.value
  }
  const clickNav = (label) => evaluate(`Array.from(document.querySelectorAll('.nav-item')).find(node => node.textContent.includes(${JSON.stringify(label)})).click()`)
  await command('Page.navigate', { url: baseUrl })
  await waitFor(() => evaluate(`Boolean(document.querySelector('.welcome-button:not(:disabled)'))`), 'welcome login')
  await evaluate(`document.querySelector('.welcome-button').click()`)
  await waitFor(() => evaluate(`document.querySelector('.user-label strong')?.textContent === '浏览器甲' && !document.querySelector('[aria-label="刷新工作台"]')?.disabled`), 'first account')
  await clickNav('订阅偏好')
  assert.equal(await evaluate(`document.querySelector('#keywords').value`), 'FIRST_PRIVATE_KEYWORD')

  // Leave a first-account refresh pending, switch users, then deliver its old response.
  holdNextSubscription = true
  await evaluate(`document.querySelector('[aria-label="刷新工作台"]').click()`)
  await waitFor(() => Boolean(heldRequest), 'held first-account request')
  await evaluate(`document.querySelector('[aria-label="退出登录"]').click()`)
  await waitFor(() => evaluate(`Boolean(document.querySelector('.welcome-button:not(:disabled)')) && !sessionStorage.getItem('ai-news-session')`), 'logout clears busy state and session')
  await evaluate(`document.querySelector('.welcome-button').click()`)
  await waitFor(() => evaluate(`document.querySelector('.user-label strong')?.textContent === '浏览器乙' && !document.querySelector('[aria-label="刷新工作台"]')?.disabled`), 'second account')
  await clickNav('订阅偏好')
  assert.equal(await evaluate(`document.querySelector('#keywords').value`), '')
  assert.equal(await evaluate(`document.querySelectorAll('.topic-option.selected').length`), 0)
  assert.equal(await evaluate(`document.querySelector('input[type="time"]').value`), '08:00')
  await fulfill(heldRequest.requestId, heldRequest.payload)
  heldRequest = null
  await delay(300)
  assert.equal(await evaluate(`document.querySelector('.user-label strong').textContent`), '浏览器乙')
  assert.equal(await evaluate(`document.querySelector('#keywords').value`), '')

  await clickNav('历史简报')
  await waitFor(() => evaluate(`document.querySelectorAll('.history-row').length === 30 && !document.querySelector('.history-filters button').disabled`), 'first history page')
  // Editing an unapplied filter must not change the query bound to the next-page cursor.
  await evaluate(`document.querySelector('.history-search input').value = 'NOT_SUBMITTED'; document.querySelector('.history-search input').dispatchEvent(new Event('input', { bubbles: true })); document.querySelector('.history-pagination button').click()`)
  await waitFor(() => evaluate(`document.querySelectorAll('.history-row').length === 35`), 'history next page')
  await evaluate(`document.querySelector('.history-search input').value = 'target'; document.querySelector('.history-search input').dispatchEvent(new Event('input', { bubbles: true })); document.querySelector('.history-filters').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))`)
  await waitFor(() => evaluate(`document.querySelectorAll('.history-row').length === 1 && !document.querySelector('.history-filters button').disabled`), 'history title search')
  assert.equal(await evaluate(`document.querySelector('.history-row h3').textContent`), 'History target 34')
  await evaluate(`document.querySelector('.history-search input').value = ''; document.querySelector('.history-search input').dispatchEvent(new Event('input', { bubbles: true })); document.querySelector('.history-bookmarked input').checked = true; document.querySelector('.history-bookmarked input').dispatchEvent(new Event('change', { bubbles: true })); document.querySelector('.history-filters select').value = 'read'; document.querySelector('.history-filters select').dispatchEvent(new Event('change', { bubbles: true })); document.querySelector('.history-filters').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))`)
  await waitFor(() => evaluate(`document.querySelectorAll('.history-row').length === 0 && !document.querySelector('.history-filters button').disabled`), 'bookmarked and read filters')
  await evaluate(`document.querySelector('.history-filters select').value = 'unread'; document.querySelector('.history-filters select').dispatchEvent(new Event('change', { bubbles: true })); document.querySelector('.history-filters').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))`)
  await waitFor(() => evaluate(`document.querySelectorAll('.history-row').length === 1 && !document.querySelector('.history-filters button').disabled`), 'bookmarked and unread filters')
  assert.equal(await evaluate(`document.querySelector('.history-row h3').textContent`), 'History target 34')
  await clickNav('Agent 记录')
  assert.equal(await evaluate(`document.body.textContent.includes('还没有 Agent 运行记录') && !document.body.textContent.includes('private-agent-marker')`), true)
  assert.equal(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), true)
  await clickNav('今日简报')
  await evaluate(`document.querySelector('.page-heading > .button-primary').click()`)
  await waitFor(() => evaluate(`document.querySelector('.task-progress')?.textContent.includes('排队中') && document.querySelector('.page-heading > .button-primary')?.disabled`), 'queued task disables duplicate preview')
  assert.equal(taskCreates, 1)
  await command('Page.reload', { ignoreCache: true })
  await waitFor(() => evaluate(`document.querySelector('.task-progress')?.textContent.includes('排队中') && document.querySelector('.user-label strong')?.textContent === '浏览器乙'`), 'reload resumes active task')
  assert.equal(taskCreates, 1)
  probeTaskStatus = 'retry_wait'
  await waitFor(() => evaluate(`document.querySelector('.task-progress')?.textContent.includes('等待重试')`), 'retry waiting is nonterminal')
  probeTaskStatus = 'published'
  await waitFor(() => evaluate(`!document.querySelector('.task-progress') && document.querySelector('.hero-copy h2')?.textContent === 'Auto updated briefing'`), 'task completion automatically refreshes briefing')
  assert.equal(taskCreates, 1)
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true })
  assert.equal(await evaluate(`Boolean(document.querySelector('.sidebar-bottom > .nav-item')?.offsetParent && document.querySelector('[aria-label="退出登录"]')?.offsetParent)`), true)
  await clickNav('账户设置')
  assert.equal(await evaluate(`Boolean(document.querySelector('.account-card'))`), true)
  await clickNav('历史简报')
  await waitFor(() => evaluate(`!document.querySelector('.history-filters button').disabled`), 'mobile history')
  assert.equal(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), true)
  await clickNav('订阅偏好')
  assert.equal(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), true)
  const version = await command('Browser.getVersion')
  const image = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false })
  const screenshot = resolve(outputRoot, `${version.product.replace(/[^a-z0-9.-]/gi, '-')}-session.png`)
  await writeFile(screenshot, Buffer.from(image.data, 'base64'))
  await clickNav('今日简报')
  await evaluate(`document.querySelector('.page-heading > .button-primary').click()`)
  await waitFor(() => evaluate(`Boolean(document.querySelector('.task-progress'))`), 'second active task before logout')
  await evaluate(`document.querySelector('[aria-label="退出登录"]').click()`)
  await waitFor(() => evaluate(`Boolean(document.querySelector('.welcome-button:not(:disabled)')) && !sessionStorage.getItem('ai-news-session')`), 'mobile logout')
  const requestsAtLogout = taskTraceRequests
  await delay(5500)
  assert.equal(taskTraceRequests, requestsAtLogout, 'Logout must stop task polling')
  console.log(JSON.stringify({ browser: version.product, accountSwitch: 'passed', lateResponse: 'ignored', stalePreferences: 'cleared', staleTraces: 'cleared', historyPaginationAndFilters: 'passed', taskReloadRetryAndCompletion: 'passed', logoutStopsTaskPolling: true, mobileAccountAndLogout: 'passed', desktopAnd390pxOverflow: false, screenshot, mockedApi: true }))
} finally {
  if (command) {
    try { await Promise.race([command('Browser.close'), delay(1000)]) } catch { /* Browser may close its socket first. */ }
  }
  if (socket) socket.close()
  if (browser.exitCode === null) browser.kill()
  await delay(500)
  if (!resolve(profile).startsWith(profileRoot + sep)) throw new Error('Profile cleanup path escaped its root')
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 })
}
