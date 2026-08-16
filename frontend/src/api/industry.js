import request from './request'

export const industryApi = {
  /** 最新一期行业热度榜（新闻热度 × 资金共振） */
  latest() {
    return request.get('/industry/latest')
  },
  /** 按日期查询 */
  history(date) {
    return request.get('/industry/history', { params: { date } })
  },
}
