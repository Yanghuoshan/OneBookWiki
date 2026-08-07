export type WikiPage = {
  id: string;
  title: string;
  path: string;
  kind: string;
  chapter?: number;
  rawSources?: string[];
  relatedPages?: string[];
};

export type WikiSection = {
  id: string;
  title: string;
  pages: string[];
};

export type WikiStructure = {
  id: string;
  title: string;
  description?: string;
  pages: WikiPage[];
  sections: WikiSection[];
  rootSections?: string[];
};

export type Locator = {
  format?: string;
  physical_page_start?: number;
  physical_page_end?: number;
  chapter?: number;
  spine_index?: number;
  spine_id?: string;
  href?: string;
  fragment?: string;
  precision?: string;
};

export type EvidenceRecord = {
  evidence_id: string;
  chunk_id: string;
  source_path: string;
  chapter: number;
  start_line: number;
  end_line: number;
  quote?: string;
  excerpt?: string;
  page?: number | null;
  spine?: string | null;
  locator?: Locator;
  display_label?: string;
  source_hash?: string;
};

export type EvidenceIndex = {
  schema_version?: number;
  evidence: Record<string, EvidenceRecord>;
};
