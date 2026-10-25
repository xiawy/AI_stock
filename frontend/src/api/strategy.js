import request from './request'

/** 策略台 API（收盘后市场级研判，只读策略台自有表，不触碰订单/持仓） */
export const strategyApi = {
  /** 三段式报告（大盘总览 / 关注板块 / 交易建议） */
  desk(date, snapshotType = 'close') {
    return request.get('/strategy/desk', {
      params: { date: date || undefined, snapshot_type: snapshotType },
    })
  },
  /** 板块结构拆解明细 */
  boards(date) {
    return request.get('/strategy/boards', { params: { date: date || undefined } })
  },
  /** 高标梯队与溯源 */
  leaders(date) {
    return request.get('/strategy/leaders', { params: { date: date || undefined } })
  },
  /** 记分看板 */
  scoreboard() {
    return request.get('/strategy/scoreboard')
  },
  /** 权重回灌现状（系数 + 未出系数的原因 + 开关） */
  coef() {
    return request.get('/strategy/coef')
  },
  /** 立即重算系数（幂等） */
  recomputeCoef() {
    return request.post('/strategy/coef/recompute')
  },
  /** 手动触发一次研判（入队，不阻塞） */
  run(date, snapshotType = 'close') {
    return request.post('/strategy/run', null, {
      params: { date: date || undefined, snapshot_type: snapshotType },
    })
  },
  /** 回填到期断言（幂等） */
  verify(asof) {
    return request.post('/strategy/verify', null, { params: { asof: asof || undefined } })
  },
}
