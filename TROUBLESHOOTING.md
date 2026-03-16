# 故障排查指南

本文档记录在开发过程中遇到的问题和解决方案。

## 常见问题速查表

| 问题类型 | 速查关键词 |
|----------|-------------|
| npm 错误 | `package.json`, `ENOENT`, 目录切换 |
| JSX 语法 | `中文字符`, `冒号`, `Unterminated`, `缺少闭合标签` |
| TypeScript | 类型错误, 属性不存在 |
| Ant Design | 组件属性, 样式不生效 |
| 样式 | 深色主题, 变量, 优先级 |
| SSH 连接 | `paramiko`, `连接超时`, `认证失败` |
| API 调用 | `404`, `500`, `CORS`, 跨域 |

## 常见问题

### 1. npm 运行时找不到 package.json

**错误信息：**
```
npm error code ENOENT
npm error path /home/lujingwei/DB-HA-Manager/backend/package.json
```

**原因：**
当前工作目录不是 frontend 目录，npm 找到了错误的 package.json。

**解决方案：**
- 执行命令前确保当前目录正确
- 使用 `cd` 明确切换到前端目录
- 或使用绝对路径运行命令

```bash
# 错误（当前目录错误）
npm run dev

# 正确（显式切换目录）
cd /home/lujingwei/DB-HA-Manager/frontend && npm run dev
```

### 2. JSX 中使用中文冒号导致语法错误

**错误信息：**
```
Unterminated string constant. (342:91)
```

**原因：**
在 JSX 表达式中使用了中文冒号 `：` 而不是英文冒号 `:`

**错误代码示例：**
```tsx
// 错误
<Alert message="错误信息：内容" />
// 或
<div>更新时间: {formatDateTime(status?.timestamp)}</div>
```

**解决方案：**
在 JSX 中使用英文冒号，中文内容应该用字符串包裹或使用 Text 组件

```tsx
// 正确
<Alert message="错误信息" description={content} />
<div>
  更新时间: <Text>{formatDateTime(status?.timestamp)}</Text>
</div>
```

### 3. TypeScript 类型/组件属性错误

**错误信息：**
```
Unexpected token, expected "}" (192:22)
```

**原因：**
使用了不存在的组件属性或拼写错误

**错误代码示例：**
```tsx
// 错误 - column 属性传数字
<Descriptions column={2} size="small" bordered>
```

Descriptions 组件的 column 属性值应该是数字或特定的对象，直接传数字会导致类型错误。

**解决方案：**
检查组件的 API 文档，使用正确的属性

```tsx
// 正确 - column 传对象
<Descriptions size="small" bordered>
<Descriptions
  size="small"
  bordered
  items={[
    {
      key: '1',
      children: <Descriptions.Item label="主机">...</Descriptions.Item>
    }
  ]}
/>
```

### 4. Ant Design 组件样式不生效

**现象：**
深色主题样式没有正确应用到组件

**原因：**
1. 缺少 `bordered={false}` 属性
2. CSS 优先级问题

**解决方案：**
在组件上显式添加样式属性

```tsx
<Card
  bordered={false}
  style={{ background: 'var(--bg2)', border: '1px solid var(--border)', borderRadius: '10px' }}
/>
```

### 5. 中文字符在 JSX 属性中的问题

**错误信息：**
```
Unterminated string constant
```

**原因：**
JSX 属性值中包含中文字符时可能需要转义

**解决方案：**
使用 JavaScript 表达式或 Text 组件包裹

```tsx
// 错误
<Tag color="red" text="操作：失败">

// 正确 - 使用 Text 组件
<Tag color="red">
  <Text>操作：失败</Text>
</Tag>

// 或使用变量
const message = '操作：失败'
<Tag color="red">{message}</Tag>
```

### 6. JSX 表达式中缺少闭合标签（批量问题 2026-03-09）

**错误信息：**
```
Unterminated string constant. (348:114)
```

**问题描述：**
在多个文件中，Text 组件未正确闭合导致 `Unterminated string constant` 错误

**常见原因：**
- 复制粘贴代码时遗漏闭合标签
- 在 JSX 属性中使用了中文标点
- IDE 自动格式化时遗漏
- 手动编辑代码时未检查标签匹配
- **批量修复记录：**
  - `TaskHistory.tsx` 第224行：Text 组件未闭合
  - `ClusterDetail.tsx` 第348行：Text 组件未闭合
  - `ClusterDetail.tsx` 第351行：Text 组件未闭合
  - `ClusterDetail.tsx` 第358行：Text 组件未闭合

**检查方法：**
1. 使用 VSCode 的括号颜色匹配功能
2. 开启 "Bracket Pair Colorizer" 插件
3. 使用 `Alt` + `.` 闭合标签
4. 在保存前全屏检查文件末尾是否有未闭合的标签
5. 使用 IDE 的自动格式化功能（`Shift` + `Alt` + `F`）

**解决方案示例：**
```tsx
// 错误 - 缺少闭合
<Text>内容
// 正确
<Text>内容</Text>
```

**错误信息：**
```
Unterminated string
```

**原因：**
JSX 属性值中包含中文字符时可能需要转义

**解决方案：**
使用 JavaScript 表达式或 Text 组件包裹

```tsx
// 错误
<Tag color="red" text="操作：失败">

// 正确 - 使用 Text 组件
<Tag color="red">
  <Text>操作：失败</Text>
</Tag>

// 或使用变量
const message = '操作：失败'
<Tag color="red">{message}</Tag>
```

4. **JSX 表达式语法错误**（2026-03-09 批量修复）
- **问题描述**: 在多个文件中，Text 组件未正确闭合导致 `Unterminated string constant` 错误
- **常见原因**:
  - 复制粘贴代码时遗漏闭合标签
  - 在 JSX 属性中使用了中文标点
  - IDE 自动格式化时遗漏
  - 手动编辑代码时未检查标签匹配
- **检查方法**:
  1. 使用 VSCode 的括号颜色匹配功能
  2. 开启 "Bracket Pair Colorizer" 插件
  3. 使用 `Alt` + `.` 闭合标签
  4. 在保存前全屏检查文件末尾是否有未闭合的标签
- **批量修复记录**:
  - `TaskHistory.tsx` 第224行：Text 组件未闭合
  - `ClusterDetail.tsx` 第348、351、358行：Text 组件未闭合
  - `SetupWizard.tsx`: 检查无误

## 调试技巧

### 1. 检查 Vite 错误输出

Vite 的错误输出会显示：
- 文件路径
- 行号和列号
- 具体的错误信息

使用以下命令查看完整错误：
```bash
# 查看后台任务输出
cat /tmp/claude-1000/-home-lujingwei-DB-HA-Manager/tasks/[task-id].output

# 或查看 Vite 进程输出
lsof -i :3000  # 查看哪个进程在监听端口
```

### 2. 使用 TypeScript 类型检查

在编写代码时，VSCode 或其他 IDE 会实时显示类型错误。

运行 TypeScript 编译检查：
```bash
npx tsc --noEmit
```

### 3. 浏览器开发者工具

打开浏览器开发者工具（F12）查看：
- Console 错误
- Network 请求错误
- React DevTools 组件状态

## 前端开发命令

```bash
# 切换到前端目录
cd /home/lujingwei/DB-HA-Manager/frontend

# 启动开发服务器
npm run dev

# 安装依赖
npm install

# 构建生产版本
npm run build

# 代码检查
npm run lint
```

## 后端开发命令

```bash
# 切换到后端目录
cd /home/lujingwei/DB-HA-Manager/backend

# 启动开发服务器
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 安装依赖
pip install -r requirements.txt
```

## 服务端口

- 前端: http://localhost:3000 或 http://192.168.123.129:3000
- 后端: http://localhost:8000
- API 文档: http://localhost:8000/api/docs
   ```
4. **JSX 表达式语法错误**（2026-03-09 批量修复）
   - **问题描述**: 在多个人文件中，Text 组件未正确闭合导致 `Unterminated string constant` 错误
   - **批量修复记录**:
     - `TaskHistory.tsx` 第224行：Text 组件未闭合
     - `ClusterDetail.tsx` 第348、351、358行：Text 组件未闭合
   - `SetupWizard.tsx`: 检查无误

### 5. APIResponse 嵌套访问问题（2026-03-09 批量修复）

**错误信息：**
```
Type 'APIResponse<ClusterStatus>' is not assignable to parameter of type 'SetStateAction<ClusterStatus | null>'.
```

**原因：**
后端 API 返回的是嵌套结构 `APIResponse<T>`，其中数据在 `data` 字段中，前端访问时未正确解包。

**解决方案：**
使用可选链访问 `response.data?.data` 而非 `response.data`

```tsx
// 错误
const response = await clusterApi.getClusterStatus(clusterId)
setStatus(response.data)

// 正确
const response = await clusterApi.getClusterStatus(clusterId)
setStatus(response.data?.data)
```

**修复记录：**
- `src/hooks/useClusterStatus.ts` - 修复状态解包
- `src/pages/Dashboard.tsx` - 修复集群列表和状态解包
- `src/pages/TaskHistory.tsx` - 修复任务列表解包

### 6. TypeScript 类型导出问题（2026-03-09）

**错误信息：**
```
error TS2305: Module '"@/api"' has no exported member 'SetupCommand'.
```

**原因：**
在 `src/api/index.ts` 中导入了 `SetupCommand` 但未导出，导致其他模块无法引用。

**解决方案：**
在 `src/api/index.ts` 中添加类型导出：

```tsx
import type { SetupCommand, SetupCommands } from './types'

// Re-export types for convenience
export type { SetupCommand, SetupCommands } from './types'
```

### 7. dayjs fromNow 方法不存在（2026-03-09）

**错误信息：**
```
Property 'fromNow' does not exist on type 'Dayjs'.
```

**原因：**
`dayjs` 的 `fromNow` 方法在单独的插件中，需要注册后才能使用。

**解决方案：**
导入并注册 `relativeTime` 插件：

```tsx
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'

// 注册相对时间插件
dayjs.extend(relativeTime)

export function formatRelativeTime(dateStr: string | undefined): string {
  if (!dateStr) return '-'
  return dayjs(dateStr).fromNow()
}
```

**修复记录：**
- `src/utils/format.ts` - 添加 relativeTime 插件导入和注册