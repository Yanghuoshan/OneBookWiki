import { useCallback, useEffect, useState } from 'react';
import type { AdminStats, BookSummary, OperationLog, TokenBookEntry, TokenUsageDetail, TokenUsageSummary } from '../types/wiki';
import {
  deleteBook,
  fetchAdminStats,
  fetchAllTokenUsage,
  fetchBookList,
  fetchBookTokenUsage,
  fetchOperations,
  updateBookMetadata,
} from '../data/bookListLoader';

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function fmtDate(iso: string): string {
  if (!iso) return '';
  return iso.replace('T', ' ').replace(/:\d{2}Z$/, '');
}

const PHASE_LABELS: Record<string, string> = {
  empty: 'Empty',
  queued: 'Queued',
  importing: 'Importing',
  indexing: 'Indexing',
  generating: 'Generating',
  rendering: 'Rendering',
  complete: 'Complete',
  failed: 'Failed',
  pending: 'Pending',
};

const OP_LABELS: Record<string, string> = {
  upload: 'Upload',
  import: 'Import',
  index: 'Index',
  generate: 'Generate',
  render: 'Render',
  retry: 'Retry',
  delete: 'Delete',
  update_metadata: 'Edit Metadata',
  pipeline: 'Pipeline',
};

// ── AdminPage ────────────────────────────────────────────────────────────────

type AdminTab = 'dashboard' | 'books' | 'operations' | 'tokens';

export default function AdminPage() {
  const [activeTab, setActiveTab] = useState<AdminTab>('dashboard');
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [stats, setStats] = useState<AdminStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [bookList, adminStats] = await Promise.all([
        fetchBookList(),
        fetchAdminStats(),
      ]);
      setBooks(bookList);
      setStats(adminStats);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const tabs: { key: AdminTab; label: string }[] = [
    { key: 'dashboard', label: '概览' },
    { key: 'books', label: '书籍管理' },
    { key: 'operations', label: '操作日志' },
    { key: 'tokens', label: 'Token 用量' },
  ];

  return (
    <div className="admin-page">
      <header className="topbar">
        <div className="brand-mark" style={{ cursor: 'pointer' }} onClick={() => { window.location.href = '/'; }}>
          ONE<span>BOOK</span> <span style={{ color: '#9a8d79' }}>| ADMIN</span>
        </div>
        <div className="book-heading">
          <span className="eyebrow">ADMINISTRATION</span>
          <h1>Dashboard</h1>
        </div>
        <div className="topbar-meta">
          <a href="/" className="admin-back-link" onClick={(e) => { e.preventDefault(); window.location.href = '/'; }}>
            ← Back to Library
          </a>
        </div>
      </header>
      <nav className="admin-tabs">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            className={`admin-tab${activeTab === tab.key ? ' admin-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <main className="admin-main">
        {loading && <div className="status-screen"><div className="loader-mark">⏳</div><p>Loading admin data…</p></div>}
        {error && !loading && (
          <div className="status-screen error-screen">
            <h1>Loading Failed</h1>
            <p className="hint">{error}</p>
            <button className="btn btn--primary" onClick={loadData}>Retry</button>
          </div>
        )}
        {!loading && !error && (
          <>
            {activeTab === 'dashboard' && <DashboardOverview stats={stats!} books={books} />}
            {activeTab === 'books' && <BookManagement books={books} onBooksChanged={loadData} />}
            {activeTab === 'operations' && <OperationLogs books={books} />}
            {activeTab === 'tokens' && <TokenUsage books={books} />}
          </>
        )}
      </main>
    </div>
  );
}

// ── DashboardOverview ────────────────────────────────────────────────────────

function DashboardOverview({ stats, books }: { stats: AdminStats; books: BookSummary[] }) {
  return (
    <div>
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-card-number">{stats.total_books}</div>
          <div className="stat-card-label">Total Books</div>
          <div className="stat-card-detail">{stats.complete_books} complete · {stats.failed_books} failed · {stats.processing_books} processing</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-number">{fmtNum(stats.total_pages)}</div>
          <div className="stat-card-label">Total Pages</div>
          <div className="stat-card-detail">{fmtNum(stats.total_chapters)} chapters</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-number">{fmtNum(stats.total_tokens)}</div>
          <div className="stat-card-label">Total Tokens</div>
          <div className="stat-card-detail">Across all books</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-number">{stats.recent_operations_24h}</div>
          <div className="stat-card-label">Ops (24h)</div>
          <div className="stat-card-detail">{stats.processing_books} books in progress</div>
        </div>
      </div>

      <h2 className="section-title">Recently Updated</h2>
      <table className="admin-table">
        <thead>
          <tr>
            <th>Title</th>
            <th>Phase</th>
            <th>Pages</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {books.slice(0, 5).map((b) => (
            <tr key={b.id}>
              <td>{b.title}</td>
              <td><span className={`book-card-phase${b.phase === 'failed' ? ' book-card-phase--failed' : ''}`}>{PHASE_LABELS[b.phase] || b.phase}</span></td>
              <td>{b.page_count ?? '—'}</td>
              <td>{fmtDate(b.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── BookManagement ───────────────────────────────────────────────────────────

function BookManagement({ books, onBooksChanged }: { books: BookSummary[]; onBooksChanged: () => void }) {
  const [filterPhase, setFilterPhase] = useState('');
  const [search, setSearch] = useState('');
  const [editing, setEditing] = useState<BookSummary | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<BookSummary | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const filtered = books.filter((b) => {
    if (filterPhase && b.phase !== filterPhase) return false;
    if (search && !b.title.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  return (
    <div>
      <div className="admin-filters">
        <select value={filterPhase} onChange={(e) => setFilterPhase(e.target.value)}>
          <option value="">All Phases</option>
          <option value="complete">Complete</option>
          <option value="failed">Failed</option>
          <option value="pending">Pending</option>
          <option value="queued">Queued</option>
          <option value="importing">Importing</option>
          <option value="indexing">Indexing</option>
          <option value="generating">Generating</option>
          <option value="rendering">Rendering</option>
        </select>
        <input
          type="text"
          placeholder="Search by title…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <span style={{ color: '#746d62', fontSize: 12 }}>{filtered.length} of {books.length} books</span>
      </div>

      {actionError && (
        <div className="upload-message upload-message--error" style={{ marginBottom: 16 }}>
          {actionError}
          <button className="btn btn--small" style={{ marginLeft: 12 }} onClick={() => setActionError(null)}>Dismiss</button>
        </div>
      )}

      <div style={{ overflowX: 'auto' }}>
        <table className="admin-table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Author</th>
              <th>Format</th>
              <th>Phase</th>
              <th>Pages</th>
              <th>Chapters</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((b) => (
              <tr key={b.id}>
                <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.title}</td>
                <td style={{ maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{b.author || '—'}</td>
                <td style={{ fontSize: 11 }}>{b.format || '—'}</td>
                <td><span className={`book-card-phase${b.phase === 'failed' ? ' book-card-phase--failed' : ''}`}>{PHASE_LABELS[b.phase] || b.phase}</span></td>
                <td>{b.page_count ?? '—'}</td>
                <td>{b.chapter_count ?? '—'}</td>
                <td style={{ fontSize: 11 }}>{fmtDate(b.created_at)}</td>
                <td>
                  <div className="actions-cell">
                    <button className="btn btn--small btn--primary" onClick={() => setEditing(b)}>Edit</button>
                    <button className="btn btn--small btn--danger" onClick={() => setDeleteTarget(b)}>Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Edit Modal */}
      {editing && (
        <EditBookModal
          book={editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); onBooksChanged(); }}
          onError={(msg) => { setActionError(msg); setEditing(null); }}
        />
      )}

      {/* Delete Confirmation */}
      {deleteTarget && (
        <DeleteConfirmModal
          book={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={() => { setDeleteTarget(null); onBooksChanged(); }}
          onError={(msg) => { setActionError(msg); setDeleteTarget(null); }}
        />
      )}
    </div>
  );
}

// ── Edit Modal ───────────────────────────────────────────────────────────────

function EditBookModal({ book, onClose, onSaved, onError }: {
  book: BookSummary;
  onClose: () => void;
  onSaved: () => void;
  onError: (msg: string) => void;
}) {
  const [title, setTitle] = useState(book.title);
  const [author, setAuthor] = useState(book.author || '');
  const [description, setDescription] = useState(book.description || '');
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateBookMetadata(book.id, {
        title: title || undefined,
        author: author || undefined,
        description: description || undefined,
      });
      onSaved();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Update failed');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Edit Book — {book.id}</h3>
        <label>Title</label>
        <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
        <label>Author</label>
        <input type="text" value={author} onChange={(e) => setAuthor(e.target.value)} />
        <label>Description</label>
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Delete Confirm Modal ─────────────────────────────────────────────────────

function DeleteConfirmModal({ book, onClose, onDeleted, onError }: {
  book: BookSummary;
  onClose: () => void;
  onDeleted: () => void;
  onError: (msg: string) => void;
}) {
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await deleteBook(book.id);
      onDeleted();
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Delete Book</h3>
        <p style={{ color: '#5c554b', fontSize: 14, lineHeight: 1.6 }}>
          Are you sure you want to delete <strong>{book.title}</strong>?
          This will permanently remove all data including the book directory from disk.
        </p>
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn btn--danger" onClick={handleDelete} disabled={deleting}>
            {deleting ? 'Deleting…' : 'Delete'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── OperationLogs ────────────────────────────────────────────────────────────

function OperationLogs({ books }: { books: BookSummary[] }) {
  const [logs, setLogs] = useState<OperationLog[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [filterBookId, setFilterBookId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const limit = 50;

  const loadLogs = useCallback(async (bookId: string, off: number) => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchOperations(bookId || undefined, limit, off);
      setLogs(result.logs);
      setTotal(result.total);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load logs');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadLogs(filterBookId, offset);
  }, [filterBookId, offset, loadLogs]);

  const totalPages = Math.max(1, Math.ceil(total / limit));
  const currentPage = Math.floor(offset / limit) + 1;

  return (
    <div>
      <div className="admin-filters">
        <select value={filterBookId} onChange={(e) => { setFilterBookId(e.target.value); setOffset(0); }}>
          <option value="">All Books</option>
          {books.map((b) => (
            <option key={b.id} value={b.id}>{b.title}</option>
          ))}
        </select>
        <span style={{ color: '#746d62', fontSize: 12 }}>{total} entries</span>
      </div>

      {error && <div className="upload-message upload-message--error">{error}</div>}

      <div style={{ overflowX: 'auto' }}>
        <table className="admin-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Book</th>
              <th>Operation</th>
              <th>Status</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {loading && logs.length === 0 && (
              <tr><td colSpan={5} style={{ textAlign: 'center', color: '#746d62', padding: 24 }}>Loading…</td></tr>
            )}
            {!loading && logs.length === 0 && (
              <tr><td colSpan={5} style={{ textAlign: 'center', color: '#746d62', padding: 24 }}>No operation logs yet.</td></tr>
            )}
            {logs.map((l) => (
              <tr key={l.id}>
                <td style={{ fontSize: 11, whiteSpace: 'nowrap' }}>{fmtDate(l.created_at)}</td>
                <td style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{l.book_title || l.book_id}</td>
                <td>{OP_LABELS[l.operation] || l.operation}</td>
                <td>
                  <span style={{ color: l.status === 'failed' ? '#8b3b2b' : '#46603a', fontWeight: 600, fontSize: 11 }}>
                    {l.status === 'failed' ? 'FAILED' : 'OK'}
                  </span>
                </td>
                <td style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 12 }}>{l.detail || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {total > limit && (
        <div className="pagination">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>← Prev</button>
          <span>Page {currentPage} of {totalPages}</span>
          <button disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Next →</button>
        </div>
      )}
    </div>
  );
}

// ── TokenUsage ───────────────────────────────────────────────────────────────

function TokenUsage({ books }: { books: BookSummary[] }) {
  const [view, setView] = useState<'summary' | 'detail'>('summary');
  const [summary, setSummary] = useState<TokenUsageSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (view !== 'summary') return;
    setLoading(true);
    setError(null);
    fetchAllTokenUsage()
      .then(setSummary)
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed'))
      .finally(() => setLoading(false));
  }, [view]);

  return (
    <div>
      <div className="admin-tabs" style={{ marginBottom: 24, padding: 0, borderBottom: '1px solid #d8d0c0' }}>
        <button
          className={`admin-tab${view === 'summary' ? ' admin-tab--active' : ''}`}
          onClick={() => setView('summary')}
        >
          Summary
        </button>
        <button
          className={`admin-tab${view === 'detail' ? ' admin-tab--active' : ''}`}
          onClick={() => setView('detail')}
        >
          Per-Book Detail
        </button>
      </div>

      {view === 'summary' && (
        <>
          {loading && <p style={{ color: '#746d62' }}>Loading…</p>}
          {error && <div className="upload-message upload-message--error">{error}</div>}
          {summary && !loading && (
            <>
              <div className="token-summary-bar">
                <div className="token-summary-item">
                  <div className="token-summary-value">{fmtNum(summary.prompt_tokens)}</div>
                  <div className="token-summary-label">Prompt Tokens</div>
                </div>
                <div className="token-summary-item">
                  <div className="token-summary-value">{fmtNum(summary.completion_tokens)}</div>
                  <div className="token-summary-label">Completion Tokens</div>
                </div>
                <div className="token-summary-item">
                  <div className="token-summary-value">{fmtNum(summary.total_tokens)}</div>
                  <div className="token-summary-label">Total Tokens</div>
                </div>
              </div>

              <div style={{ overflowX: 'auto' }}>
                <table className="admin-table">
                  <thead>
                    <tr>
                      <th>Book</th>
                      <th>Prompt Tokens</th>
                      <th>Completion</th>
                      <th>Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.books.map((b: TokenBookEntry) => (
                      <tr key={b.book_id}>
                        <td>{b.title}</td>
                        <td>{fmtNum(b.prompt_tokens)}</td>
                        <td>{fmtNum(b.completion_tokens)}</td>
                        <td><strong>{fmtNum(b.total_tokens)}</strong></td>
                      </tr>
                    ))}
                    {summary.books.length === 0 && (
                      <tr><td colSpan={4} style={{ textAlign: 'center', color: '#746d62', padding: 24 }}>No token usage data yet.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      {view === 'detail' && <TokenDetail books={books} />}
    </div>
  );
}

// ── TokenDetail (per-book) ───────────────────────────────────────────────────

function TokenDetail({ books }: { books: BookSummary[] }) {
  const [selectedId, setSelectedId] = useState('');
  const [detail, setDetail] = useState<TokenUsageDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDetail = useCallback(async (bookId: string) => {
    if (!bookId) { setDetail(null); return; }
    setLoading(true);
    setError(null);
    try {
      const result = await fetchBookTokenUsage(bookId);
      setDetail(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  return (
    <div>
      <div className="admin-filters">
        <select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
          <option value="">Select a book…</option>
          {books.map((b) => (
            <option key={b.id} value={b.id}>{b.title}</option>
          ))}
        </select>
      </div>

      {error && <div className="upload-message upload-message--error">{error}</div>}

      {loading && <p style={{ color: '#746d62' }}>Loading…</p>}

      {detail && !loading && (
        <>
          <div className="token-summary-bar">
            <div className="token-summary-item">
              <div className="token-summary-value">{fmtNum(detail.prompt_tokens)}</div>
              <div className="token-summary-label">Prompt Tokens</div>
            </div>
            <div className="token-summary-item">
              <div className="token-summary-value">{fmtNum(detail.completion_tokens)}</div>
              <div className="token-summary-label">Completion Tokens</div>
            </div>
            <div className="token-summary-item">
              <div className="token-summary-value">{fmtNum(detail.total_tokens)}</div>
              <div className="token-summary-label">Total Tokens</div>
            </div>
          </div>

          {detail.entries.length > 0 && (
            <div style={{ overflowX: 'auto' }}>
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Stage</th>
                    <th>Node</th>
                    <th>Model</th>
                    <th>Prompt</th>
                    <th>Completion</th>
                    <th>Total</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.entries.map((entry, i) => (
                    <tr key={i}>
                      <td style={{ fontSize: 11 }}>{entry.stage || '—'}</td>
                      <td style={{ fontSize: 11, maxWidth: 100, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.node_id || '—'}</td>
                      <td style={{ fontSize: 11 }}>{entry.model || '—'}</td>
                      <td>{fmtNum(entry.prompt_tokens ?? 0)}</td>
                      <td>{fmtNum(entry.completion_tokens ?? 0)}</td>
                      <td><strong>{fmtNum(entry.total_tokens ?? 0)}</strong></td>
                      <td>
                        <span style={{ color: entry.status === 'failed' ? '#8b3b2b' : '#46603a', fontWeight: 600, fontSize: 11 }}>
                          {entry.status === 'failed' ? 'FAIL' : 'OK'}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {detail.entries.length === 0 && (
            <p style={{ color: '#746d62', textAlign: 'center', padding: 24 }}>
              No detailed token entries for this book.
            </p>
          )}
        </>
      )}
    </div>
  );
}
