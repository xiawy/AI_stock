import request from './request'

/**
 * 三榜联动 API（新闻榜 / 行业榜 / 热股榜）。
 *
 * 三个榜原本各自排序、互不引用，这里只提供"把它们连起来看"的读接口：
 * 变化视图、证据链、成绩单。全部只读，写入口仍在各自的生产链路里。
 */
export const boardApi = {
  /** 行业榜变化：Δrank / 新进 / 连榜天数 */
  industryDiff(date) {
    return request.get('/board/industry/diff', { params: { date: date || undefined } })
  },
  /** 新闻榜变化：与上一份快照对比（一天 3 份，不是与昨天比） */
  impactDiff(date) {
    return request.get('/board/impact/diff', { params: { date: date || undefined } })
  },
  /** 热股榜变化：靠日快照（quant_pool_snapshot）才能算 */
  recommendationDiff(date) {
    return request.get('/board/recommendation/diff', { params: { date: date || undefined } })
  },
  /** 证据链：一个主题在三个榜上的全部证据 */
  chain(theme, date) {
    return request.get('/board/chain', {
      params: { theme, date: date || undefined },
    })
  },
  /** 同事件多源成员 */
  cluster(snapshotId, clusterId) {
    return request.get('/board/cluster', {
      params: { snapshot_id: snapshotId, cluster_id: clusterId },
    })
  },
  /** 三榜成绩单（只读；includeReturns 会拉 K 线，较慢） */
  scorecard({ days = 30, includeReturns = false } = {}) {
    return request.get('/board/scorecard', {
      params: { days, include_returns: includeReturns ? 1 : 0 },
    })
  },
}
