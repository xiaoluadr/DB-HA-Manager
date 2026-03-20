import axios from 'axios'

const isNonNullObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

type FastAPIErrorResponse = {
  detail: unknown
}

const hasFastAPIDetail = (value: unknown): value is FastAPIErrorResponse =>
  isNonNullObject(value) && 'detail' in value

const extractMessage = (value: unknown): string | undefined => {
  if (!isNonNullObject(value)) {
    return undefined
  }
  const { message } = value as { message?: unknown }
  return typeof message === 'string' ? message : undefined
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
    const fallback = error.message || '请求失败'
    const isFastAPIError = hasFastAPIDetail(responseData)

    // 保持 error.response.data 不变，让业务层可以读取完整的错误结构
    // 只在需要简单错误消息时提供 fallback
    if (isFastAPIError) {
      const detail = responseData.detail
      const detailMessage = typeof detail === 'string'
        ? detail
        : extractMessage(detail)
      const message = detailMessage || fallback
      console.error('API Error:', message, responseData)
      if (typeof error === 'object' && error) {
        error.message = message
      }
    } else {
      const message = extractMessage(responseData) || fallback
      if (typeof error === 'object' && error) {
        error.message = message
      }
      console.error('API Error:', message)
    }
    return Promise.reject(error)
  }
)

export default api
