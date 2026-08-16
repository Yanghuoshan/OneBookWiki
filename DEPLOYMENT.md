# OneBookWiki 部署指南

## 快速启动（推荐）

### 使用统一启动脚本

```bash
# 启动所有服务（服务器 + Chat Worker）
ONEBOOKWIKI_ENV=production ./start_all.sh start

# 查看服务状态
./start_all.sh status

# 查看 Chat Worker 日志
./start_all.sh logs

# 停止所有服务
./start_all.sh stop
```

## 手动启动（分离模式）

如果你需要更精细的控制，可以分别启动：

### 1. 启动主服务器

```bash
ONEBOOKWIKI_ENV=production ./start.sh
```

### 2. 启动 Chat Worker（另一个终端）

```bash
./start.sh --chat-worker
```

或后台运行：

```bash
nohup ./start.sh --chat-worker > logs/chat_worker.log 2>&1 &
```

## 环境变量

在 `.env` 文件中配置：

```bash
# 环境模式
ONEBOOKWIKI_ENV=production

# 服务器配置
ONEBOOKWIKI_HOST=0.0.0.0
ONEBOOKWIKI_PORT=8000

# 数据目录
ONEBOOKWIKI_BOOKS_ROOT=/path/to/books
ONEBOOKWIKI_DB_PATH=/path/to/onebookwiki.db

# AI 提供商配置（必需）
GENERATION_PROVIDER=anthropic  # 或 openai
ANTHROPIC_API_KEY=your_key_here
# 或
OPENAI_API_KEY=your_key_here
```

## 重要说明

### 为什么需要 Chat Worker？

问答功能需要后台处理任务（检索、生成答案），Chat Worker 是一个独立的进程，负责：

1. 从任务队列中领取问答请求
2. 检索书籍证据
3. 调用 AI 模型生成答案
4. 更新数据库状态

**没有 Chat Worker，问答请求会一直处于 `queued` 状态，无法获得答案。**

### 生产环境要求

- Python 3.10+
- 已安装依赖：`pip install -e .`
- 已构建前端：`cd frontend && npm install && npm run build`
- 配置了 AI API Key（Anthropic 或 OpenAI）

## 故障排查

### 问答不工作

1. 检查 Chat Worker 是否运行：
   ```bash
   ./start_all.sh status
   ```

2. 查看 Worker 日志：
   ```bash
   ./start_all.sh logs
   # 或
   tail -f logs/chat_worker.log
   ```

3. 检查环境变量是否配置正确（特别是 API Key）

### 服务无法启动

1. 检查端口是否被占用：
   ```bash
   lsof -i :8000  # macOS/Linux
   netstat -ano | findstr :8000  # Windows
   ```

2. 检查 Python 版本：
   ```bash
   python3 --version  # 需要 3.10+
   ```

3. 检查日志目录是否可写：
   ```bash
   mkdir -p logs
   ```

## 进程管理（可选）

对于生产环境，建议使用进程管理工具：

### 使用 systemd

创建 `/etc/systemd/system/onebookwiki.service`:

```ini
[Unit]
Description=OneBookWiki Server
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/onebookwiki
Environment="ONEBOOKWIKI_ENV=production"
ExecStart=/path/to/onebookwiki/start.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

创建 `/etc/systemd/system/onebookwiki-worker.service`:

```ini
[Unit]
Description=OneBookWiki Chat Worker
After=network.target

[Service]
Type=simple
User=your_user
WorkingDirectory=/path/to/onebookwiki
ExecStart=/path/to/onebookwiki/start.sh --chat-worker
Restart=always

[Install]
WantedBy=multi-user.target
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable onebookwiki onebookwiki-worker
sudo systemctl start onebookwiki onebookwiki-worker
```

### 使用 supervisor

```ini
[program:onebookwiki-server]
command=/path/to/onebookwiki/start.sh
directory=/path/to/onebookwiki
environment=ONEBOOKWIKI_ENV="production"
autostart=true
autorestart=true
user=your_user

[program:onebookwiki-worker]
command=/path/to/onebookwiki/start.sh --chat-worker
directory=/path/to/onebookwiki
autostart=true
autorestart=true
user=your_user
```
