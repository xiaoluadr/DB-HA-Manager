import { Routes, Route } from 'react-router-dom'
import { Layout } from 'antd'

import Dashboard from '@/pages/Dashboard'
import ClusterDetail from '@/pages/ClusterDetail'
import SetupWizard from '@/pages/SetupWizard'
import SetupProgress from '@/pages/SetupProgress'
import TaskHistory from '@/pages/TaskHistory'
import Settings from '@/pages/Settings'
import AppHeader from '@/components/AppHeader'
import AppSidebar from '@/components/AppSidebar'

const { Content } = Layout

function App() {
  return (
    <Layout style={{ minHeight: '100vh' }}>
      <AppHeader />
      <Layout>
        <AppSidebar />
        <Content style={{ padding: '18px 20px' }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/clusters/:clusterId" element={<ClusterDetail />} />
            <Route path="/setup" element={<SetupWizard />} />
            <Route path="/setup/:taskId" element={<SetupProgress />} />
            <Route path="/tasks" element={<TaskHistory />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

export default App
