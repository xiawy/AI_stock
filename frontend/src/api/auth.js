import request from './request'

export const authApi = {
  register(data) {
    // { username, email, password }
    return request.post('/auth/register', data)
  },
  login(data) {
    // { username, password } → { access_token, user }
    return request.post('/auth/login', data)
  },
  logout() {
    return request.post('/auth/logout')
  },
  me() {
    return request.get('/auth/me')
  },
}
