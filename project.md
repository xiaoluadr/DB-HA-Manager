DB-HA-Manager


# 项目名称：跨数据库高可用管理平台（DB-HA-Manager）
##分段一：项目概述与核心功能
## 1. 项目概述
开发一个统一的 Web 管理平台，用于管理多种主流数据库（Oracle、MySQL、SQL Server、PostgreSQL）的高可用架构。  
**当前阶段**：聚焦 Oracle 数据库的 **Data Guard 主备** 管理，实现搭建、日常管理、切换演练、应急接管四大功能。  
**实现方式**：完全基于原生 Oracle 工具（SQL\*Plus、RMAN）和操作系统命令，**不依赖 Data Guard Broker**，所有操作均通过 SSH 远程执行脚本并解析结果，确保与任何 Oracle 环境兼容。  
**未来扩展**：逐步支持 MySQL 主从复制、SQL Server Always On、PG 流复制，以及分片集群等高可用模式。  
**用户交互**：提供友好的 Web 前端（React）和 CLI 命令行两种方式，满足不同运维习惯。

## 2. 核心功能（Oracle MVP 范围）
- **搭建主备**：自动配置 Oracle Data Guard 物理备库，包括环境检查、主库配置、备库克隆、DG 配置等（不使用 Broker）。
- **日常管理**：
  - 监控主备状态（角色、同步延迟、保护模式、归档 gap）
  - 手动同步/修复（如强制切换日志、恢复备库）
  - 清理归档日志（通过 RMAN 或 OS 命令）
  - 备份主库（集成 RMAN）
- **切换演练**：
  - **Switchover**：正常切换，支持真实执行和演练模式（仅检查可行性）
  - **Failover**：应急切换，支持演练模式（模拟故障，不实际激活备库）
- **应急接管**：
  - 一键 Failover（真实故障场景）
  - 接管后原主库修复与重搭引导
##分段二：技术栈与系统架构

## 3. 技术栈

### 3.1 后端
| 组件 | 选型 | 说明 |
|------|------|------|
| 编程语言 | Python 3.9+ | 跨平台、生态丰富 |
| Web 框架 | FastAPI | 高性能、自动生成 OpenAPI 文档，便于前后端协作 |
| 数据库驱动 | `python-oracledb` | Oracle 官方驱动（可选，主要用于轻量查询） |
| 远程执行 | `paramiko` / `asyncssh` | SSH 执行命令、文件传输 |
| 配置管理 | Pydantic + YAML | 配置解析与验证，敏感信息加密（`cryptography`） |
| 任务队列 | 可选 Celery / 简单后台任务 | 处理长时间任务（搭建、切换），前端轮询进度 |
| 日志 | `loguru` | 结构化日志，便于前端展示 |

### 3.2 前端
| 组件 | 选型 | 说明 |
|------|------|------|
| 框架 | React 18 + TypeScript | 类型安全、组件化开发 |
| UI 库 | Ant Design 5.x | 丰富后台组件，符合企业级风格 |
| 状态管理 | Zustand 或 Redux Toolkit | 轻量、易于集成 |
| 路由 | React Router v6 | 页面路由 |
| HTTP 客户端 | axios | 封装请求/响应拦截器 |
| 实时通信 | WebSocket | 用于推送主备状态变更（可选） |
| 图表 | ECharts | 展示延迟趋势、切换历史 |
| 构建工具 | Vite | 快速启动和热更新 |

### 3.3 部署
- 前后端分离部署，可打包为 Docker 镜像
- 提供 `docker-compose.yml` 一键启动（前端 + 后端 + 可选 Redis）

## 4. 系统架构

### 4.1 整体架构图
```plaintext
┌─────────────────────────────────────────────────────────────┐
│                        浏览器 (React)                        │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP/WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      后端 API (FastAPI)                      │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                  核心业务逻辑层                         │  │
│  │  ┌───────────────────────────────────────────────┐   │  │
│  │  │            DatabaseHADriver 接口               │   │  │
│  │  ├───────────────────────────────────────────────┤   │  │
│  │  │ OracleDataGuardDriver  │ MySQLDriver (预留)   │   │  │
│  │  └───────────────────────────────────────────────┘   │  │
│  └───────────────────────────────────────────────────────┘  │
│  ┌───────────────────────────────────────────────────────┐  │
│  │                 公共基础设施层                         │  │
│  │  - RemoteExecutor (SSH)                               │  │
│  │  - SqlExecutor (SQL*Plus 封装)                        │  │
│  │  - ConfigManager                                       │  │
│  │  - Logger                                              │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ SSH / SQL*Plus
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     数据库服务器                             │
│  (Oracle / MySQL / ... 各自的主备节点)                       │
└─────────────────────────────────────────────────────────────┘
###4.2 核心接口设计（后端）
定义一个抽象基类 DatabaseHADriver，所有数据库高可用实现必须继承并实现以下方法：

python
from abc import ABC, abstractmethod

class DatabaseHADriver(ABC):
    @abstractmethod
    def setup(self, config: dict) -> str:
        """搭建主备，返回任务ID（异步任务）"""
        pass

    @abstractmethod
    def status(self) -> dict:
        """获取主备状态（同步）"""
        pass

    @abstractmethod
    def switchover(self, dry_run: bool = False) -> dict:
        """切换演练（dry_run=True时仅检查不执行）"""
        pass

    @abstractmethod
    def failover(self, dry_run: bool = False) -> dict:
        """应急接管"""
        pass

    @abstractmethod
    def manage(self, action: str, **kwargs) -> any:
        """其他管理操作（如清理归档、手动同步等）"""
        pass
###4.3 前端与后端交互
API 规范：遵循 RESTful 风格，使用 OpenAPI 3.0 文档（FastAPI 自动生成）。前端通过 openapi-typescript-codegen 生成 TypeScript 客户端代码。

异步任务：耗时操作（搭建、切换）采用异步模式：

后端返回 202 Accepted，包含 task_id。

前端轮询 GET /api/tasks/{task_id} 获取进度和结果，或通过 WebSocket 接收实时日志。

实时状态：通过 WebSocket 连接 /ws/status，后端推送主备状态变化（如延迟更新、角色切换）。

认证：初期可简化（内网使用），如需要可启用 JWT 认证。



##分段三：后端功能模块详解（Oracle）

## 5. 后端功能模块详解（Oracle）

### 5.1 搭建主备（`setup`）
- **输入**：主库连接信息、备库主机信息、数据文件路径、备库 Oracle 环境变量等。
- **步骤**（完全基于 SSH + SQL\*Plus/RMAN）：
  1. 检查主库：归档模式、force logging、监听状态。
  2. 配置主库参数：设置 Data Guard 相关初始化参数。
  3. 配置网络：生成并分发 `tnsnames.ora`、`listener.ora`，启动监听。
  4. 备库准备：检查磁盘空间、创建目录。
  5. 主库备份并传输：使用 RMAN 备份数据文件，通过 SCP 传输到备库。
  6. 备库恢复：使用 RMAN 恢复，启动备库到 mount 状态并启用实时应用。
  7. 验证同步状态。
- **输出**：任务日志，最终成功/失败状态。

### 5.2 日常管理（`manage`）
- **状态监控**（`status`）：查询主备库角色、延迟、gap 等，返回结构化数据。
- **手动同步**：强制切换日志、注册缺失归档。
- **清理归档**：RMAN 或 OS 命令删除已应用归档。
- **备份主库**：调用预定义 RMAN 脚本。

### 5.3 切换演练（`switchover` / `failover`）
- **Switchover 演练模式**：检查切换条件（主备库状态、gap），输出报告。
- **Switchover 真实模式**：执行原生切换 SQL，等待完成，验证新主备。
- **Failover 演练模式**：检查备库是否可用，输出演练报告。
- **Failover 真实模式**：激活备库为主库，完成后引导原主库修复。
##分段四：前端详细设计

## 6. 前端设计

### 6.1 页面/组件规划
| 页面/组件 | 功能描述 |
|-----------|----------|
| **登录页** | 简单的认证（可选，初期可省略） |
| **仪表盘** | 卡片展示所有受管数据库集群概览（角色、延迟、保护模式）；ECharts 展示延迟趋势；最新操作记录列表。 |
| **集群详情页** | 点击某个集群后进入，展示主备拓扑图（主库、备库节点），各节点详细状态、参数；提供操作按钮（搭建、切换演练、应急接管、管理）。 |
| **搭建向导** | 分步表单，输入主库信息、备库信息、路径等，提交后跳转至任务详情页，实时显示日志输出。 |
| **切换演练页** | 对指定集群执行切换（带 dry-run 模式），显示检查报告和结果。 |
| **应急接管页** | 紧急 Failover 按钮，需二次确认，执行后展示进度。 |
| **任务历史页** | 列出所有执行过的任务（搭建、切换等），可查看详细日志。 |
| **配置管理页** | 编辑数据库连接信息、SSH 密钥等（需加密存储）。 |

### 6.2 前端与后端 API 对应关系（示例）
| 前端操作 | 后端 API 端点 | 方法 | 说明 |
|----------|---------------|------|------|
| 获取所有集群概览 | `GET /api/clusters` | - | 返回集群列表及简要状态 |
| 获取指定集群状态 | `GET /api/clusters/{cluster_id}/status` | - | 返回详细主备状态 |
| 发起搭建 | `POST /api/clusters/{cluster_id}/setup` | 异步 | 返回 task_id |
| 执行切换 | `POST /api/clusters/{cluster_id}/switchover` | 同步/异步 | 带 `dry_run` 参数 |
| 执行接管 | `POST /api/clusters/{cluster_id}/failover` | 同步/异步 | 带 `dry_run` 参数 |
| 获取任务进度 | `GET /api/tasks/{task_id}` | - | 返回进度和日志 |
| WebSocket 状态推送 | `WS /ws/clusters/{cluster_id}` | - | 实时推送状态变化 |

### 6.3 前端项目结构
```plaintext
frontend/
├── public/                  # 静态资源
├── src/
│   ├── api/                 # 自动生成的 API 客户端
│   ├── assets/              # 图片、样式等
│   ├── components/          # 复用组件
│   │   ├── ClusterCard.tsx
│   │   ├── StatusChart.tsx
│   │   └── ...
│   ├── pages/               # 页面
│   │   ├── Dashboard.tsx
│   │   ├── ClusterDetail.tsx
│   │   ├── SetupWizard.tsx
│   │   ├── TaskHistory.tsx
│   │   └── ...
│   ├── store/               # Zustand 状态管理
│   ├── hooks/               # 自定义 hooks
│   ├── utils/               # 工具函数（如格式化时间）
│   ├── App.tsx
│   ├── main.tsx
│   └── vite-env.d.ts
├── index.html
├── package.json
├── tsconfig.json
└── vite.config.ts


##分段五：开发计划与代码结构
```markdown
## 7. 开发计划（分阶段）

### 阶段一：基础框架与 Oracle 搭建 + 状态监控
- **后端**：
  - 搭建 FastAPI 项目，实现健康检查接口。
  - 实现 `RemoteExecutor`（SSH）和 `SqlExecutor`。
  - 实现 `OracleDataGuardDriver.setup`（仅框架，关键步骤输出日志）。
  - 实现 `OracleDataGuardDriver.status`（查询并返回 JSON）。
- **前端**：
  - 初始化 React + TypeScript + Ant Design 项目。
  - 配置路由、axios 封装。
  - 实现仪表盘页面，调用 `GET /api/clusters/status` 展示数据。
- **交付物**：可展示主备状态的 Web 界面，支持通过 CLI 触发搭建（未完整实现）。

### 阶段二：搭建向导 + 异步任务
- **后端**：
  - 完善 `setup` 方法，实现完整搭建流程（环境检查、参数配置、备份传输、恢复）。
  - 引入异步任务队列（简单内存队列或 Celery），返回 task_id。
  - 实现任务进度查询接口。
- **前端**：
  - 实现搭建向导表单，提交后跳转至任务详情页。
  - 任务详情页轮询进度，实时展示日志。
- **交付物**：可通过 Web 界面完整搭建 Oracle Data Guard 主备。

### 阶段三：切换演练与应急接管
- **后端**：
  - 实现 `switchover` 和 `failover` 方法，支持 dry-run 模式。
  - 完善检查逻辑和错误处理。
- **前端**：
  - 在集群详情页增加切换/接管操作按钮。
  - 实现切换演练报告展示弹窗。
  - 应急接管二次确认对话框。
- **交付物**：支持切换演练和真实 Failover。

### 阶段四：日常管理与历史记录
- **后端**：
  - 实现 `manage` 方法（归档清理、手动同步、备份）。
  - 记录所有操作到数据库（SQLite/PostgreSQL）。
- **前端**：
  - 增加“任务历史”页面，展示历史操作。
  - 在集群详情页增加“管理”下拉菜单，执行清理等操作。
- **交付物**：完整的 Oracle 主备管理功能。

### 阶段五：扩展与优化
- **后端**：抽象接口，添加 MySQL 驱动（验证扩展性）。
- **前端**：支持多数据库类型切换，拓扑图可视化优化。
- **交付物**：可扩展的架构，支持第二种数据库。

## 8. 代码仓库结构（Monorepo）
```plaintext
db_ha_manager/
├── backend/
│   ├── app/
│   │   ├── api/              # FastAPI 路由
│   │   ├── core/             # 公共基础设施（executor, config, logger）
│   │   ├── drivers/          # 数据库驱动（base, oracle, mysql...）
│   │   ├── models/           # Pydantic 模型
│   │   ├── tasks/            # 后台任务处理
│   │   └── main.py           # 应用入口
│   ├── scripts/              # 辅助脚本（RMAN 模板等）
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── ... (如上结构)
│   └── Dockerfile
├── docker-compose.yml        # 一键启动后端、前端、数据库（可选）
└── README.md


##分段六：风险与下一步工作**
```markdown
## 9. 风险与注意事项
- **Oracle 版本兼容性**：测试 11gR2 至 19c 的 SQL 语法差异。
- **安全**：SSH 密钥管理，禁止明文密码；API 可添加简单 IP 白名单。
- **错误处理**：所有步骤应有重试/回滚机制，关键操作前备份参数文件。
- **幂等性**：搭建脚本应支持重复执行而不破坏环境。

## 10. 下一步工作
请 Claude 根据以上设计，首先构建项目骨架：
1. 生成 `backend/` 基础代码：FastAPI 应用、SSH 执行器、Oracle 驱动框架。
2. 生成 `frontend/` 基础代码：React 项目、路由、API 客户端生成配置。
3. 编写第一个 API `/api/status` 并在前端展示简单数据。

确保代码模块化、注释清晰，为后续扩展打好基础。
