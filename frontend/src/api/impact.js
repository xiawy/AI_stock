import request from './request'

export const impactApi = {
  /** 最新一期 Top 20 影响力榜 */
  latest() {
    return request.get('/impact/latest')
  },
  /** 按日期查询 */
  history(date) {
    return request.get('/impact/history', { params: { date } })
  },
  /** 单条新闻详情 */
  detail(newsId) {
    return request.get(`/impact/detail/${newsId}`)
  },
}
