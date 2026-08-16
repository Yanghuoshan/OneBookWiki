import type { BookSummary } from '../types/wiki';
import { getCoverUrl } from '../data/bookListLoader';

type Props = {
  book: BookSummary;
  onRetry?: (bookId: number) => void;
};

const phaseLabels: Record<string, string> = {
  queued: '排队中',
  importing: '导入中',
  indexing: '索引中',
  generating: '生成中',
  rendering: '渲染中',
  complete: '已完成',
  failed: '处理失败',
  pending: '待处理',
};

function navigateToBook(bookId: number) {
  window.location.href = `/book/${encodeURIComponent(bookId)}`;
}

export default function BookCard({ book, onRetry }: Props) {
  const isComplete = book.phase === 'complete';
  const isFailed = book.phase === 'failed';
  const isClickable = isComplete || (isFailed && onRetry);
  const coverUrl = getCoverUrl(book.id, book.cover_path);
  const phaseLabel = phaseLabels[book.phase] || book.phase;

  function handleClick() {
    if (isComplete) {
      navigateToBook(book.id);
    } else if (isFailed && onRetry) {
      onRetry(book.id);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      handleClick();
    }
  }

  return (
    <article
      className={`book-card ${isComplete ? 'book-card--complete' : ''} ${isFailed ? 'book-card--failed' : ''} ${!isComplete && !isFailed ? 'book-card--pending' : ''}`}
      onClick={handleClick}
      role={isClickable ? 'button' : undefined}
      tabIndex={isClickable ? 0 : undefined}
      onKeyDown={isClickable ? handleKeyDown : undefined}
      aria-label={isClickable ? `${book.title} — ${phaseLabel}` : undefined}
    >
      <div className="book-card-cover">
        {coverUrl ? (
          <img
            className="book-card-cover-img"
            src={coverUrl}
            alt={`${book.title} cover`}
            loading="lazy"
          />
        ) : (
          <span className="book-card-format">{book.format || '...'}</span>
        )}
      </div>
      <div className="book-card-body">
        <h3 className="book-card-title">{book.title}</h3>
        {book.description && (
          <p className="book-card-desc">{book.description}</p>
        )}
        <div className="book-card-meta">
          {isComplete && book.page_count !== undefined && book.page_count > 0 && (
            <span>{book.page_count} 页</span>
          )}
          {!isComplete && (
            <span className={`book-card-phase ${isFailed ? 'book-card-phase--failed' : ''}`}>
              {phaseLabel}
            </span>
          )}
          {isFailed && book.error_message && (
            <span className="book-card-error" title={book.error_message}>
              {book.error_message.slice(0, 60)}{book.error_message.length > 60 ? '…' : ''}
            </span>
          )}
          {book.author && <span>{book.author}</span>}
        </div>
      </div>
    </article>
  );
}
