import type { ChatConversation } from '../types/wiki';

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    let detail = `Request failed (HTTP ${response.status})`;
    try {
      const value = await response.json() as { detail?: string };
      detail = value.detail || detail;
    } catch {
      // Keep the HTTP status fallback when a proxy returns non-JSON.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export type CreateConversationResponse = {
  conversationId: string;
  turnId: string;
  status: 'queued';
  answerUrl: string;
};

export function createConversation(bookId: number, question: string): Promise<CreateConversationResponse> {
  return requestJson(`/api/books/${bookId}/chat-conversations`, {
    method: 'POST',
    body: JSON.stringify({ question }),
  });
}

export function getConversation(conversationId: string): Promise<ChatConversation> {
  return requestJson(`/api/chat-conversations/${encodeURIComponent(conversationId)}`);
}

export function appendConversationTurn(conversationId: string, question: string): Promise<{ turnId: string; status: 'queued' }> {
  return requestJson(`/api/chat-conversations/${encodeURIComponent(conversationId)}/turns`, {
    method: 'POST',
    body: JSON.stringify({ question }),
  });
}
