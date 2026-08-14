import request from './request'

export const stocksApi = {
  /** q: 6-digit code / SH600519 / Chinese name → { code, name, label } */
  search(q) {
    return request.get('/stocks/search', { params: { q } })
  },
  quote(code) {
    return request.get(`/stocks/${code}/quote`)
  },
  kline(code, days = 120) {
    return request.get(`/stocks/${code}/kline`, { params: { days } })
  },
}

export const historyApi = {
  list() {
    return request.get('/history')
  },
  report(ticker, tradeDate) {
    return request.get(`/history/${ticker}/${tradeDate}`)
  },
  markdownUrl(ticker, tradeDate) {
    return `/api/history/${ticker}/${tradeDate}/markdown`
  },
  pdfUrl(ticker, tradeDate) {
    return `/api/history/${ticker}/${tradeDate}/pdf`
  },
}

export const watchlistApi = {
  list() {
    return request.get('/watchlist')
  },
  add(ticker, note = '') {
    return request.post('/watchlist', { ticker, note })
  },
  remove(ticker) {
    return request.delete(`/watchlist/${ticker}`)
  },
}
