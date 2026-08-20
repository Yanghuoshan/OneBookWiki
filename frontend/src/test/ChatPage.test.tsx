import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import ChatPage from '../app/ChatPage';
import type { ChatConversation, EvidenceIndex, WikiStructure } from '../types/wiki';
import { loadBook } from '../data/bookLoader';
import { getConversation } from '../data/chatLoader';

vi.mock('../data/bookLoader', async importOriginal => {
  const actual = await importOriginal<typeof import('../data/bookLoader')>();
  return { ...actual, loadBook: vi.fn() };
});
vi.mock('../data/chatLoader', () => ({
  appendConversationTurn: vi.fn(),
  getConversation: vi.fn(),
}));

const evidenceId = 'evr-0123456789abcdef0123456789abcdef';
const structure: WikiStructure = {
  id: 'book-1', contractVersion: 'grounded-v2', projectionStatus: 'healthy', bookRevisionId: 'book-rev-1',
  title: 'Grounded book', pages: [], sections: [], sourceOutline: [],
};
const evidence: EvidenceIndex = {
  contractVersion: 'grounded-v2', projectionStatus: 'healthy', bookRevisionId: 'book-rev-1',
  evidence: {
    [evidenceId]: {
      evidence_id: evidenceId, evidenceRevisionId: evidenceId, bookRevisionId: 'book-rev-1',
      source_title: 'Primary source', excerpt: 'Verified excerpt',
    },
  },
};

function conversation(overrides: Partial<ChatConversation['turns'][number]> = {}): ChatConversation {
  return {
    id: 'conversation-1', book_id: 1, book_title: 'Grounded book', book_phase: 'complete', status: 'active',
    created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
    turns: [{
      id: 'turn-1', turn_no: 1, question: 'What happened?', answer: 'The answer.', status: 'succeeded',
      citations: [{ evidence_id: evidenceId, book_revision_id: 'book-rev-1' }], created_at: '2026-01-01T00:00:00Z', ...overrides,
    }],
  };
}

describe('ChatPage answer evidence states', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(loadBook).mockResolvedValue({ structure, evidence });
    vi.mocked(getConversation).mockResolvedValue(conversation());
    window.history.replaceState({}, '', '/book/1/ask/conversation-1');
  });

  it('renders a verified answer with its indexed evidence card', async () => {
    render(<ChatPage bookId={1} conversationId="conversation-1" />);
    expect(await screen.findByText('The answer.')).toBeInTheDocument();
    expect(screen.getByText('Primary source')).toBeInTheDocument();
    expect(screen.getByText('Verified excerpt')).toBeInTheDocument();
  });

  it('fails closed on an unresolved citation instead of synthesizing a source', async () => {
    vi.mocked(getConversation).mockResolvedValue(conversation({
      citations: [{ evidence_id: 'evr-ffffffffffffffffffffffffffffffff', book_revision_id: 'book-rev-1', quote: 'Unverified quote' }],
    }));
    render(<ChatPage bookId={1} conversationId="conversation-1" />);
    expect(await screen.findByText('The answer.')).toBeInTheDocument();
    expect(screen.queryByText('Unverified quote')).not.toBeInTheDocument();
    expect(screen.queryByText('Source reference')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('引用不可验证');
  });

  it('shows the explicit no-citation state for an answer without citations', async () => {
    vi.mocked(getConversation).mockResolvedValue(conversation({ citations: [] }));
    render(<ChatPage bookId={1} conversationId="conversation-1" />);
    expect(await screen.findByText('The answer.')).toBeInTheDocument();
    expect(screen.getByText('此回答暂无引用证据。')).toBeInTheDocument();
  });
});
