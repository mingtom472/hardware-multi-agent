# Hardware Multi-Agent · Engineering AI Studio

面向嵌入式硬件与电源工程的多模型协作 MVP，使用 **Python 3.12、FastAPI、Next.js 和 TypeScript**。默认 Mock 模式无需 API Key，可完成任务分类、可选图片分支、并行硬件分析、独立验证、结果汇总、重试及 Token/费用统计。

模型输出、图片识别和费用在 Mock 模式下均明确标为模拟。附件原件会保存，但当前不会真正识别电路图片、解析 PDF 或执行上传代码。工程结论仍需数据手册、计算和实测支持。

## 安装与启动

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js 22+（推荐 24）与 npm。在仓库根目录执行：

```bash
cp .env.example .env
./scripts/install.sh
./scripts/check.sh
./scripts/dev.sh
```

安装使用 `uv.lock` 和 `frontend/package-lock.json` 冻结依赖。默认前端端口 `3000`、API 端口 `8000`，均仅绑定 `127.0.0.1`。`Ctrl+C` 停止脚本自行启动的服务。端口冲突时：

```bash
BACKEND_PORT=8100 FRONTEND_PORT=3100 ./scripts/dev.sh
```

Windows PowerShell 脚本也已提供：

```powershell
Copy-Item .env.example .env
.\scripts\install.ps1
.\scripts\check.ps1
.\scripts\start.ps1
```

Windows 脚本尚未在 Windows 实机执行验证；Linux 测试通过不等于 Windows 验收通过。`dev-next.sh` 保留为 `dev.sh` 的兼容入口。

## 工作流与模型边界

```text
任务分类 → 可选图片识别 → 并行硬件分析 → 验证 → 结果汇总
                         每次调用：超时、重试、耗时、Token、费用
```

工作流默认配置位于 `backend/app/workflows/default.json`。可配置分析模型列表、图片/验证/汇总模型、并行数、单次超时和重试次数。模型注册表还支持自动、手动和混合角色路由，以及快速、专业、专家策略；自动规划目前是规则式实现，尚未经过模型效果评测。任务拥有最大调用次数、执行轮次及预算边界。

`backend/app/providers/` 为 OpenAI、Claude、DeepSeek、豆包和 GLM 保留独立适配器。OpenAI Responses、Anthropic Messages、各家的 Chat/Ark 协议不被视为完全相同。额外提供 Qwen 与 Moonshot 文本适配器。

- **默认 Mock**：所有支持的供应商通过独立模拟实例执行，不向第三方发出模型请求。
- **已实现的真实文本协议**：OpenAI、Claude、DeepSeek、Qwen、Moonshot；协议测试使用 HTTP mock transport，真实账号和目标模型可达性尚未验证。
- **额外预留**：豆包、GLM 真实接入、真实视觉、图片生成和 Claude Agent SDK；未实现的真实能力会明确报错，不冒充成功。

注册表中的目标模型名称是可编辑配置，并非可调用承诺。真实使用需要正确的 model ID、后端密钥、明确的连接测试及价格配置。文本连接测试可能收费，成功也不证明视觉、SDK 或其他模型可用。

## API 使用

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/api/providers
curl --fail http://127.0.0.1:8000/api/workflows/default
curl --fail -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"评审24V输入、5V输出Buck电源的器件裕量和测试计划","file_ids":[]}'
```

任务响应包含 `id`，通过 `GET /api/tasks/{id}` 查看阶段、调用记录、Token、费用和结果。任务与阶段快照保存到 SQLite；重启后历史可查询，中断任务需要显式重试，不会自动重放可能收费的调用。API 还提供任务取消、重试、记录删除、费用查询及 CSV 导出。

## 文件上传与资源限制

支持图片、PDF、源代码及一般二进制文件，不用扩展名白名单阻止原件保存。浏览器直接发送原始文件体，Next.js 上传 Route Handler 转发原始流，FastAPI 边接收边写盘并计算 SHA-256；不会无条件把整个文件转为 Base64 或装入内存。文件名只作元数据，磁盘路径使用生成的对象标识符。

```bash
curl --fail -X POST 'http://127.0.0.1:8000/api/uploads?filename=capture.bin' \
  -H 'Content-Type: application/octet-stream' --data-binary @capture.bin
```

将返回的文件 `id` 放入任务的 `file_ids`。不支持内容解析的文件仍保存原件，状态为未解析；上传不授予执行权限。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `HW_MAX_UPLOAD_BYTES` | 未设置 | 可选管理员单文件字节上限；例如 `26214400` 为 25 MiB |
| `HW_MAX_STORAGE_BYTES` | 未设置 | 可选本地存储总字节配额 |
| `HW_STORAGE_RESERVE_BYTES` | `16777216` | 每块写入前至少保留 16 MiB 可用磁盘空间 |
| `HW_UPLOAD_CHUNK_BYTES` | `65536` | 写入分块 64 KiB |
| `HW_MAX_STORED_FILES` | `1000` | 本地文件数量上限 |
| `HW_MAX_FILES_PER_TASK` | `8` | 单任务附件数上限 |
| `HW_MAX_ACTIVE_TASKS` | `4` | 活动任务上限 |

未设置单文件字节上限也**不代表真正无限大小**。实际磁盘、文件系统、浏览器、网络、反向代理和外部 API 都有限制；管理员可按部署环境配置资源配额。分片上传、断点续传与对象存储当前只有接口，尚不可用。原始文件和 SQLite 默认位于 `.local/data`。

## 安全配置与费用

`.env.example` 只包含空密钥项。真实 API Key 应通过本机 `.env`、后端进程环境或部署平台秘密管理注入，不能提交到 Git，也不能放进 `NEXT_PUBLIC_*`、工作流 JSON、提示词、日志、报告或前端构建参数。

`BACKEND_URL` 是 Next.js 服务端代理目标。普通 API rewrites 在构建时确定，上传路由也在运行时读取；切换部署目标时应同步设置并重新构建。

Token 区分供应商返回值与本地模拟/估算值，缓存输入只在供应商返回时统计。费用按可配置价格估算，并非供应商发票。SQLite 台账支持调用前预算预检、费用预留和完成后对账；未知费用保留为待核实。默认单任务 `$1`、每日 `$10`、每月 `$100`，可配置。估值不能保证最终供应商账单绝无偏差，失败或超时也可能收费。

## 硬件工具与扩展接口

当前确定性工具包括：

- `calculate_buck_duty(vin, vout)`：理想连续导通 Buck 的 `D = Vout / Vin`，验证 `Vin > Vout > 0`，说明单位、假设及排除项。
- `analyze_bom_csv(content)`：检查 CSV 缺失参数、重复位号、数量格式及位号数量匹配；不包括电气额定值审核。

电源拓扑识别、器件电气参数检查、数据手册解析、通用公式与波形处理保留接口。嵌入式软件 Agent、Claude Agent SDK、编译、调试及真实硬件操作均不在首阶段实现。

PostgreSQL 任务仓储、Redis 队列、对象存储及分片上传协议位于 `backend/app/ports.py`。设置对应 URL 不会自动启用集成。当前使用 SQLite、本地文件和单个后端 worker；不支持分布式执行或生产多租户隔离。

## 测试和部署

```bash
./scripts/check.sh                    # pytest + TypeScript + Next生产构建
(cd frontend && npx playwright install --with-deps chromium)
npm --prefix frontend run test:e2e    # 真实浏览器 + 本地FastAPI，模型仍为Mock
# 已有Chromium的受限环境：
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium npm --prefix frontend run test:e2e

docker compose config --quiet
docker compose up --build -d          # FastAPI + Next.js
docker compose ps
docker compose down                  # 保留数据库与原始文件卷
```

本轮 170 项后端测试、4 项浏览器测试、类型检查、生产构建、直接启动及当前 Docker 功能请求均通过。详见 [验证记录](docs/validation.md)。缺少凭据的真实模型检查不计为通过。

容器配置是后续部署起点，默认只在本机发布端口。公网鉴权、多租户、文件扫描、生产权限管理未完成；当前适合本地可信用户开发。详见 [部署说明](docs/deployment.md)。

## 可选 Streamlit 工作台与后续需求

先前 V2.1 需求的 Streamlit 工作台保留为额外入口，不替代本次要求的 Next.js 主界面：

```bash
./scripts/install.sh --python-only
./scripts/check.sh --python-only
./scripts/dev-studio.sh
# 或容器额外启用：
docker compose --profile studio up --build -d
```

Windows 使用 `start.ps1 -Studio`；安装/检查时可加 `-PythonOnly`。该界面使用 `HW_API_URL` 与 `HW_BROWSER_API_URL` 区分服务端 API 地址和浏览器上传地址。完整历史需求见 [V2.1需求书](docs/requirements-v2.1.md)，实现边界及后续阶段见 [路线图](docs/roadmap.md)。

```text
backend/app/             FastAPI、编排、上传、持久化、费用与扩展接口
backend/app/providers/   独立适配器和模拟模型
backend/app/hardware/    硬件基础工具与专业扩展接口
backend/tests/          Python自动化测试
frontend/               Next.js / TypeScript主界面与浏览器测试
ui/                     可选Streamlit界面
scripts/                安装、启动和检查脚本
docs/                   架构、部署、历史需求及实现边界
```
