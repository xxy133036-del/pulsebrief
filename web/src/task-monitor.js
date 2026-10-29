export function createTaskMonitor({ load, update, failed, hasActiveTasks, setTimer = setTimeout, clearTimer = clearTimeout, intervalMs = 5000 }) {
  let generation = 0
  let timer = null
  let failures = 0

  function stop() {
    generation += 1
    if (timer !== null) clearTimer(timer)
    timer = null
  }

  function schedule(run, wait) {
    timer = setTimer(() => tick(run), wait)
  }

  async function tick(run) {
    timer = null
    try {
      const result = await load()
      if (run !== generation) return
      await update(result, () => run === generation)
      if (run !== generation) return
      failures = 0
    } catch (error) {
      if (run !== generation) return
      failures += 1
      failed(error)
    }
    if (run === generation && hasActiveTasks()) {
      schedule(run, Math.min(intervalMs * 2 ** Math.min(failures, 3), 30000))
    }
  }

  function start(immediate = false) {
    stop()
    failures = 0
    schedule(generation, immediate ? 0 : intervalMs)
  }

  return { start, stop }
}
