import assert from 'node:assert/strict'
import test from 'node:test'
import { stopReasonLabel } from '../src/task-labels.js'

test('evidence repair failure is explained without implying an unverified summary was published', () => {
  assert.equal(
    stopReasonLabel('evidence_repair_failed'),
    '摘要依据核验未完成，未通过校验的内容已被丢弃。',
  )
})

test('known task stop reasons are translated and unknown values remain inspectable', () => {
  assert.equal(
    stopReasonLabel('language_repair_failed_model_timeout'),
    '简报已通过事实校验并发布；中文润色阶段模型请求超时，部分表达可能未完成本地化。',
  )
  assert.equal(stopReasonLabel('no_matching_candidates'), '当前采集内容没有匹配这份订阅的候选。')
  assert.equal(stopReasonLabel('provider_specific_reason'), 'provider_specific_reason')
  assert.equal(stopReasonLabel(''), '任务尚未结束')
})
