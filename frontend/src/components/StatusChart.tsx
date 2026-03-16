import { Card } from 'antd'
import ReactECharts from 'echarts-for-react'
import dayjs from 'dayjs'

interface StatusChartProps {
  data: Array<{ time: string; lag: number }>
  title?: string
}

function StatusChart({ data, title = '延迟趋势' }: StatusChartProps) {
  const option = {
    title: {
      text: title,
      left: 'center',
    },
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        if (!params || params.length === 0) return ''
        const param = params[0]
        const time = dayjs(param.name).format('HH:mm:ss')
        return `${time}<br/>延迟: ${param.value}s`
      },
    },
    xAxis: {
      type: 'category',
      data: data.map((d) => dayjs(d.time).format('HH:mm:ss')),
      boundaryGap: false,
    },
    yAxis: {
      type: 'value',
      name: '延迟(秒)',
      min: 0,
    },
    series: [
      {
        name: '延迟',
        type: 'line',
        smooth: true,
        data: data.map((d) => d.lag),
        areaStyle: {
          opacity: 0.3,
        },
        lineStyle: {
          color: '#52c41a',
        },
        itemStyle: {
          color: '#52c41a',
        },
      },
    ],
    grid: {
      left: '50px',
      right: '20px',
      bottom: '40px',
      top: '60px',
    },
  }

  return (
    <Card title={title}>
      <ReactECharts option={option} style={{ height: '300px' }} />
    </Card>
  )
}

export default StatusChart
