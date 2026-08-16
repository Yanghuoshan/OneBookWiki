import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { loadBook, resolveBookBase } from '../data/bookLoader';
import { appendConversationTurn, getConversation } from '../data/chatLoader';
import type { ChatConversation, EvidenceIndex, EvidenceRecord, WikiPage, WikiStructure } from '../types/wiki';
import MarkdownReader from '../components/MarkdownReader';
import PageTree from '../components/PageTree';
import SourcePanel from '../components/SourcePanel';
import ChatComposer from '../components/ChatComposer';

type Props = { bookId: number; conversationId: string };

const NAVIGATION_WIDTH = 280;
const SOURCE_PANEL_DEFAULT_WIDTH = 320;
const SOURCE_PANEL_MIN_WIDTH = 240;
const SOURCE_PANEL_MAX_WIDTH = 760;
const CONTENT_MIN_WIDTH = 360;
const POLL_INTERVAL_MS = 1000;
const activeStatuses = new Set(['queued', 'retrieving', 'generating']);

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum);
}

export default function ChatPage({ bookId, conversationId }: Props) {
  const [structure, setStructure] = useState<WikiStructure | null>(null);
  const [evidence, setEvidence] = useState<EvidenceIndex>({ evidence: {} });
  const [conversation, setConversation] = useState<ChatConversation | null>(null);
  const [source, setSource] = useState<EvidenceRecord | null>(null);
  const [sourcePanelWidth, setSourcePanelWidth] = useState(SOURCE_PANEL_DEFAULT_WIDTH);
  const [gridWidth, setGridWidth] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const gridRef = useRef<HTMLElement>(null);
  const bookBase = useMemo(() => resolveBookBase(`/book/${bookId}/ask/${conversationId}`), [bookId, conversationId]);

  const loadConversation = useCallback(async () => {
    const loadedConversation = await getConversation(conversationId);
    if (loadedConversation.book_id !== bookId) {
      throw new Error('Conversation does not belong to this book');
    }
    setConversation(loadedConversation);
  }, [bookId, conversationId]);

  useEffect(() => {
    let disposed = false;
    Promise.all([loadBook(bookBase), getConversation(conversationId)])
      .then(([loadedBook, loadedConversation]) => {
        if (loadedConversation.book_id !== bookId) throw new Error('Conversation does not belong to this book');
        if (disposed) return;
        setStructure(loadedBook.structure);
        setEvidence(loadedBook.evidence);
        setConversation(loadedConversation);
        setError(null);
      })
      .catch(reason => {
        if (!disposed) setError(reason instanceof Error ? reason.message : '无法加载对话');
      });
    return () => { disposed = true; };
  }, [bookBase, bookId, conversationId]);

  const active = conversation?.turns.some(turn => activeStatuses.has(turn.status)) || false;
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => {
      loadConversation()
        .then(() => setError(null))
        .catch(reason => setError(reason instanceof Error ? reason.message : '无法刷新对话'));
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [active, loadConversation]);

  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const updateGridWidth = () => setGridWidth(grid.clientWidth);
    updateGridWidth();
    const observer = new ResizeObserver(updateGridWidth);
    observer.observe(grid);
    return () => observer.disconnect();
  }, [structure]);

  if (error && !conversation) return <div className="status-screen error-screen"><h1>无法打开回答</h1><p>{error}</p></div>;
  if (!conversation || !structure) return <div className="status-screen"><div className="loader-mark">◎</div><p>正在加载回答...</p></div>;

  const pagesByPath = new Map(structure.pages.map(page => [page.path, page]));
  const sourcePanelMaximumWidth = Math.max(
    SOURCE_PANEL_MIN_WIDTH,
    Math.min(SOURCE_PANEL_MAX_WIDTH, gridWidth - NAVIGATION_WIDTH - CONTENT_MIN_WIDTH),
  );
  const effectiveSourcePanelWidth = clamp(sourcePanelWidth, SOURCE_PANEL_MIN_WIDTH, sourcePanelMaximumWidth);
  const gridStyle = { '--source-panel-width': `${effectiveSourcePanelWidth}px` } as CSSProperties;

  const openWikiPage = (page: WikiPage) => {
    window.location.assign(`/book/${bookId}?page=${encodeURIComponent(page.id)}`);
  };

  const submitFollowUp = async (question: string) => {
    await appendConversationTurn(conversationId, question);
    await loadConversation();
  };

  return (
    <div className="reader-shell chat-page-shell">
      <header className="topbar">
        <div className="brand-mark" onClick={() => { window.location.assign('/'); }} style={{ cursor: 'pointer' }} title="返回首页">ONE<span>BOOK</span></div>
        <div className="book-heading"><span className="eyebrow">EVIDENCE-GROUNDED ANSWERS</span><h1>{conversation.book_title}</h1></div>
        <div className="topbar-meta">Conversation</div>
      </header>
      <main ref={gridRef} className="reader-grid chat-reader-grid" data-source-open={Boolean(source)} style={gridStyle}>
        <aside className="navigation-pane">
          <p className="pane-label">READING MAP</p>
          <p className="description">回答会重新检索本书中的可验证证据。</p>
          <PageTree structure={structure} onSelect={openWikiPage} />
        </aside>
        <section className="content-pane chat-content-pane" aria-live="polite">
          <div className="chat-scrollable-area">
            <div className="chat-page-header">
              <a href={`/book/${bookId}`} className="chat-back-link">返回 wiki</a>
              <h2>回答</h2>
              <p>此页面只对应当前 URL 中的对话。</p>
            </div>
            {error && <p className="chat-refresh-error" role="alert">{error}</p>}
            <div className="chat-thread">
              {conversation.turns.map(turn => (
                <article className={`chat-turn chat-turn--${turn.status}`} key={turn.id}>
                  <div className="chat-question"><span className="chat-role">问题 {turn.turn_no}</span><p>{turn.question}</p></div>
                  <div className="chat-answer">
                    {activeStatuses.has(turn.status) ? (
                      <p className="chat-pending">正在检索书籍证据并生成回答...</p>
                    ) : turn.answer ? (
                      <div className="markdown-body"><MarkdownReader content={turn.answer} evidence={evidence} pagePath="chat-answer.md" pagesByPath={pagesByPath} onCitation={setSource} onPageLink={openWikiPage} /></div>
                    ) : (
                      <p className="chat-result-error">{turn.refusal_code ? `证据不足：${turn.refusal_code}` : turn.error_message || '该问题未能生成回答。'}</p>
                    )}
                  </div>
                </article>
              ))}
            </div>
          </div>
          <div className="chat-composer-container">
            <ChatComposer onSubmit={submitFollowUp} disabled={active} compact />
          </div>
        </section>
        {source && <SourcePanel
          record={source}
          width={effectiveSourcePanelWidth}
          minWidth={SOURCE_PANEL_MIN_WIDTH}
          maxWidth={sourcePanelMaximumWidth}
          onWidthChange={setSourcePanelWidth}
          onClose={() => setSource(null)}
        />}
      </main>
    </div>
  );
}
