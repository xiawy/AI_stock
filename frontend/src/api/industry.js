import request from './request'

export const industryApi = {
  /** 最新一期行业榜（量化选股生命周期优选前 10 行业） */
  latest() {
    return request.get('/industry/latest')
  },
  /** 按日期查询 */
  history(date) {
    return request.get('/industry/history', { params: { date } })
  },
  /** 行业关联新闻（来自最新新闻影响力快照） */
  news(rankingId) {
    return request.get(`/industry/${rankingId}/news`)
  },
}
