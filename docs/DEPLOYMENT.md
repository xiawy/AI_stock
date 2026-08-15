# AI Stock 部署运维文档

> 版本：v0.5.14 ｜ 适用 OS：Windows / Linux / macOS

---

## 1. 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| Python | ≥ 3.10（推荐 3.11/3.12） | 引擎与后端 |
| Node.js | ≥ 18（建议 20 LTS） | 仅前端构建需要 |
| 网络 | 可访问国内财经数据源 | 通达信 TCP 7709、东财/新浪/腾讯/同花顺 HTTPS |
| LLM API | 至少一家厂商 Key | DeepSeek/Qwen/GLM/MiniMax/OpenAI/Anthropic/OpenRouter/Ollama 等 |
| 磁盘 | ~500MB 起步 | 依赖 + 数据缓存（随使用增长） |

**网络可达性自检**（部署前建议执行）：

```powershell
# 通达信行情服务器（TCP 7709）
Test-NetConnection pytdx-ignite@zhongtai.cn -Port 7709   # 或任一 TDX 服务器
# 东财数据接口
curl.exe -m 10 "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fs=m:90+t:2&fields=f12,f14"
```

任一不通不会立即致命（有 fallback 链），但会降低数据质量评级。

## 2. 安装

### 2.1 引擎 + CLI（必装基础）

```powershell
cd D:\code\AI_stock

# 方式一：pip
python -m venv .venv
.venv\Scripts\pip install -e .

# 方式二：uv（更快）
uv venv && uv pip install -e .

# 验证
.venv\Scripts\ai-stock --version
```

### 2.2 可选依赖

```powershell
# Claude Agent SDK（个人 Pro/Max 订阅额度替代 API 计费）
.venv\Scripts\pip install -e ".[agentsdk]"

# 自进化层（Chroma 向量库；不装则 evolution 自动降级关闭）
.venv\Scripts\pip install -e ".[evolution]"
```

**已知依赖约束**：
- 无 `[google]` extras：mootdx 要求 `httpx<0.26` 与 google-genai 要求的 `httpx>=0.28` 结构性冲突。Gemini 用户需独立环境安装或手动升 httpx（详见 pyproject.toml 内注释）
- `mootdx>=0.11.7` 是兼容 pandas≥2.3 的下限，勿手动降级

### 2.3 后端（backend，独立 venv）

```powershell
cd D:\code\AI_stock\backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e ..          # 引擎作为库安装（共享数据目录）
```

### 2.4 前端（frontend）

```powershell
cd D:\code\AI_stock\frontend
npm install
```

## 3. 配置

### 3.1 引擎配置 — 项目根 `.env`

复制 `.env.example` 为 `.env`，最小配置两行：

```ini
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
DEEPSEEK_API_KEY=sk-xxxxxxxx
```

完整变量：

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | ✅ | openai/deepseek/qwen/glm/minimax/xai/openrouter/anthropic/azure/ollama/claude_agent_sdk |
| `LLM_MODEL` | ✅ | 两档（deep/quick）默认共用此模型 |
| 对应 `<PROVIDER>_API_KEY` | ✅ | 如 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` |
| `BACKEND_URL` | — | 中转/代理网关（国内访问 OpenAI/Anthropic 常用） |
| `TRADINGAGENTS_MAX_TOKENS` | — | 单回复 token 上限；**报告写到一半断掉通常撞此值，调大即可** |
| `LLM_THINKING_LEVEL` / `LLM_REASONING_EFFORT` / `LLM_EFFORT` | — | Google/OpenAI/Anthropic 思考档位 |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | agent_sdk 订阅模式；**不可与 `ANTHROPIC_API_KEY` 共存**（共存会直接报错，护栏行为） |

**目录重定位环境变量**（Docker/多实例场景常用）：

| 变量 | 默认 | ⚠️ 现状 |
|---|---|---|
| `TRADINGAGENTS_RESULTS_DIR` | `~/.tradingagents/logs` | 写入生效，但 **Web 历史读取端仍硬编码默认路径**（技术文档 B4），设置后 Web 历史页看不到报告 |
| `TRADINGAGENTS_CACHE_DIR` | `~/.tradingagents/cache` | 同上，`web/history.py` 的 checkpoint 读取未跟随 |
| `TRADINGAGENTS_MEMORY_LOG_PATH` | `~/.tradingagents/memory/trading_memory.md` | — |
| `TRADINGAGENTS_EVOLUTION_DIR` | `~/.tradingagents/evolution_data` | — |

> 结论：**当前版本建议保持默认目录**，自定义目录变量在 Web/CLI 混用场景有已知不一致。

### 3.2 后端配置 — `backend/.env`

复制 `backend/.env.example`：

```ini
SECRET_KEY=<必改！python -c "import secrets; print(secrets.token_hex(32))">
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
DATABASE_URL=sqlite:///./data/aistock.db
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
PROJECT_ROOT=..
DEBUG=false
```

> ⚠️ `SECRET_KEY` 有开发默认值（技术文档 B10）：生产**必须**显式配置，否则 JWT 可被伪造。CORS 按实际部署域名调整。

## 4. 运行

### 4.1 CLI

```powershell
.venv\Scripts\ai-stock          # 交互式：选股 → 选分析师 → 出报告
```

### 4.2 后端（端口 8000）

```powershell
cd D:\code\AI_stock\backend
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

- 首次启动自动建表（`init_db()`）；Swagger：`http://localhost:8000/docs`；健康检查：`/api/health`
- 生产模式去掉 `--reload`，建议 `--workers 1`（任务管理器为进程内存态，多 worker 会分裂任务注册表）

### 4.3 前端（端口 5173）

```powershell
cd D:\code\AI_stock\frontend
npm run dev          # 开发（Vite 已代理 /api → 8000）
npm run build        # 生产构建 → dist/，由任意静态服务器/Nginx 托管
```

访问 `http://localhost:5173` → 注册 → 登录 → 工作台。

### 4.4 Docker（CLI 模式）

镜像仅包含 CLI（`ENTRYPOINT ["ai-stock"]`），已预装 CJK 字体并预建数据目录解决卷权限问题（issue #46/#48）：

```powershell
# 构建并交互运行（.env 自动注入）
docker compose run --rm ai-stock

# 本地 Ollama 模式（profile）
docker compose --profile ollama run --rm ai-stock-ollama
```

数据持久化在命名卷 `tradingagents_data`（`/home/appuser/.tradingagents`）。

> ⚠️ 前后端**无官方 Docker 服务定义**（技术文档 O6）：生产部署 Web 版需自行编排 backend（uvicorn）与 frontend（nginx 托管 dist 并反代 `/api`）。

## 5. 数据目录与产物

```
~/.tradingagents/
├── cache/                    # 日线 CSV（{code}-astock-daily.csv）、mootdx 探测缓存
│   └── checkpoints/          # per-ticker 断点 SQLite（checkpoint_enabled 时）
├── logs/
│   └── <TICKER>/<YYYY-MM-DD>/
│       └── full_states_log_<date>.json    # 全量状态快照（报告/辩论/决策）
├── memory/
│   └── trading_memory.md     # 记忆日志（pending → 结算回填）
├── evolution_data/           # Chroma 向量库 + 进化状态
├── learnings/                # 复盘产出
└── incomplete_tasks.json     # 可续跑任务索引

backend/data/aistock.db       # 后端 SQLite：users / analysis_tasks / watchlist
```

## 6. 数据库运维

```powershell
cd D:\code\AI_stock\backend

# 生成迁移（改了 ORM 模型后）
.venv\Scripts\python -m alembic revision --autogenerate -m "add xxx"

# 应用迁移
.venv\Scripts\python -m alembic upgrade head

# 备份（SQLite 单文件，停写后直接复制）
Copy-Item data\aistock.db data\aistock.db.bak
```

- 开发环境首启 `init_db()` 自动建表；此后 schema 演进一律走 Alembic
- SQLite 适合单机/小团队；并发写入增大后可切 `DATABASE_URL` 到 PostgreSQL（SQLAlchemy 2.0 写法天然兼容，迁移文件或需 review 方言差异）

## 7. 日常运维操作

| 操作 | 方法 |
|---|---|
| **清数据缓存**（数据源改版/缓存损坏时） | 删 `~/.tradingagents/cache/*.csv`；缓存当日命中会跳过在线拉取， stale 数据先删缓存 |
| **清理断点** | 删 `cache/checkpoints/` 下对应 ticker 的 SQLite；Web 端也可在"未完成任务"里移除 |
| **记忆日志维护** | 直接编辑 `trading_memory.md`（append-only 格式，`[date \| ticker \| rating \| ...]`）；pending 条目勿删（等待结算）；可设 `memory_log_max_entries` 自动轮转 |
| **重置进化层** | 删 `~/.tradingagents/evolution_data/`；自定义策略在仓库 `custom_strategies/` 内随版本管理 |
| **日志排查** | 引擎日志走 Python logging（CLI 实时打印；backend 看 uvicorn 输出）；每次分析的全量过程在 `full_states_log_*.json` |
| **磁盘回收** | 定期归档/清理 `logs/`（按 ticker 分目录，可整目录移走） |

## 8. 监控与健康检查

| 检查点 | 命令/位置 | 期望 |
|---|---|---|
| 后端存活 | `GET /api/health` | `{"status":"ok"}` |
| 任务进度 | `GET /api/analysis/status/{task_id}` | 前端 2s 轮询同源；阶段推进 |
| LLM 可用 | 发起一次单分析师短分析 | 正常产出报告 |
| 数据源可用 | 跑一次 `600519` 技术分析 | K 线非空、无连续 fallback 警告 |
| pipeline 调度 | backend 启动日志无 "Pipeline service init failed" | 调度器随 lifespan 启停 |

## 9. 安全清单

- [ ] `backend/.env` 的 `SECRET_KEY` 已替换为随机 32 字节 hex（**生产强制**）
- [ ] `DEBUG=false`
- [ ] `CORS_ORIGINS` 只含真实前端域名（勿用 `*`，与 credentials 冲突且不安全）
- [ ] HTTPS 终结（Nginx/Caddy 反代），JWT 走 `Authorization` 头不出现在 URL
- [ ] `.env` 不入库（`.gitignore` 已覆盖，自检一次）
- [ ] API Key 最小权限、定期轮换；`CLAUDE_CODE_OAUTH_TOKEN` 勿与 `ANTHROPIC_API_KEY` 同置
- [ ] 多用户部署时关注 rate limit（当前 API 层无内建限流，可在反代层加）

## 10. 故障排查手册

| 症状 | 成因 | 处置 |
|---|---|---|
| **mootdx 连不上/名称解析失败** | 通达信服务器不可达或被防火墙拦 TCP 7709 | 系统会自动探测多服务器并对失败做 300s 负缓存；持续失败换网络或等待；错误信息会明确提示"稍后重试或直接输入 6 位代码" |
| **报告写到一半截断** | 撞单回复 token 上限（非上下文超长） | `.env` 设 `TRADINGAGENTS_MAX_TOKENS=8192`（或更高） |
| **数据全部 `[数据缺失]`** | 数据源限流/封禁（东财节流 1s 仍有峰值风险） | 稍后重试；质量门控会自动降级并在报告标注；清缓存强制重拉 |
| **Web 历史页空白但报告存在** | 设置了 `TRADINGAGENTS_RESULTS_DIR`（已知不一致 B4） | 移除该环境变量回到默认目录，或等修复 |
| **"未完成任务"一直不消失** | 任务完成但索引更新被 Windows 文件占用拦截 | 系统已重试+降级并告警；确认无多实例共用同一 home 后手动编辑 `incomplete_tasks.json` |
| **分析卡在某阶段不动** | LLM 供应商限流/网络断 | Web 端暂停后可续跑（checkpoint）；或终止重跑；查 uvicorn 日志定位卡住的节点 |
| **重启后任务永远"进行中"** | backend 重启孤儿任务（已知 B9） | 手动将 DB `analysis_tasks` 中 running 行改为 failed；或等生命周期修复 |
| **agent_sdk 分析失败回落 API** | 订阅额度耗尽/CLI 未登录 | 正常降级行为；`claude setup-token` 重新生成 token |
| **评级解析失败/默认 Hold** | 模型未按格式输出五档 | 换指令跟随更强的模型；解析器是确定性正则，报告原文可查证格式 |
| **PDF 中文乱码/空白** | 系统无 CJK 字体 | Windows/macOS 一般自带；Linux 装 `fonts-noto-cjk`（Docker 镜像已预装） |
| **版本一致性测试失败** | CHANGELOG 顶部条目落后于 pyproject 版本 | 补 CHANGELOG 条目（发版流程检查项） |

## 11. 升级与回滚

```powershell
# 升级（源码部署）
git pull
.venv\Scripts\pip install -e . --upgrade
cd backend; .venv\Scripts\python -m alembic upgrade head
# frontend 有变更时
cd ../frontend; npm install; npm run build

# 回滚
git checkout <tag>
.venv\Scripts\pip install -e .
.venv\Scripts\python -m alembic downgrade -1     # 如需回退迁移
```

升级前备份：`backend/data/aistock.db` + `~/.tradingagents/`（至少 `memory/` 与 `logs/`）。

## 12. 已知运维风险（截至 v0.5.14）

1. **B4 目录环境变量不一致**：Web 历史读取硬编码默认路径（§3.1 表格）；自定义目录场景历史/续跑功能受损
2. **B9 孤儿任务**：backend 重启后 DB 中 running 任务无对应进程，需人工修正状态
3. **B10 SECRET_KEY 默认值**：忘配等于裸奔，上线前务必检查
4. **Web 无 Docker 编排**：仅 CLI 有官方镜像；Web 生产部署需自行反代与进程守护（建议 systemd/NSSM + Nginx）
5. **单进程任务注册表**：AnalysisTaskManager 在内存，backend 只能单 worker 水平扩展受限；多实例部署需粘性路由或引入外部任务队列（roadmap）
6. **免费数据源无 SLA**：高峰限流属正常现象，质量门控与 fallback 已尽力兜底，但重度使用建议错峰（盘后）
7. **测试基线 1 失败**：`test_version_consistency`（CHANGELOG 落后），不影响运行，属发版流程问题

## 13. 性能与容量参考

- 单次完整分析（6 分析师 + 1 轮辩论）：节点 10+、LLM 调用 20~50 次（含工具循环）；耗时主要取决于 LLM 延迟（分钟级）
- 日线缓存命中后数据层耗时可忽略；首次分析某股票会拉全量日线（~800 根）
- backend 单机承载：受 LLM 供应商 rate limit 限制远先于受服务器资源限制；分析任务为后台线程，CPU 占用低
- `logs/` 增长：每股票每日一个 JSON（数十至数百 KB），按需归档

---

*本文档对应代码版本 v0.5.14；命令以 PowerShell（Windows）书写，Linux/macOS 同理替换路径分隔符与激活脚本。*
