# AI Stock — FastAPI + Vue 3 前后端分离架构

本文档描述从 Streamlit 单体应用到 **FastAPI + Vue 3 + SQLite** 前后端分离架构的迁移（Phase 0–2 已完成；原 Streamlit UI 已随 ai_stock 更名移除）。

## 目录结构

```
ai-stock/
├── backend/                        # FastAPI 后端（新）
│   ├── app/
│   │   ├── main.py                 # 应用入口（CORS、路由注册、 lifespan 建表）
│   │   ├── api/                    # 路由层
│   │   │   ├── auth.py             # 注册 / 登录 / 登出 / me
│   │   │   ├── analysis.py         # 分析任务：start/status/result/控制/续跑
│   │   │   ├── stocks.py           # 股票搜索 / 实时行情 / K线
│   │   │   ├── history.py          # 历史报告 + Markdown/PDF 导出
│   │   │   └── watchlist.py        # 自选股 CRUD
│   │   ├── core/
│   │   │   ├── config.py           # pydantic-settings（.env 驱动）
│   │   │   ├── database.py         # SQLAlchemy engine / session / init_db
│   │   │   ├── auth.py             # bcrypt 密码哈希 + PyJWT 签发/校验
│   │   │   └── trading/            # 原项目引擎桥接（sys.path + 惰性导入）
│   │   ├── models/                 # ORM：users / analysis_tasks / watchlist
│   │   ├── schemas/                # Pydantic 请求/响应模型
│   │   ├── services/
│   │   │   ├── analysis_service.py # 任务管理器（复用 web.runner/progress/history）
│   │   │   └── stock_service.py    # 股票查询（复用 dataflows/a_stock）
│   │   └── dependencies.py         # get_current_user（JWT 依赖注入）
│   ├── migrations/                 # Alembic（env.py 从应用 settings 读 URL）
│   ├── tests/                      # pytest（内存 SQLite + TestClient）
│   ├── requirements.txt
│   └── .env.example
│
├── frontend/                       # Vue 3 前端（新）
│   └── src/
│       ├── api/                    # axios 封装 + 模块化 API（自动附 JWT）
│       ├── stores/auth.js          # Pinia：token/user 持久化
│       ├── router/index.js         # 路由 + 未登录守卫
│       ├── views/                  # Login / Register / Dashboard / Analysis / HistoryReport
│       └── components/             # StockSearch / ProgressTracker / ReportViewer / KlineChart
│
├── ai_stock/                       # 核心引擎（保持不变，被后端复用）
├── web/                            # 引擎支撑模块（runner/progress/history/导出）
└── cli/                            # 原 CLI（保留）
```

## 迁移策略：复用而非复制（渐进式）

原项目的 `ai_stock/`、`web/`、`cli/` 包**保持原位不动**，后端通过
`app/core/trading` 把项目根加入 `sys.path` 直接 import：

| 原模块 | 后端复用方式 |
|---|---|
| `web/runner.py::run_analysis_in_thread` | 后台线程执行管线（独立于任何 UI 框架） |
| `web/progress.py::ProgressTracker` | 线程安全进度状态 → `/analysis/status` 快照 |
| `web/history.py` | 未完成任务 / 断点续跑 / 历史列表 |
| `web/stock_display.py` | 「代码+名称」归一化 |
| `web/pdf_export.py` | Markdown / PDF 导出端点 |
| `ai_stock/dataflows/a_stock.py` | 搜索 / 行情 / K线 API |
| `ai_stock/graph/trading_graph.py` | 由 runner 调度的多 Agent 管线 |

好处：单一事实来源，前后端分离版与 CLI 共享同一份引擎代码与
`~/.tradingagents` 数据目录（断点/历史通用）。

引擎导入全部为**惰性导入**：API 进程可在未安装分析栈依赖时启动并服务
认证请求；只有真正发起分析/查询时才加载重型依赖。

## Session State → 新架构映射

| 原 `st.session_state` | 迁移后 |
|---|---|
| `tracker` | 后端内存任务注册表（task_id → ProgressTracker）+ `analysis_tasks` 表 |
| `start_analysis` | `POST /api/analysis/start` |
| `viewing_history` | 路由 `/history/:ticker/:date` |
| `llm_provider / *_llm / llm_base_url` | 请求体参数（每任务快照入库） |
| `market_lookback_days` | 请求体 `lookback_days`（默认本月第一天） |

## API 一览

| 方法 | 端点 | 功能 |
|---|---|---|
| POST | `/api/auth/register` | 注册（bcrypt 哈希入库） |
| POST | `/api/auth/login` | 登录 → JWT（24h） |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前用户（需 Bearer Token） |
| POST | `/api/analysis/start` | 启动分析（原「开始分析」） |
| POST | `/api/analysis/resume-checkpoint` | 从断点续跑 |
| GET | `/api/analysis/status/{task_id}` | 进度快照（前端 2s 轮询） |
| GET | `/api/analysis/result/{task_id}` | 最终报告（完成后） |
| POST | `/api/analysis/{task_id}/pause\|resume\|stop` | 生命周期控制 |
| GET | `/api/analysis/tasks` | 当前用户任务记录（SQLite） |
| GET | `/api/analysis/incomplete` | 可续跑任务（文件系统断点） |
| GET | `/api/stocks/search?q=` | 代码/中文名 → 6 位代码 |
| GET | `/api/stocks/{code}/quote` | 实时行情（腾讯源） |
| GET | `/api/stocks/{code}/kline?days=` | 日 K 线（ECharts 蜡烛图数据） |
| GET | `/api/history` | 历史分析列表 |
| GET | `/api/history/{ticker}/{date}` | 历史报告 JSON |
| GET | `/api/history/{ticker}/{date}/markdown\|pdf` | 导出下载 |
| GET/POST/DELETE | `/api/watchlist` | 自选股管理 |
| GET | `/api/health` | 健康检查 |

交互文档：启动后端后访问 `http://localhost:8000/docs`（Swagger UI）。

## 数据库（SQLite）

表：`users`、`analysis_tasks`（用户归属 + LLM 配置快照 + 状态）、
`watchlist`。开发环境由 `init_db()` 自动建表；演进用 Alembic：

```powershell
cd backend
.venv\Scripts\python -m alembic revision --autogenerate -m "your change"
.venv\Scripts\python -m alembic upgrade head
```

## 本地开发

### 配置（项目根目录 .env，单一事实来源）

服务变量与 LLM key 统一写在**项目根目录** `.env`（v0.5.14 起；模板见
根目录 `.env.example` 的「Backend 服务」一节）：

```dotenv
# LLM（引擎与 backend 共用）
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...

# Backend 服务（可选，不写则用代码默认值；生产必改 SECRET_KEY）
SECRET_KEY=<python -c "import secrets; print(secrets.token_hex(32))">
```

`backend/.env` 若存在则优先级更高，仅作本地覆盖通道。

### 后端（端口 8000）

```powershell
cd D:\code\AI_stock\backend
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

依赖安装（首次）：

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e ..     # 原项目作为引擎库安装
```

### 前端（端口 5173，代理 /api → 8000）

```powershell
cd D:\code\AI_stock\frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:5173` → 注册 / 登录 → 工作台发起分析。

### LLM Key

已合并进项目根目录 `.env`（见上）：`MINIMAX_API_KEY` / `DEEPSEEK_API_KEY`
等与 backend 服务变量写在同一份里；也可设 `BACKEND_URL`。

## 测试

```powershell
cd D:\code\AI_stock\backend
.venv\Scripts\python -m pytest tests -q
```

## 后续阶段（未完）

- Phase 3：SSE/WebSocket 实时进度推送（当前为 2s 轮询）、批量分析、移动端适配
- Phase 4：E2E 测试（Playwright）、Docker Compose 一键部署、软切换路由
