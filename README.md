# DB-HA-Manager

跨数据库高可用管理平台 - 用于管理 Oracle、MySQL、SQL Server、PostgreSQL 等数据库的高可用架构。

## 当前状态

本项目已完成核心功能开发，可用于生产环境：

### 后端 (Python + FastAPI)
- ✅ FastAPI 应用框架
- ✅ SSH 远程执行器 (基于 paramiko)
- ✅ SQL*Plus 执行器封装
- ✅ RMAN 执行器封装
- ✅ 配置管理器 (YAML + 加密)
- ✅ DatabaseHADriver 抽象基类
- ✅ Oracle Data Guard 驱动实现
- ✅ 完整的 Oracle Data Guard 搭建流程（7步骤）
- ✅ 任务管理器 (支持持久化、取消)
- ✅ Switchover 真实切换（支持演练模式）
- ✅ Failover 真实接管（支持演练模式）
- ✅ 归档清理和备份功能
- ✅ 资源监控 API（表空间、归档保留）
- ✅ 历史延迟数据 API（定时收集）
- ✅ 任务日志 API
- ✅ 集群 CRUD API
- ✅ API 路由 (健康检查、集群管理、任务管理)
- ✅ CORS 配置（支持环境变量）
- ✅ 加密密钥从环境变量读取

### 前端 (React + TypeScript + Ant Design)
- ✅ React 18 + TypeScript 项目
- ✅ Ant Design 5.x UI 库（深色主题）
- ✅ Vite 构建工具
- ✅ API 客户端 (axios)
- ✅ 路由配置
- ✅ 状态管理 (Zustand)
- ✅ 仪表盘页面（实时延迟历史数据）
- ✅ 集群详情页面（真实资源监控）
- ✅ 搭建向导页面
- ✅ 任务历史页面（支持筛选）
- ✅ 搜索功能
- ✅ 设置页面（环境、刷新间隔、主题）
- ✅ 任务徽章（真实数据）
- ✅ 延迟趋势图表 (ECharts)

## 快速开始

### 使用 Docker Compose（推荐）

```bash
# 克隆仓库
git clone https://github.com/your-org/DB-HA-Manager.git
cd DB-HA-Manager

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，设置你的环境变量

# 配置集群信息
cp config.yaml.example config.yaml
# 编辑 config.yaml，添加你的 Oracle 集群配置

# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f

# 访问应用
# 前端: http://localhost:8080
# 后端 API: http://localhost:8000
# API 文档: http://localhost:8000/api/docs
```

### 本地开发

#### 后端

```bash
cd backend

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -r requirements.txt

# 运行开发服务器
uvicorn app.main:app --reload --port 8000
```

#### 前端

```bash
cd frontend

# 安装依赖
npm install

# 运行开发服务器
npm run dev
```

## 项目结构

```
DB-HA-Manager/
├── backend/                 # 后端代码
│   ├── app/
│   │   ├── api/            # API 路由
│   │   ├── core/           # 基础设施 (SSH, SQL, Config, Logger)
│   │   ├── drivers/        # 数据库驱动
│   │   ├── models/         # Pydantic 模型
│   │   ├── tasks/          # 后台任务
│   │   └── main.py         # 应用入口
│   ├── scripts/            # RMAN 模板等
│   └── requirements.txt
├── frontend/               # 前端代码
│   ├── src/
│   │   ├── api/            # API 客户端
│   │   ├── components/     # React 组件
│   │   ├── pages/          # 页面
│   │   ├── store/          # 状态管理
│   │   └── ...
│   └── package.json
├── docker-compose.yml
└── README.md
```

## 配置说明

### 环境变量配置

项目支持通过环境变量进行配置，推荐在生产环境中使用环境变量而不是修改配置文件。创建 `.env` 文件并复制 `.env.example` 作为模板：

```bash
cp .env.example .env
# 编辑 .env 文件，设置你的环境变量
```

主要环境变量：

| 变量名 | 说明 | 默认值 |
|---------|------|---------|
| `ENCRYPTION_KEY` | 加密密钥（32字节 base64编码） | 自动生成 |
| `LOG_LEVEL` | 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL) | INFO |
| `DATABASE_URL` | 数据库连接字符串（未来） | sqlite:///./db_ha_manager.db |
| `TASK_TIMEOUT` | 任务超时时间（秒） | 3600 |
| `MAX_CONCURRENT_TASKS` | 最大并发任务数 | 5 |
| `ALLOWED_ORIGINS` | CORS 允许的源（逗号分隔） | localhost, 内网地址 |
| `API_HOST` | API 服务监听地址 | 0.0.0.0 |
| `API_PORT` | API 服务端口 | 8000 |
| `TASKS_DATA_PATH` | 任务数据存储路径 | data/tasks.json |
| `SYNC_HISTORY_PATH` | 同步历史数据路径 | data/sync_history |
| `SYNC_HISTORY_RETENTION_DAYS` | 同步历史保留天数 | 7 |

### 配置文件

`config.yaml` 用于定义集群信息：

```yaml
app:
  app_name: "DB-HA-Manager"
  debug: false
  log_level: "INFO"

clusters:
  my-cluster:
    cluster_name: "生产环境-Oracle DG"
    db_type: "oracle"
    primary:
      sid: "ORCL"
      oracle_home: "/u01/app/oracle/product/19c/dbhome_1"
      oracle_sid: "ORCL"
    primary_ssh:
      host: "192.168.1.10"
      port: 22
      username: "oracle"
      private_key_path: "/home/oracle/.ssh/id_rsa"
    standby:
      sid: "ORCL"
      oracle_home: "/u01/app/oracle/product/19c/dbhome_1"
      oracle_sid: "ORCL"
    standby_ssh:
      host: "192.168.1.11"
      port: 22
      username: "oracle"
      private_key_path: "/home/oracle/.ssh/id_rsa"
    data_files_path: "/u01/oradata/ORCL"
    archivelog_path: "/u01/oradata/ORCL/archivelog"
```

## API 文档

启动后端服务后，访问 http://localhost:8000/api/docs 查看 Swagger/OpenAPI 文档。

## 功能计划

### 已完成
- [x] 项目架构设计
- [x] 后端基础框架
- [x] 前端基础框架
- [x] 基础 API 接口
- [x] 前端页面框架
- [x] Oracle Data Guard 完整搭建流程（7步骤，支持命令审批）
- [x] Switchover 真实切换（支持演练模式）
- [x] Failover 真实接管（支持演练模式）
- [x] 任务管理（支持持久化、取消、日志查询）
- [x] 集群 CRUD API
- [x] 归档清理和备份功能
- [x] 资源监控（表空间、归档保留）
- [x] 历史延迟数据（定时收集）
- [x] 前端实时数据展示
- [x] 前端搜索功能
- [x] 前端设置页面
- [x] CORS 配置（支持环境变量）
- [x] 加密密钥从环境变量读取

### 待完成
- [ ] WebSocket 实时推送（已实现后端，前端待完成）
- [ ] 数据库存储 (PostgreSQL)
- [ ] 用户认证 (JWT)
- [ ] MySQL 主从支持
- [ ] SQL Server Always On 支持
- [ ] PostgreSQL 流复制支持
- [ ] Prometheus 监控集成
- [ ] ELK 日志收集集成

## 安全说明

- SSH 连接使用密钥认证，禁止明文密码
- 配置文件中的敏感信息会被加密存储
- 生产环境应启用用户认证和访问控制
- 使用环境变量 `ENCRYPTION_KEY` 设置加密密钥，避免密钥丢失

## 故障排查

遇到问题时，请查看 [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) 文档，其中包含：
- 常见错误类型和解决方案
- JSX 语法注意事项
- 开发命令速查
- 前后端启动和调试技巧

## 安全说明

- SSH 连接使用密钥认证，禁止明文密码
- 配置文件中的敏感信息会被加密存储
- 生产环境应启用用户认证和访问控制
- CORS 配置默认允许本地和内网地址，生产环境应设置 `ALLOWED_ORIGINS` 环境变量限制允许的源
- 使用环境变量 `ENCRYPTION_KEY` 设置加密密钥，避免密钥丢失

## 许可证

MIT License

## 联系方式

如有问题或建议，请提交 Issue 或 Pull Request。
