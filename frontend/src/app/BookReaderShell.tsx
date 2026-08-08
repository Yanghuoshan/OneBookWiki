import { useEffect, useMemo, useState } from 'react';
import { formatConfidence, formatPdfRange, formatSourceType, loadBook, loadPage, resolveBookBase } from '../data/bookLoader';
import type { EvidenceIndex, EvidenceRecord, SourceOutlineNode, WikiPage, WikiStructure } from '../types/wiki';
import PageTree from '../components/PageTree';
import MarkdownReader from '../components/MarkdownReader';
import SourcePanel from '../components/SourcePanel';

type Props = { initialStructure?: WikiStructure; initialEvidence?: EvidenceIndex };

function sourceNodesByPageId(nodes: SourceOutlineNode[]): Map<string, SourceOutlineNode> {
  const result = new Map<string, SourceOutlineNode>();
  const visit = (node: SourceOutlineNode) => {
    node.pageIds.forEach(pageId => result.set(pageId, node));
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return result;
}

function pageMeta(page: WikiPage, sourceNode?: SourceOutlineNode): string[] {
  const breadcrumb = page.breadcrumb?.length ? page.breadcrumb.join(' › ') : sourceNode?.breadcrumb.join(' › ');
  const sourceType = page.sourceKind || sourceNode?.kind;
  const range = formatPdfRange(page.physicalPageStart, page.physicalPageEnd);
  const confidence = formatConfidence(sourceNode?.confidence);
  const part = (page.partCount || 1) > 1 ? `Part ${page.part || 1} of ${page.partCount}` : undefined;
  return [breadcrumb, formatSourceType(sourceType), range, part, confidence].filter((value): value is string => Boolean(value));
}

export default function BookReaderShell({ initialStructure, initialEvidence }: Props) {
  const [structure, setStructure] = useState<WikiStructure | null>(initialStructure || null);
  const [evidence, setEvidence] = useState<EvidenceIndex>(initialEvidence || { evidence: {} });
  const [current, setCurrent] = useState<WikiPage | null>(null);
  const [content, setContent] = useState('');
  const [source, setSource] = useState<EvidenceRecord | null>(null);
  const [loading, setLoading] = useState(!initialStructure);
  const [error, setError] = useState<string | null>(null);
  const bookBase = useMemo(() => resolveBookBase(), []);

  useEffect(() => {
    if (structure) return;
    loadBook(bookBase).then(({ structure: loadedStructure, evidence: loadedEvidence }) => {
      setStructure(loadedStructure);
      setEvidence(loadedEvidence);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载书籍数据')).finally(() => setLoading(false));
  }, [bookBase, structure]);

  const defaultPage = useMemo(() => {
    if (!structure) return null;
    const routeId = new URLSearchParams(window.location.search).get('page');
    return structure.pages.find(page => page.id === routeId) || structure.pages[0] || null;
  }, [structure]);

  const pagesByPath = useMemo(() => new Map(structure?.pages.map(page => [page.path, page]) || []), [structure]);
  const sourcePageNodes = useMemo(() => sourceNodesByPageId(structure?.sourceOutline || []), [structure]);

  useEffect(() => {
    if (!current && defaultPage) setCurrent(defaultPage);
  }, [current, defaultPage]);

  useEffect(() => {
    if (!current) return;
    setContent('');
    loadPage(current, bookBase).then(setContent).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载页面内容'));
    const url = new URL(window.location.href);
    url.searchParams.set('page', current.id);
    window.history.replaceState({}, '', url);
  }, [bookBase, current]);

  if (loading) return <div className="status-screen"><div className="loader-mark">◎</div><p>正在打开 OneBookWiki…</p></div>;
  if (error && !structure) return <div className="status-screen error-screen"><h1>无法打开这本书</h1><p>{error}</p><p className="hint">当前书籍路径为 {bookBase}。请确认该路径下存在 wiki/structure.json。</p></div>;
  if (!structure) return null;

  const meta = current ? pageMeta(current, sourcePageNodes.get(current.id)) : [];

  return (
    <div className="reader-shell">
      <header className="topbar">
        <div className="brand-mark">ONE<span>BOOK</span></div>
        <div className="book-heading"><span className="eyebrow">EVIDENCE-GROUNDED READING</span><h1>{structure.title}</h1></div>
        <div className="topbar-meta">{structure.pages.length} pages</div>
      </header>
      <main className="reader-grid">
        <aside className="navigation-pane"><p className="pane-label">READING MAP</p><p className="description">{structure.description || '基于原始文本生成的证据导向阅读地图。'}</p><PageTree structure={structure} currentPageId={current?.id} onSelect={setCurrent} /></aside>
        <section className="content-pane" aria-live="polite">
          {current ? <><div className="content-meta">{meta.map(item => <span key={item}>{item}</span>)}</div><div className="markdown-body"><MarkdownReader content={content || '正在加载页面…'} evidence={evidence} pagePath={current.path} pagesByPath={pagesByPath} onCitation={setSource} onPageLink={setCurrent} /></div></> : <div className="empty-state"><h2>选择一个页面</h2><p>从左侧阅读地图开始浏览。</p></div>}
        </section>
        {source && <SourcePanel record={source} onClose={() => setSource(null)} />}
      </main>
    </div>
  );
}
