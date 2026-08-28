import request from './request'

/** 量化交易 API（自选池 / 用户模拟账户 / 手动买卖） */
export const quantApi = {
  /** 自选池列表（可选状态过滤: active / hold / removed / stop_holding） */
  pool(status) {
    return request.get('/quant/pool', { params: status ? { status_filter: status } : {} })
  },
  /** 调整自选池状态（买入后 bought / 移出 removed） */
  setPoolStatus(symbol, status) {
    return request.put(`/quant/pool/${encodeURIComponent(symbol)}/status`, { status })
  },
  /** 我的模拟账户快照（首次访问自动开户） */
  myAccount() {
    return request.get('/quant/me/account')
  },
  /** 我的持仓 */
  myHoldings() {
    return request.get('/quant/me/holdings')
  },
  /** 我的交易流水 */
  myTrades(params = {}) {
    return request.get('/quant/me/trades', { params })
  },
  /** 我的交易统计（胜率/累计盈亏） */
  myStats() {
    return request.get('/quant/me/stats')
  },
  /** 手动买入（quantity 为股数, 1 手 = 100 股; price 缺省取实时行情） */
  buy(payload) {
    return request.post('/quant/me/buy', payload)
  },
  /** 手动卖出（portion 为比例, 1 = 清仓; 自动记录该笔盈亏） */
  sell(payload) {
    return request.post('/quant/me/sell', payload)
  },

  // ---- 量化进化审核（策略规则 + 进化记录, 人工确认后才生效） ----
  /** 策略规则库 */
  rules(enabledOnly = false) {
    return request.get('/quant/rules', { params: enabledOnly ? { enabled_only: true } : {} })
  },
  /** 规则单测用例 */
  ruleCases(ruleId) {
    return request.get(`/quant/rules/${encodeURIComponent(ruleId)}/cases`)
  },
  /** 运行规则单测（上线前置门槛） */
  testRule(ruleId) {
    return request.post(`/quant/rules/${encodeURIComponent(ruleId)}/test`)
  },
  /** 直接更新规则（仅 enabled 字段, 紧急禁用通道） */
  updateRule(ruleId, payload) {
    return request.put(`/quant/rules/${encodeURIComponent(ruleId)}`, payload)
  },
  /** 批准候选规则（先跑单测, 全过才启用） */
  approveRule(ruleId, note = '') {
    return request.post(`/quant/rules/${encodeURIComponent(ruleId)}/approve`, { note })
  },
  /** 拒绝候选规则 */
  rejectRule(ruleId, note = '') {
    return request.post(`/quant/rules/${encodeURIComponent(ruleId)}/reject`, { note })
  },
  /** 进化记录（可按状态过滤: pending_review / approved / rejected / oos_rejected） */
  evolutions(statusFilter = '', limit = 50) {
    const params = { limit }
    if (statusFilter) params.status_filter = statusFilter
    return request.get('/quant/evolutions', { params })
  },
  /** 批准进化候选（规则转正式启用） */
  approveEvolution(id, note = '') {
    return request.post(`/quant/evolutions/${id}/approve`, { note })
  },
  /** 拒绝进化候选 */
  rejectEvolution(id, note = '') {
    return request.post(`/quant/evolutions/${id}/reject`, { note })
  },
  /** 触发止损参数进化（后台执行, 需传标的代码列表） */
  evolve(payload = {}) {
    return request.post('/quant/evolve', payload)
  },
}
