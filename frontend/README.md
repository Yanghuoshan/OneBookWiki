# OneBookWiki Reader

独立的 React/Vite 静态阅读器。它读取一个 book root 下的生成产物：

- `wiki/structure.json`
- `wiki/*.md`
- `wiki/evidence.json`

开发时设置 `VITE_ONEBOOKWIKI_BASE_URL` 指向 book root 的 HTTP 路径。例如从仓库根目录运行一个静态服务器后：

```text
VITE_ONEBOOKWIKI_BASE_URL=/books/sample-book npm run dev
```

生产构建：

```text
npm install
npm run build
```

页面采用左侧阅读地图、中间 Markdown 正文、按需来源面板的布局。PDF 来源显示 PDF 物理页码；EPUB 来源显示章节、spine 和 href。旧的 `C5E8` 引用会作为兼容输入解析，但界面优先显示可读来源标签。
