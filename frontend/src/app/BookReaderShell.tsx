import { useEffect, useMemo, useState } from 'react';
import { loadBook, loadPage } from '../data/bookLoader';
import type { EvidenceIndex, EvidenceRecord, WikiPage, WikiStructure } from '../types/wiki';
import PageTree from '../components/PageTree';
import MarkdownReader from '../components/MarkdownReader';
import SourcePanel from '../components/SourcePanel';

type Props = { initialStructure?: WikiStructure; initialEvidence?: EvidenceIndex };

export default function BookReaderShell({ initialStructure, initialEvidence }: Props) {
  const [structure, setStructure] = useState<WikiStructure | null>(initialStructure || null);
  const [evidence, setEvidence] = useState<EvidenceIndex>(initialEvidence || { evidence: {} });
  const [current, setCurrent] = useState<WikiPage | null>(null);
  const [content, setContent] = useState('');
  const [source, setSource] = useState<EvidenceRecord | null>(null);
  const [loading, setLoading] = useState(!initialStructure);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (structure) return;
    loadBook().then(({ structure: loadedStructure, evidence: loadedEvidence }) => {
      setStructure(loadedStructure);
      setEvidence(loadedEvidence);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载书籍数据')).finally(() => setLoading(false));
  }, [structure]);

  const defaultPage = useMemo(() => {
    if (!structure) return null;
    const routeId = new URLSearchParams(window.location.search).get('page');
    return structure.pages.find(page => page.id === routeId) || structure.pages[0] || null;
  }, [structure]);

  useEffect(() => {
    if (!current && defaultPage) setCurrent(defaultPage);
  }, [current, defaultPage]);

  useEffect(() => {
    if (!current) return;
    setContent('');
    loadPage(current).then(setContent).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : '无法加载页面内容'));
    const url = new URL(window.location.href);
    url.searchParams.set('page', current.id);
    window.history.replaceState({}, '', url);
  }, [current]);

  if (loading) return <div className="status-screen"><div className="loader-mark">◎</div><p>正在打开 OneBookWiki…</p></div>;
  if (error && !structure) return <div className="status-screen error-screen"><h1>无法打开这本书</h1><p>{error}</p><p className="hint">请设置 VITE_ONEBOOKWIKI_BASE_URL，指向包含 wiki/structure.json 的 book root。</p></div>;
  if (!structure) return null;

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
          {current ? <><div className="content-meta"><span>{current.kind}{current.chapter ? ` · chapter ${current.chapter}` : ''}</span><span>{current.path}</span></div><div className="markdown-body"><MarkdownReader content={content || '正在加载页面…'} evidence={evidence} onCitation={setSource} /></div></> : <div className="empty-state"><h2>选择一个页面</h2><p>从左侧阅读地图开始浏览。</p></div>}
        </section>
        {source && <SourcePanel record={source} onClose={() => setSource(null)} />}
      </main>
    </div>
  );
}
