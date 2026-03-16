import axios from 'axios'

const isNonNullObject = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

// 创建 axios 实例
const api = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 请求拦截器
api.interceptors.request.use(
  (config) => {
    // 可以在这里添加 token 等
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器
api.interceptors.response.use(
  (response) => {
    return response.data
  },
  (error) => {
    // FastAPI 错误响应结构: { detail: {...} } 或 { detail: "error string" }
    // FastAPI 成功响应结构: { success: true, data: {...}, message: "..." }
    const responseData = error.response?.data
    const isFastAPIError = isNonNullObject(responseData) && 'detail' in responseData

    // 保持 error.response.data 不变，让业务层可以读取完整的错误结构
    // 只在需要简单错误消息时提供 fallback
    if (isFastAPIError) {
      const detail = responseData.detail
      const message = typeof detail === 'string' ? detail : detail?.message || error.message || '请求失败'
      console.error('API Error:', message, responseData)
    } else {
      const message = responseData?.message || error.message || '请求失败'
      if (error && typeof error === 'object') {
        error.message = message
      }
      console.error('API Error:', message)
    }
    return Promise.reject(error)
  }
)

export default api
