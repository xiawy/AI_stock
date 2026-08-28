import request from './request'

/** 进化审核 API（人工审核策略草稿） */
export const evolutionApi = {
  /** 各 Agent 进化状态概览 */
  agents() {
    return request.get('/evolution/agents')
  },
  /** 触发一次复盘（后台执行，返回 job_id） */
  triggerReview(agent) {
    return request.post(`/evolution/review/${agent}`)
  },
  /** 轮询复盘任务进度 */
  reviewJob(jobId) {
    return request.get(`/evolution/review/jobs/${jobId}`)
  },
  /** 待审核草稿列表（可按 agent 过滤） */
  drafts(agent) {
    return request.get('/evolution/drafts', { params: agent ? { agent } : {} })
  },
  /** 草稿全文 */
  draft(agent, filename) {
    return request.get(`/evolution/drafts/${agent}/${encodeURIComponent(filename)}`)
  },
  /** 批准并应用草稿 */
  approveDraft(agent, filename) {
    return request.post(`/evolution/drafts/${agent}/${encodeURIComponent(filename)}/approve`)
  },
  /** 拒绝并删除草稿 */
  rejectDraft(agent, filename) {
    return request.delete(`/evolution/drafts/${agent}/${encodeURIComponent(filename)}`)
  },
  /** 最新复盘总结 */
  learning(agent) {
    return request.get(`/evolution/learnings/${agent}`)
  },
}
