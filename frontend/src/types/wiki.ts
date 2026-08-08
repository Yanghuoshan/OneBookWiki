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
  locator?: Record<string, unknown>;
  display_label?: string;
};

export type EvidenceIndex = {
  schema_version?: number;
  evidence: Record<string, EvidenceRecord>;
};
