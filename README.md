# OneBookWiki

OneBookWiki 是一个基于检索优先策略的结构化书籍分析工具。它将书籍按章节拆分为不可变存档，建立持久化本地索引，通过 LLM 生成结构化维基页面，并提供 Web 阅读器和 REST API。

```text
raw/chapters/  →  不可变证据登记（SQLite Grounded v2）  →  发布的知识版本
      \→  wiki/book.md, wiki/index.md, wiki/chapters, wiki/knowledge, wiki/structure.json, wiki/evidence.json
```

原始章节文件是唯一事实来源。所有陈述与综合内容先在 SQLite 中以不可变的 Grounded v2 知识版本发布，`wiki/` 下的静态 Markdown/JSON 只是该已发布版本的只读投影，可随时从数据库重新渲染。

---

## 快速启动

完整 pipeline：**导入源文件 → 向量索引 → LLM 生成维基 → 渲染 Markdown**。其中索引依赖嵌入模型，生成依赖大模型——两者必须配置其一才能跑通。

### 1. 安装

```bash
git clone <repo-url>
cd onebookwiki
pip install -e ".[server,rag,local-embeddings]"
```

### 2. 配置嵌入模型

Pipeline 默认使用本地 BGE-M3。模型首次运行自动下载到 `~/.cache/huggingface/`，也可以手动指定已有模型路径：

```bash
# 使用 HuggingFace 默认路径（首次自动下载，约 2GB）
export ONEBOOKWIKI_BGE_M3_MODEL=BAAI/bge-m3

# 或指定本地已下载的模型目录
export ONEBOOKWIKI_BGE_M3_MODEL=/path/to/models/bge-m3
```

如不想部署本地模型，可用 ModelScope 云端嵌入替代：

```bash
export MODELSCOPE_API_KEY=your-token
# 构建时加 --backend modelscope
```

或使用纯关键词检索（无嵌入模型依赖，但检索质量较低）：

```bash
# 构建时加 --backend lexical
```

### 3. 配置大模型

生成维基页面需要 OpenAI 兼容接口。配置 API 密钥和模型：

```bash
export ONEBOOKWIKI_LLM_PROVIDER=openai-compatible
export ONEBOOKWIKI_LLM_API_KEY=your-api-key
export ONEBOOKWIKI_LLM_MODEL=gpt-4o-mini
export ONEBOOKWIKI_LLM_BASE_URL=https://api.openai.com/v1   # 可替换为任意兼容端点
```

仅做检索、不做生成时加 `--provider none`，无需配置大模型。

### 4. 构建

发布一个可对外提供服务的 Grounded v2 版本（非 `--dry-run`）时，必须提供 `--database` 和 `--book-id`，指向一个已存在的 Grounded v2 SQLite 数据库和其中的数字书籍 ID：

```bash
onebookwiki-build book.epub ./books/my-book \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
```

不带 `--database`/`--book-id` 时只能使用 `--dry-run`：导入、索引、构建生成计划，但不发布知识、不渲染 `wiki/`、不构建向量。

支持的源格式：PDF、EPUB、MOBI/AZW/AZW3、TXT、DOC/DOCX、HTML。

PDF 默认从书签/目录/标题自动检测章节边界；如需手动控制分章，可使用 `--pages-per-chapter` 或 `--structure-manifest`。

### 5. 启动服务

启动脚本通过环境变量 `ONEBOOKWIKI_ENV` 区分两种运行模式：

| 模式 | `ONEBOOKWIKI_ENV` | 行为 |
|------|-------------------|------|
| 开发 | 不设置 或 `development`（默认） | 后端 :8000 + Vite 前端 :5173，前后端分离 |
| 生产 | `production` | 后端 :8000 托管前端静态文件 + API，单端口对外 |

#### 开发模式（默认）

前后端分离，Vite 开发服务器代理 API 请求到后端：

```bash
# 终端 1：后端 API
# Linux/macOS              |  Windows PowerShell
./start.sh                 |  .\start.ps1

# 终端 2：前端阅读器
cd frontend && npm install && npm run dev -- --host 127.0.0.1
```

浏览器打开 `http://127.0.0.1:5173/book`，上传页面 `http://127.0.0.1:5173/admin`。

#### 生产模式

后端单端口托管前端静态文件 + API + 书籍文件，适合外网部署：

```bash
# Linux/macOS
ONEBOOKWIKI_ENV=production ./start.sh

# Windows PowerShell
$env:ONEBOOKWIKI_ENV = "production"
.\start.ps1

# Windows CMD
set ONEBOOKWIKI_ENV=production
start_server.bat
```

浏览器打开 `http://<服务器IP>:8000` 或绑定的域名即可访问，无需独立前端服务。

生产模式下启动脚本自动使用 `--workers 4` 多进程和 `--proxy-headers` 参数。如需调整 worker 数量，修改对应启动脚本中的 `--workers` 值。

### Docker 一键部署

Docker 镜像内置生产模式，构建并启动后直接对外服务：

```bash
docker compose up -d
```

访问 `http://<服务器IP>:8000`。

### 可选：PDF OCR 辅助

对于缺少文字层的扫描版 PDF，可安装本地 PP-OCRv5 辅助结构分析。模型需提前下载到本地目录：

```bash
pip install -e ".[pdf-ocr]"
# 还需安装平台对应的 PaddlePaddle CPU/GPU 包

export ONEBOOKWIKI_PDF_OCR_DET_MODEL=/path/to/PaddleOCR/PP-OCRv5_mobile_det
export ONEBOOKWIKI_PDF_OCR_REC_MODEL=/path/to/PaddleOCR/PP-OCRv5_mobile_rec

onebookwiki-build scan.pdf ./books/my-book --pdf-ocr assist --provider openai-compatible --model gpt-4o-mini
```

OCR 仅在 PDF 原生文字层缺失时辅助结构判定，不会替换原始章节文本。Windows 上 PaddlePaddle 安装较复杂，推荐用 Docker 或 WSL2。

---

## 设计

默认向量后端是本地的 `BAAI/bge-m3`（通过 SentenceTransformers）；使用 `--backend lexical` 进行无依赖的关键词检索，或 `--backend modelscope` 使用远程嵌入服务。查询可融合词汇和向量候选结果，使用倒数排名融合（RRF），然后在上下文组装前应用本地查询感知重排序器。

## 本地 BGE-M3 嵌入与 PDF

安装本地嵌入依赖，下载一次 `BAAI/bge-m3` 或将配置指向本地模型目录，然后构建向量索引。SentenceTransformers 默认自动选择设备；通过环境变量覆盖：

```bash
pip install -e ".[local-embeddings]"

# Linux / macOS
export ONEBOOKWIKI_BGE_M3_MODEL=BAAI/bge-m3
export ONEBOOKWIKI_BGE_M3_DEVICE=cuda

# Windows PowerShell
$env:ONEBOOKWIKI_BGE_M3_MODEL = "BAAI/bge-m3"
$env:ONEBOOKWIKI_BGE_M3_DEVICE = "cuda"

onebookwiki-ingest-pdf book.pdf ./books/my-book --pages-per-chapter 25
onebookwiki-ingest index ./books/my-book
onebookwiki-generate all ./books/my-book \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
```

BGE-M3 向量持久化在 `<book>/.onebookwiki/vectors.json`；仅当原始内容、分块配置、后端和模型全部匹配时才复用未变章节。默认分块配置为中文/CJK `400/60/520` 令牌，英文 `500/75/650`（目标/重叠/硬上限）。修改分块配置、切换后端或更改模型路径后需重新运行索引命令。

## ModelScope 嵌入

使用可选远程 ModelScope 后端，安装云集成，在源码控制之外设置 token：

```bash
pip install -e ".[cloud]"

# Linux / macOS
export MODELSCOPE_API_KEY=your-token

# Windows PowerShell
$env:MODELSCOPE_API_KEY = "your-token"

onebookwiki-ingest-pdf book.pdf ./books/my-book --pages-per-chapter 25
onebookwiki-ingest index ./books/my-book --backend modelscope
onebookwiki-generate all ./books/my-book --embedding-backend modelscope \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
```

云后端将变更的分块文本发送到 `https://api-inference.modelscope.cn/v1`，使用 `Qwen/Qwen3-Embedding-8B`。Token 仅从 `MODELSCOPE_API_KEY`（或 `ONEBOOKWIKI_EMBEDDING_API_KEY`）读取，绝不存储在 manifest、日志或源码文件中。复制 `.env.example` 查看变量名称。

## 源格式导入

所有支持的源格式在解析后共享一个结构化的物化合约：原始章节、schema-v3 `source.json`、源单元定位器、分块证据定位器、索引、生成、渲染和检查。PDF 保留其原生布局/OCR 感知路径；EPUB 保留书脊、目录、href 和片段溯源。

| 输入 | 解析器与主定位器 | 安装 / 限制 |
| --- | --- | --- |
| PDF | 原生 PDF 布局，物理页面 | `.[cloud]`（PyMuPDF） |
| EPUB | 标准库 ZIP/XML 书脊和目录 | 无额外依赖 |
| HTML/HTM | 标准库 `HTMLParser`，锚点/块 | 无额外依赖 |
| TXT | 确定性标题和源行 | `.[text]` 改进旧编码检测 |
| DOCX | 有序段落/表格和标题样式 | `.[docx]` |
| DOC | 进程内纯文本提取，推断段落 | `.[doc]`；不保留布局和表格 |
| MOBI/AZW/AZW3 | 无 DRM 提取的 EPUB/HTML 载荷 | `.[kindle]` 安装 GPL-3.0-only `mobi`；使用前请评估该许可证。不支持 DRM 移除 |

```bash
onebookwiki-build book.epub ./books/my-book \
  --provider openai-compatible --model gpt-4o-mini --database ./onebookwiki.db --book-id 1
onebookwiki-build book.pdf ./books/my-book --pages-per-chapter 25 \
  --provider openai-compatible --model gpt-4o-mini --database ./onebookwiki.db --book-id 1
onebookwiki-build book.txt ./books/my-book --backend lexical --provider none --dry-run
onebookwiki-build book.docx ./books/my-book --backend lexical --provider none --dry-run

# 分步执行
onebookwiki-ingest-epub book.epub ./books/my-book
onebookwiki-ingest-pdf book.pdf ./books/my-book --pages-per-chapter 25
onebookwiki-ingest index ./books/my-book --backend lexical
onebookwiki-generate all ./books/my-book --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
```

使用 `--source-format` 仅用于无扩展名或命名错误的文件。PDF 结构/OCR 选项在其他格式会被拒绝。`--keep-front-matter` 仅限于 EPUB 和无 DRM 的 Kindle 输入。

## 维基生成与对话

索引完成后，配置的 OpenAI 兼容提供商生成结构化陈述（statement）和综合内容（composition），先原子化发布为不可变的 Grounded v2 知识版本，再渲染为确定性的 Markdown/JSON 投影。可选的 `cloud` 安装包含 `json-repair`，可在验证前修复常见的模型 JSON 格式错误。

`onebookwiki-generate` 有 5 个子命令：`chapter`、`book`、`all`、`resume`、`status`。只有 `all` 和 `resume` 会发布并渲染一个完整的可对外发布版本；`chapter` 和 `book` 仅生成/检查点化草稿知识，从不发布、渲染或构建向量。`all`/`resume` 在非 `--dry-run` 时必须提供 `--database` 和 `--book-id`：

```bash
# 发布一个完整版本：注册证据 → 发布数据库快照 → 生成 → 渲染 → （可选）构建向量
onebookwiki-generate all ./books/my-book \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1

onebookwiki-generate status ./books/my-book
onebookwiki-generate resume ./books/my-book \
  --database ./onebookwiki.db --book-id 1
onebookwiki-cost ./books/my-book --json
```

使用 `--dry-run` 构建有界的可恢复计划而不调用模型、不要求数据库、不发布、不渲染。检查点和生成的 JSON 工件位于 `.onebookwiki/` 下；使用量以追加模式记录在 `usage.jsonl` 中。

对话（Chat）不是独立 CLI，而是通过 FastAPI 服务（见「后端 API」）异步处理：提交问题后由 `server/chat_worker.py` 检索已发布知识版本中的陈述/综合内容，仅在证据充分时生成回答，并且只引用该书当前健康的 Grounded v2 版本；证据不足时会拒绝回答而不是编造。

唯一支持的引用形式是不可变的 `onebookwiki://evidence/evr-<32 位十六进制>` 链接，指向 `wiki/evidence.json` 中已发布的证据版本；不存在、也不解析任何旧式章节内引用标记（如 `C5E8`）。

## 一键构建

```bash
onebookwiki-build book.epub ./books/my-book \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
onebookwiki-build book.pdf ./books/my-book --pages-per-chapter 25 \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
onebookwiki-build book.epub ./books/my-book --resume \
  --provider openai-compatible --model gpt-4o-mini \
  --database ./onebookwiki.db --book-id 1
# 使用 --backend lexical 无需本地模型，或 --backend modelscope 使用远程嵌入服务
# 省略 --database/--book-id 时必须加 --dry-run（仅导入/索引/生成计划，不发布/渲染）
```

构建后的目录结构：

```text
raw/chapters/                 不可变源文件
.onebookwiki/                 可重建的证据登记、分块、向量、检查点、使用量
wiki/book.md                  已发布知识版本的书籍概览（含引用）
wiki/index.md                 章节阅读导航
wiki/chapters/*.md            按章节路由的已发布综合内容页面
wiki/knowledge/*.md           跨章节/全书范围的已发布综合内容页面
wiki/structure.json           规范化的页面/章节图，含 bookRevisionId
wiki/evidence.json            已发布知识版本的证据索引，含 bookRevisionId
```

`structure.json`/`evidence.json` 中的 `bookRevisionId` 必须与数据库中当前健康的活跃版本一致；`wiki/` 内容永远对应且仅对应这一个已发布版本。

## 命令参考

```bash
onebookwiki-build        # 完整管线：导入 → 索引 → 生成 → 发布 → 渲染 → 检查
onebookwiki-generate     # chapter/book/all/resume/status；只有 all/resume 发布并渲染
onebookwiki-ingest       # 构建索引（词汇或向量）
onebookwiki-ingest-epub  # EPUB 导入
onebookwiki-ingest-pdf   # PDF 导入
onebookwiki-check        # 一致性检查（校验已发布 Grounded v2 投影）
onebookwiki-cost         # 成本报告
```

所有命令支持 `--help` 查看详细参数。对话（Chat）通过 FastAPI 服务提供，没有独立的 `onebookwiki-chat`/`onebookwiki-query` CLI。

---

## 配置

### 环境变量

复制 `.env.example` 到 `.env` 并编辑。主要变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ONEBOOKWIKI_LLM_PROVIDER` | LLM 提供商 | `openai-compatible` |
| `ONEBOOKWIKI_LLM_API_KEY` | LLM API 密钥 | — |
| `ONEBOOKWIKI_LLM_MODEL` | LLM 模型名称 | `gpt-4o-mini` |
| `ONEBOOKWIKI_LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `ONEBOOKWIKI_LLM_TIMEOUT` | 请求超时(秒) | `60` |
| `ONEBOOKWIKI_LLM_CONCURRENCY` | 并发数 | `1` |
| `ONEBOOKWIKI_BGE_M3_MODEL` | BGE-M3 模型路径 | `BAAI/bge-m3` |
| `ONEBOOKWIKI_BGE_M3_DEVICE` | 推理设备 | 自动 |
| `ONEBOOKWIKI_EMBEDDING_BASE_URL` | 嵌入 API 地址 | ModelScope 默认 |
| `MODELSCOPE_API_KEY` | ModelScope API 密钥 | — |
| `ONEBOOKWIKI_PORT` | 服务端口 | `8000` |
| `ONEBOOKWIKI_HOST` | 服务地址 | `0.0.0.0` |
| `ONEBOOKWIKI_BOOKS_ROOT` | 书籍存储目录 | `./books` |

---

## 前端阅读器

前端位于 `frontend/`，是一个独立的 React/Vite 阅读器，连接 FastAPI 后端。

### 开发模式

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

浏览器访问：

```text
http://127.0.0.1:5173/book                  # 阅读首页
http://127.0.0.1:5173/book/1                # 指定数字书籍 ID
http://127.0.0.1:5173/book/1?page=...       # 直接打开特定页面
http://127.0.0.1:5173/admin                 # 管理后台（上传、处理状态）
```

### 生产构建

```bash
cd frontend
npm run build   # 输出到 frontend/dist/
npm run preview -- --host 127.0.0.1
```

部署时需要同时发布 `frontend/dist/` 和后端 API 服务。通过 FastAPI 的 `/book` 静态挂载或 Nginx 反向代理提供书籍文件。

更多路由和部署配置详见 [`frontend/README.md`](frontend/README.md)。

---

## 后端 API

启动 FastAPI 服务：

```bash
# Linux / macOS
./start.sh

# Windows PowerShell
.\start.ps1

# Docker
docker compose up -d
```

服务端提供以下功能：

- `POST /api/upload` — 上传电子书，自动触发后台处理管线
- `GET /api/books` — 列出所有书籍
- `GET /api/books/{id}/status` — 查看处理进度
- `GET /api/books/{id}/cover` — 获取封面图片
- `POST /api/books/{id}/process` — 重试失败的处理任务
- `GET /api/health` — 健康检查
- `GET /docs` — OpenAPI 交互文档

处理阶段：`queued → importing → indexing → generating → rendering → complete`（失败则进入 `failed`）。

---

## 平台说明

### Linux
```bash
sudo apt install python3 python3-pip
pip install -e ".[server,rag,imports]"
# PDF OCR 可选：sudo apt install libgomp1
```

### macOS
```bash
brew install python@3.12
pip install -e ".[server,rag,imports]"
# Apple Silicon 用户注意 PyTorch 版本选择
```

### Windows
```powershell
# 推荐使用 Python 3.10+（从 python.org 安装）
pip install -e ".[server,rag,imports]"
# 重依赖（sentence-transformers、PaddleOCR）推荐通过 Docker 或 WSL2 运行
```

**Docker 推荐用于所有平台：**
```bash
docker compose up -d
```

---

## 测试

后端（在 `onebookwiki/` 下运行）：

```bash
python -m unittest discover -s tests -v
```

`server/test_import.py` 和 `server/test_fastapi.py` 依赖有状态的外部环境，不包含在上述常规套件中。

前端（在 `frontend/` 下运行）：

```bash
npm install
npm test
npm run build
```

## Skill

将 `SKILL.md` 和 `references/` 复制到 Agent Skills 兼容目录。Skill 定义了 Ingest、Query、Review 和 Lint 行为；CLI 提供确定性索引和检查。

## 许可证

MIT License — 详见 [LICENSE](LICENSE)。

注意：可选的 `.[kindle]` 依赖包含 GPL-3.0-only 的 `mobi` 包。仅在评估并接受该许可证后安装。
