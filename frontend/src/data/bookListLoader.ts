/** API client for book listing, upload, status, retry, and admin operations. */
import type {
  AdminStats,
  BookStatus,
  BookSummary,
  OperationLog,
  TokenUsageDetail,
  TokenUsageSummary,
} from '../types/wiki';

const API_BASE = '/api';

export async function fetchBookList(): Promise<BookSummary[]> {
  const res = await fetch(`${API_BASE}/books`);
  if (!res.ok) throw new Error(`Failed to load book list (HTTP ${res.status})`);
  const data = await res.json();
  return data.books as BookSummary[];
}

export async function fetchBookStatus(bookId: number): Promise<BookStatus> {
  const res = await fetch(`${API_BASE}/books/${encodeURIComponent(bookId)}/status`);
  if (!res.ok) throw new Error(`Failed to load status (HTTP ${res.status})`);
  return res.json() as Promise<BookStatus>;
}

export function getCoverUrl(bookId: number, coverPath?: string | null): string | undefined {
  if (coverPath) {
    return `/book/${encodeURIComponent(bookId)}/${coverPath}`;
  }
  return undefined;
}

export class UploadError extends Error {
  constructor(message: string, public status?: number) {
    super(message);
    this.name = 'UploadError';
  }
}

export type UploadResult = {
  bookId: number;
  title: string;
  phase: string;
};

export async function uploadBook(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append('file', file);

  if (onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${API_BASE}/upload`);
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable) {
          onProgress(Math.round((event.loaded / event.total) * 100));
        }
      });
      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch {
            reject(new UploadError('Invalid server response'));
          }
        } else {
          try {
            const error = JSON.parse(xhr.responseText);
            reject(new UploadError(
              error.detail || error.error || `Upload failed (HTTP ${xhr.status})`,
              xhr.status,
            ));
          } catch {
            reject(new UploadError(`Upload failed (HTTP ${xhr.status})`, xhr.status));
          }
        }
      });
      xhr.addEventListener('error', () => reject(new UploadError('Network error')));
      xhr.send(formData);
    });
  }

  const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new UploadError(
      (body as { detail?: string }).detail || `Upload failed (HTTP ${res.status})`,
      res.status,
    );
  }
  return res.json() as Promise<UploadResult>;
}

export async function retryProcessing(
  bookId: number,
): Promise<{ bookId: number; phase: string }> {
  const res = await fetch(
    `${API_BASE}/books/${encodeURIComponent(bookId)}/process`,
    { method: 'POST' },
  );
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `Retry failed (HTTP ${res.status})`);
  }
  return res.json() as Promise<{ bookId: number; phase: string }>;
}

// ── Admin API ────────────────────────────────────────────────────────────────

export async function fetchAdminStats(): Promise<AdminStats> {
  const res = await fetch(`${API_BASE}/admin/stats`);
  if (!res.ok) throw new Error(`Failed to load stats (HTTP ${res.status})`);
  return res.json();
}

export async function fetchOperations(
  bookId?: number,
  limit = 100,
  offset = 0,
): Promise<{ logs: OperationLog[]; total: number; limit: number; offset: number }> {
  const params = new URLSearchParams();
  if (bookId !== undefined) params.set('book_id', String(bookId));
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  const res = await fetch(`${API_BASE}/admin/operations?${params}`);
  if (!res.ok) throw new Error(`Failed to load operations (HTTP ${res.status})`);
  return res.json();
}

export async function fetchAllTokenUsage(): Promise<TokenUsageSummary> {
  const res = await fetch(`${API_BASE}/admin/tokens`);
  if (!res.ok) throw new Error(`Failed to load token usage (HTTP ${res.status})`);
  return res.json();
}

export async function fetchBookTokenUsage(bookId: number): Promise<TokenUsageDetail> {
  const res = await fetch(`${API_BASE}/admin/tokens/${encodeURIComponent(bookId)}`);
  if (!res.ok) throw new Error(`Failed to load book token usage (HTTP ${res.status})`);
  return res.json();
}

export async function deleteBook(bookId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/admin/books/${encodeURIComponent(bookId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `Delete failed (HTTP ${res.status})`);
  }
}

export async function updateBookMetadata(
  bookId: number,
  updates: { title?: string; author?: string; description?: string },
): Promise<BookSummary> {
  const res = await fetch(`${API_BASE}/admin/books/${encodeURIComponent(bookId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error((body as { detail?: string }).detail || `Update failed (HTTP ${res.status})`);
  }
  return res.json();
}
