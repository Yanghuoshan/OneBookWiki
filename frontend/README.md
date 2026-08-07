# OneBookWiki Reader

独立的 React/Vite 静态阅读器。每本书读取其 book root 下的生成产物：

- `wiki/structure.json`
- `wiki/*.md`
- `wiki/evidence.json`

## URL 选择书籍

开发服务器默认从 `onebookwiki/books/` 读取书籍，并支持直接修改网址切换书籍：

```text
http://127.0.0.1:5173/book
http://127.0.0.1:5173/book/zhenshi
http://127.0.0.1:5173/book/another-book?page=chapter-01
```

- `/book` 和 `/book/wiki/...` 继续指向默认书籍 `books/zhenshi`。
- `/book/<book-id>` 指向 `books/<book-id>`。
- `<book-id>` 只能包含字母、数字、`_` 和 `-`，并且对应 `onebookwiki/books/` 下的目录。
- `?page=<page-id>` 只在当前书籍的 `wiki/structure.json` 中解析。
- 每本书必须先生成 `wiki/structure.json`、Markdown 页面和 `wiki/evidence.json`。

启动开发服务器：

```powershell
cd D:\workspace\deepwiki4book\onebookwiki\frontend
npm install
npm run dev -- --host 127.0.0.1
```

生产构建和本地预览：

```powershell
npm run build
npm run preview -- --host 127.0.0.1
```

Vite dev 和 preview 都支持上述 `/book`、`/book/<book-id>` 路径。

## 非标准部署路径

如果阅读器被部署到不是 `/book` 的路径，可以设置：

```text
VITE_ONEBOOKWIKI_BASE_URL=/books/sample-book npm run dev
```

在标准 `/book` 路由下，URL 中的书籍路径优先于这个环境变量。

## 生产静态部署

`npm run build` 只生成前端 `dist/`，不会自动把 `books/` 复制进构建目录。生产服务器需要：

1. 发布 `frontend/dist` 和所有书籍的 `books/<book-id>/wiki/` 静态文件。
2. 将 `/book/<book-id>/wiki/...` 映射到对应的书籍目录。
3. 保留 `/book/wiki/...` 到默认书籍的兼容映射。
4. 将 `/book`、`/book/` 和 `/book/<book-id>` 的 HTML 导航请求重写到 `dist/index.html`。
5. 不要把 `/book/<book-id>/wiki/...` 重写为 HTML；这些请求必须返回真实 JSON 或 Markdown 文件。
6. 保留查询字符串，以便 `?page=` 刷新后仍然有效。

没有 SPA fallback 的静态主机无法直接刷新 `/book/<book-id>`，需要配置等价的 rewrite 规则。

页面采用左侧阅读地图、中间 Markdown 正文、按需来源面板的布局。PDF 来源显示 PDF 物理页码；EPUB 来源显示章节、spine 和 href。旧的 `C5E8` 引用会作为兼容输入解析，但界面优先显示可读来源标签。
