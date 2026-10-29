const STOP_REASON_LABELS = {
  evidence_repair_failed: '摘要依据核验未完成，未通过校验的内容已被丢弃。',
  language_repair_failed: '简报已通过事实校验并发布；中文润色服务暂不可用，部分表达可能未完成本地化。',
  model_final_response: 'Agent 已完成本次生成。',
  model_token_budget_exceeded: '为避免本次模型用量继续增加，Agent 已停止；请缩小订阅范围后重试。',
  no_matching_candidates: '当前采集内容没有匹配这份订阅的候选。',
  subscription_paused: '订阅已暂停。',
  no_subscription_preferences: '尚未设置订阅偏好。',
  worker_error: '后台任务遇到错误，请稍后重试。',
}

export function stopReasonLabel(value) {
  if (!value) return '任务尚未结束'
  const languageRepairPrefix = 'language_repair_failed_'
  if (value.startsWith(languageRepairPrefix)) {
    const reason = value.slice(languageRepairPrefix.length)
    const reasonLabels = {
      model_auth_error: '模型认证失败',
      model_network_error: '网络连接中断',
      model_rate_limited: '模型请求受限',
      model_server_error: '模型服务异常',
      model_timeout: '模型请求超时',
      model_unavailable: '模型暂不可用',
    }
    const detail = reasonLabels[reason] || '模型请求失败'
    return `简报已通过事实校验并发布；中文润色阶段${detail}，部分表达可能未完成本地化。`
  }
  return STOP_REASON_LABELS[value] || value
}
