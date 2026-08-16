import request from './request'

export const recommendationApi = {
  /** 最新一期 Top 10 + 3 备选 */
  latest() {
    return request.get('/recommendation/latest')
  },
  /** 按日期查询 */
  history(date) {
    return request.get('/recommendation/history', { params: { date } })
  },
}
