# 本地启动与部署

## 默认开发流程：FastAPI + Next.js

需要 Python 3.12、uv、Node.js 22+（推荐 24）和 npm。在仓库根目录：

```bash
cp .env.example .env
./scripts/install.sh
./scripts/check.sh
./scripts/dev.sh
```

默认 API `8000`、Next.js `3000`，只绑定本机。`BACKEND_PORT=8100 FRONTEND_PORT=3100 ./scripts/dev.sh` 可调整端口。脚本只管理自己创建的两个服务，退出时停止它们。

手动运行：

```bash
# 终端一
.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --workers 1
# 终端二
BACKEND_URL=http://127.0.0.1:8000 npm --prefix frontend run dev -- --port 3000
```

Windows 提供 `install.ps1`、`check.ps1`、`start.ps1`，默认同样安装/运行 Next.js。当前 Linux 云环境无法替代 Windows 实机验收，因此 Windows 路径标为未验证。

## 容器

```bash
docker compose config --quiet
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health
docker compose logs --tail=100 backend frontend
docker compose down
```

默认服务是 FastAPI 和 Next.js，均使用非 root 运行用户。`studio-data` 命名卷保存 SQLite 和原始附件；普通停止不要加 `-v`，该选项会删除数据。后端仅使用一个 worker/副本；SQLite 历史持久化不代表多副本任务执行或共享上传锁。

前端 `BACKEND_URL` 在构建时写入普通 API rewrites，上传 Route Handler 还在运行时读取。Compose 同时设置构建参数和运行环境为 `http://backend:8000`。更换目标时应同步两处并重新构建。生产启动使用 Next standalone 输出和静态资源。

Compose 可从本机 `.env` 向后端注入配置，不向前端注入供应商密钥。`.env` 被排除在镜像构建上下文外。真实部署应使用平台安全秘密管理；真实模型 ID、权限和能力仍须通过明确的连接测试确认。

## 可选 Streamlit 工作台

```bash
./scripts/install.sh --python-only
./scripts/check.sh --python-only
./scripts/dev-studio.sh
# 容器额外启用：
docker compose --profile studio up --build -d
```

Streamlit 端口为 `8501`。服务端使用 `HW_API_URL`，浏览器直接上传组件使用 `HW_BROWSER_API_URL`。容器内的 `http://backend:8000` 不能作为宿主机浏览器地址。Windows 使用 `start.ps1 -Studio`；`-Studio -WithNext` 可同时启动两种界面。

## 上传、费用及外部能力

默认没有固定产品层单文件字节上限，但磁盘保留量、文件数量、任务附件数与并发均有资源保护。管理员可显式设置 `HW_MAX_UPLOAD_BYTES` 和 `HW_MAX_STORAGE_BYTES`，例如单文件 25 MiB。修改时同时检查 Next.js、反向代理、文件系统、网络与供应商 API 的限制，不能宣称真正无限上传。

上传 Route Handler 转发原始流，后端每块写入前检查资源。分片上传、断点续传、对象存储直传与大文件内容解析尚未实现；未知文件仅保留原件，不执行或解压。

Mock 默认不需密钥，也不会发送模型请求。真实文本协议测试采用 HTTP mock transport，不代表真实账号连通。启用真实调用还需要准确模型 ID、合法凭据、价格及连接测试；失败或超时可能计费，本地估值和预算台账需要与外部账单对账。

PostgreSQL、Redis、对象存储、完整 RAG、ngspice、真实视觉和 Agent SDK 尚未接通。当前配置适用于本地可信用户，公网鉴权、多租户、文件扫描和不可信代码执行隔离未完成，不应将本机开发服务直接暴露到公网。

## 验证范围

`GET /health` 只说明 API 启动成功。完整验证还需上传原件、提交 Mock 任务、轮询完成、检查阶段/Token/费用，以及重启后查询 SQLite 历史。前端应通过类型检查、生产构建和浏览器测试；Docker 应独立检查镜像构建及容器中的真实请求。

历史版本的容器检查不能自动套用到后续修改。当前版本的通过数量、未运行项及外部阻塞以实际测试报告为准。真实账号、Windows 和公网发布属于独立验证项。

## 代理与证书

安装保留 TLS 和锁文件校验。企业代理环境应向 Docker 构建提供受信任 CA 与已有代理路由。Dockerfile 支持可选 BuildKit secret `build_ca`，只在依赖安装时挂载，不写入最终镜像：

```bash
docker build --build-arg HTTPS_PROXY --build-arg HTTP_PROXY \
  --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem \
  -f backend/Dockerfile -t hardware-multi-agent .
```

前端 Dockerfile 支持相同 secret。可用本机 override 配置 `build.secrets` 和 `build.args`；不要把代理凭据写入仓库或关闭 TLS。默认 Docker 配置目录不可写时，可将 `DOCKER_CONFIG` 指向可写缓存目录。
