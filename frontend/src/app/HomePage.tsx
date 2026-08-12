import { useCallback, useEffect, useRef, useState } from 'react';
import type { BookSummary } from '../types/wiki';
import { fetchBookList, fetchBookStatus, retryProcessing } from '../data/bookListLoader';
import UploadZone from '../components/UploadZone';
import BookCard from '../components/BookCard';

const POLL_INTERVAL_MS = 3000;

function isActive(book: BookSummary): boolean {
  return book.phase !== 'complete' && book.phase !== 'failed';
}

export default function HomePage() {
  const [books, setBooks] = useState<BookSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const booksRef = useRef<BookSummary[]>([]);

  // Keep booksRef in sync so poll callbacks see latest state
  booksRef.current = books;

  async function loadBooks() {
    setError(null);
    try {
      const list = await fetchBookList();
      setBooks(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load books');
    } finally {
      setLoading(false);
    }
  }

  // Initial load
  useEffect(() => {
    loadBooks();
  }, []);

  // Set up polling for active (in-progress) books
  const startPolling = useCallback(() => {
    const activeBooks = booksRef.current.filter(isActive);
    const activeIds = new Set(activeBooks.map((b) => b.id));

    // Remove timers for books no longer active
    for (const [id, timer] of pollTimers.current) {
      if (!activeIds.has(id)) {
        clearInterval(timer);
        pollTimers.current.delete(id);
      }
    }

    // Add timers for newly active books
    for (const book of activeBooks) {
      if (!pollTimers.current.has(book.id)) {
        const timer = setInterval(async () => {
          try {
            const status = await fetchBookStatus(book.id);
            setBooks((prev) =>
              prev.map((b) =>
                b.id === book.id
                  ? { ...b, phase: status.phase, error_message: status.error ?? b.error_message }
                  : b,
              ),
            );
            // Stop polling if terminal state reached
            if (status.phase === 'complete' || status.phase === 'failed') {
              const t = pollTimers.current.get(book.id);
              if (t) {
                clearInterval(t);
                pollTimers.current.delete(book.id);
              }
            }
          } catch {
            // Silently ignore polling errors
          }
        }, POLL_INTERVAL_MS);
        pollTimers.current.set(book.id, timer);
      }
    }
  }, []);

  // Re-evaluate polling when books change
  useEffect(() => {
    if (!loading) startPolling();
    return () => {
      for (const timer of pollTimers.current.values()) {
        clearInterval(timer);
      }
      pollTimers.current.clear();
    };
  }, [books, loading, startPolling]);

  async function handleUploaded() {
    await loadBooks();
  }

  async function handleRetry(bookId: string) {
    try {
      await retryProcessing(bookId);
      setBooks((prev) =>
        prev.map((b) => (b.id === bookId ? { ...b, phase: 'queued' as const, error_message: undefined } : b)),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Retry failed');
    }
  }

  const completeBooks = books.filter((b) => b.phase === 'complete');
  const pendingBooks = books.filter((b) => b.phase !== 'complete');

  return (
    <div className="home-page">
      <header className="topbar">
        <div className="brand-mark">
          ONE<span>BOOK</span>
        </div>
        <div className="book-heading">
          <span className="eyebrow">EVIDENCE-GROUNDED READING</span>
          <h1>OneBookWiki</h1>
        </div>
        <div className="topbar-meta">{books.length} book{books.length !== 1 ? 's' : ''}</div>
      </header>
      <main className="home-main">
        <UploadZone onUploaded={handleUploaded} />

        {loading && (
          <div className="status-screen">
            <div className="loader-mark">◎</div>
            <p>Loading book list...</p>
          </div>
        )}

        {error && (
          <div className="status-screen error-screen">
            <h1>Loading Failed</h1>
            <p>{error}</p>
          </div>
        )}

        {!loading && !error && (
          <>
            {completeBooks.length > 0 && (
              <section className="books-section">
                <h2 className="section-title">Completed Books</h2>
                <div className="book-grid">
                  {completeBooks.map((book) => (
                    <BookCard key={book.id} book={book} />
                  ))}
                </div>
              </section>
            )}

            {pendingBooks.length > 0 && (
              <section className="books-section">
                <h2 className="section-title">Processing</h2>
                <div className="book-grid">
                  {pendingBooks.map((book) => (
                    <BookCard key={book.id} book={book} onRetry={handleRetry} />
                  ))}
                </div>
              </section>
            )}

            {books.length === 0 && (
              <div className="empty-state">
                <h2>No Books Yet</h2>
                <p>Upload your first ebook to get started.</p>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
