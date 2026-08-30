import request from './request'

export const recommendationApi = {
  /** 最新热股（自选观察池活跃股票，按置信度降序） */
  latest() {
    return request.get('/recommendation/latest')
  },
  /** 按日期查询 */
  history(date) {
    return request.get('/recommendation/history', { params: { date } })
  },
}
