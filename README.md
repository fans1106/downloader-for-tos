# HF Downloader

一个基于 FastAPI 的 Hugging Face 下载和 TOS 上传服务，提供：

- Web UI 任务管理
- 串行任务队列
- dataset / model 双支持
- 任务级 HF Token
- 任务级 TOS 目标地址
- 任务日志、取消、重试、健康检查

## 本地启动

```bash
pip install -r requirements.txt
python download.py
```

默认地址：`http://0.0.0.0:8000/tasks`

## Docker 打包

```bash
docker build -t hf-downloader:latest .
```

镜像内只打包应用本身，不内置特定环境的 `tosutil` 二进制和配置。推荐把 `tosutil` 和 `.tosutilconfig` 从宿主机挂载进容器。

```bash
docker run --rm \
  -p 8000:8000 \
  -e HF_DOWNLOADER_APP_SECRET=replace-this-secret \
  -e HF_DOWNLOADER_AUTH_PASSWORD=replace-this-password \
  -e HF_DOWNLOADER_TOSUTIL_PATH=/opt/tosutil/tosutil \
  -e HF_DOWNLOADER_TOSUTIL_CONFIG=/opt/tosutil/.tosutilconfig \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/downloads:/app/downloads \
  -v /path/to/tosutil-dir:/opt/tosutil:ro \
  hf-downloader:latest
```

## Docker Compose 一键启动

先把宿主机上的 `tosutil` 和 `.tosutilconfig` 放到 `./tosutil/` 目录：

```bash
./tosutil/tosutil
./tosutil/.tosutilconfig
```

然后直接启动：

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

停止并删除容器：

```bash
docker compose down
```

如果你的 `tosutil` 不在项目下的 `./tosutil`，可以先指定宿主机目录：

```bash
export HF_DOWNLOADER_TOSUTIL_DIR=/absolute/path/to/tosutil-dir
docker compose up -d --build
```

Compose 默认使用 named volumes 持久化数据，避免 Linux 上 bind mount 的目录权限问题。

## 持久化目录

容器里建议持久化以下目录：

- `/app/data`：SQLite 数据库
- `/app/logs`：服务日志
- `/app/downloads`：任务下载缓存
- `/opt/tosutil`：挂载 `tosutil` 和 `.tosutilconfig`

## 环境变量

- `HF_DOWNLOADER_HOST`
- `HF_DOWNLOADER_PORT`
- `HF_DOWNLOADER_APP_SECRET`
- `HF_DOWNLOADER_AUTH_PASSWORD`：Web/API 访问密码，默认使用 `HF_DOWNLOADER_APP_SECRET`
- `HF_DOWNLOADER_TOSUTIL_PATH`
- `HF_DOWNLOADER_TOSUTIL_CONFIG`
- `HF_DOWNLOADER_TOSUTIL_DIR`
- `HF_DOWNLOADER_DEFAULT_BATCH_SIZE_GB`
- `HF_DOWNLOADER_DEFAULT_MAX_RETRIES`
- `HF_DOWNLOADER_QUEUE_POLL`
- `HF_DOWNLOADER_LOG_LEVEL`

## API

- `GET /healthz`
- `GET /api/tasks`
- `POST /api/tasks`
- `GET /api/tasks/{id}`
- `POST /api/tasks/{id}/cancel`
- `POST /api/tasks/{id}/retry`
- `POST /api/tasks/reorder`
- `GET /api/tasks/{id}/logs`
- `GET /api/tasks/{id}/stream`
