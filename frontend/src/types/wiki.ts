export type SourceLocator = Record<string, unknown>;

export type WikiPage = {
  id: string;
  title: string;
  path: string;
  kind: string;
  sourceUnitId?: string;
  sourceTitle?: string;
  sourceKind?: string;
  breadcrumb?: string[];
  physicalPageStart?: number;
  physicalPageEnd?: number;
  spine?: string;
  spineIndex?: number;
  href?: string;
  fragment?: string;
  sourceUnitLocator?: SourceLocator;
  part?: number;
  partCount?: number;
  bookTitle?: string;
};

export type WikiSection = {
  id: string;
  title: string;
  pages: string[];
};

export type SourceOutlineNode = {
  id: string;
  title: string;
  kind: string;
  breadcrumb: string[];
  confidence: number;
  pageIds: string[];
  children: SourceOutlineNode[];
};

export type WikiStructure = {
  id: string;
  title: string;
  description?: string;
  pages: WikiPage[];
  sections: WikiSection[];
  sourceOutline: SourceOutlineNode[];
};

export type EvidenceRecord = {
  evidence_id: string;
  chunk_id?: string;
  source_path?: string;
  chapter?: number;
  start_line?: number;
  end_line?: number;
  quote?: string;
  excerpt?: string;
  excerpt_start_line?: number;
  excerpt_end_line?: number;
  excerpt_truncated?: boolean;
  source_hash?: string;
  book_title?: string;
  source_title?: string;
  breadcrumb?: string[];
  source_type?: string;
  physical_page_start?: number;
  physical_page_end?: number;
  spine?: string;
  spine_index?: number;
  href?: string;
  fragment?: string;
  locator?: SourceLocator;
  source_unit_locator?: SourceLocator;
  display_label?: string;
};

export type EvidenceIndex = {
  schema_version?: number;
  evidence: Record<string, EvidenceRecord>;
};

// ---- Book listing types ----

export type BookPhase =
  | "empty"
  | "queued"
  | "importing"
  | "indexing"
  | "generating"
  | "rendering"
  | "complete"
  | "failed"
  | "pending";

export type BookSummary = {
  id: number;
  title: string;
  author?: string;
  format?: string;
  source_name?: string;
  source_hash?: string;
  cover_path?: string;
  phase: BookPhase;
  page_count?: number;
  chapter_count?: number;
  description?: string;
  error_message?: string;
  created_at: string;
  updated_at: string;
};

export type BookStatus = {
  bookId: number;
  title: string;
  phase: BookPhase;
  error?: string | null;
};

// ---- Admin types ----

export type OperationLog = {
  id: number;
  book_id: number;
  book_title?: string;
  operation: string;
  phase?: string;
  status: string;
  detail?: string;
  created_at: string;
};

export type AdminStats = {
  total_books: number;
  complete_books: number;
  failed_books: number;
  processing_books: number;
  pending_books: number;
  total_pages: number;
  total_chapters: number;
  total_tokens: number;
  recent_operations_24h: number;
};

export type TokenBookEntry = {
  book_id: number;
  title: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

export type TokenUsageSummary = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  book_count: number;
  books: TokenBookEntry[];
};

export type TokenEntry = {
  run_id?: string;
  node_id?: string;
  stage?: string;
  attempt?: number;
  provider?: string;
  model?: string;
  timestamp?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  status?: string;
};

export type TokenUsageDetail = {
  book_id: number;
  book_title: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  entries: TokenEntry[];
};

// ---- Persistent chat types ----

export type ChatTurnStatus = 'queued' | 'retrieving' | 'generating' | 'succeeded' | 'refused' | 'failed';

export type ChatCitation = {
  evidence_id: string;
  chunk_id?: string;
  source_path?: string;
  chapter?: number;
  start_line?: number;
  end_line?: number;
  quote?: string;
};

export type ChatTurn = {
  id: string;
  turn_no: number;
  question: string;
  answer?: string | null;
  status: ChatTurnStatus;
  refusal_code?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  citations: ChatCitation[];
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
};

export type ChatConversation = {
  id: string;
  book_id: number;
  book_title: string;
  book_phase: BookPhase;
  status: 'active';
  created_at: string;
  updated_at: string;
  turns: ChatTurn[];
};
