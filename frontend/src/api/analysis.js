import request from './request'

export const analysisApi = {
  /** Start a new analysis run (fresh=true wipes previous checkpoints). */
  start(payload) {
    return request.post('/analysis/start', payload)
  },
  /** Resume an interrupted run from its checkpoint. */
  resumeCheckpoint(payload) {
    return request.post('/analysis/resume-checkpoint', payload)
  },
  status(taskId) {
    return request.get(`/analysis/status/${taskId}`)
  },
  result(taskId) {
    return request.get(`/analysis/result/${taskId}`)
  },
  pause(taskId) {
    return request.post(`/analysis/${taskId}/pause`)
  },
  resume(taskId) {
    return request.post(`/analysis/${taskId}/resume`)
  },
  stop(taskId) {
    return request.post(`/analysis/${taskId}/stop`)
  },
  tasks() {
    return request.get('/analysis/tasks')
  },
  incomplete() {
    return request.get('/analysis/incomplete')
  },
}
