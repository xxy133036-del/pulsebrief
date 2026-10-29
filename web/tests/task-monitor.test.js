import assert from 'node:assert/strict'
import test from 'node:test'
import { createTaskMonitor } from '../src/task-monitor.js'

function fakeClock() {
  let now = 0
  let nextId = 0
  const timers = new Map()
  return {
    now: () => now,
    count: () => timers.size,
    setTimer: (callback, wait) => {
      const id = ++nextId
      timers.set(id, { at: now + wait, callback })
      return id
    },
    clearTimer: (id) => timers.delete(id),
    async advance(wait) {
      const end = now + wait
      while (timers.size) {
        const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0]
        if (timer.at > end) break
        now = timer.at
        timers.delete(id)
        await timer.callback()
      }
      now = end
    },
  }
}

test('a task completing after forty seconds still updates and then stops polling', async () => {
  const clock = fakeClock()
  let active = true
  let polls = 0
  const observed = []
  const monitor = createTaskMonitor({
    ...clock,
    load: async () => ({ status: ++polls === 10 ? 'published' : 'running' }),
    update: (task) => { observed.push([clock.now(), task.status]); active = task.status !== 'published' },
    failed: assert.fail,
    hasActiveTasks: () => active,
  })
  monitor.start()
  await clock.advance(60000)
  assert.deepEqual(observed.at(-1), [50000, 'published'])
  assert.equal(polls, 10)
  assert.equal(clock.count(), 0)
})

test('stopping while a request is pending ignores its late response', async () => {
  const clock = fakeClock()
  let resolveRequest
  const updates = []
  const monitor = createTaskMonitor({
    ...clock,
    load: () => new Promise((resolve) => { resolveRequest = resolve }),
    update: (result) => updates.push(result),
    failed: assert.fail,
    hasActiveTasks: () => true,
  })
  monitor.start(true)
  const running = clock.advance(0)
  monitor.stop()
  resolveRequest({ status: 'published' })
  await running
  assert.deepEqual(updates, [])
  assert.equal(clock.count(), 0)
})

test('temporary query errors back off and recover without declaring task failure', async () => {
  const clock = fakeClock()
  const times = []
  const errors = []
  let active = true
  const monitor = createTaskMonitor({
    ...clock,
    load: async () => { times.push(clock.now()); if (times.length < 3) throw new Error('network'); return 'published' },
    update: () => { active = false },
    failed: (error) => errors.push(error.message),
    hasActiveTasks: () => active,
  })
  monitor.start()
  await clock.advance(60000)
  assert.deepEqual(times, [5000, 15000, 35000])
  assert.deepEqual(errors, ['network', 'network'])
  assert.equal(clock.count(), 0)
})

test('an explicit refresh checks an idle account once without starting an idle loop', async () => {
  const clock = fakeClock()
  let calls = 0
  const monitor = createTaskMonitor({
    ...clock, load: async () => ++calls, update: () => {}, failed: assert.fail,
    hasActiveTasks: () => false,
  })
  monitor.start(true)
  await clock.advance(60000)
  assert.equal(calls, 1)
  assert.equal(clock.count(), 0)
})
