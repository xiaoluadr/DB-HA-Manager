import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'

// 注册相对时间插件
dayjs.extend(relativeTime)

/**
 * 格式化日期时间
 */
export function formatDateTime(dateStr: string | undefined): string {
  if (!dateStr) return '-'
  return dayjs(dateStr).format('YYYY-MM-DD HH:mm:ss')
}

/**
 * 格式化相对时间
 */
export function formatRelativeTime(dateStr: string | undefined): string {
  if (!dateStr) return '-'
  return dayjs(dateStr).fromNow()
}

/**
 * 获取同步状态颜色
 */
export function getSyncStatusColor(status: string): string {
  const colorMap: Record<string, string> = {
    SYNCED: 'green',
    LAGGING: 'orange',
    ERROR: 'red',
    UNKNOWN: 'gray',
  }
  return colorMap[status] || 'gray'
}

/**
 * 获取同步状态文本
 */
export function getSyncStatusText(status: string): string {
  const textMap: Record<string, string> = {
    SYNCED: '已同步',
    LAGGING: '同步延迟',
    ERROR: '错误',
    UNKNOWN: '未知',
  }
  return textMap[status] || status
}

/**
 * 格式化文件大小
 */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`
}
