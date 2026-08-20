import type { ChatCitation, EvidenceIndex, EvidenceRecord, SourceOutlineNode, WikiPage, WikiSection, WikiStructure } from '../types/wiki';

const configuredBase = ((import.meta.env.VITE_ONEBOOKWIKI_BASE_URL as string | undefined) || '/book').replace(/\/$/, '');
const bookIdPattern = /^[1-9]\d*$/;

function routeBookBase(pathname: string): string | undefined {
  const prefix = '/book';
  if (pathname !== prefix && !pathname.startsWith(`${prefix}/`)) return undefined;
  const suffix = pathname.slice(prefix.length).replace(/^\/+/, '');
  const firstSegment = suffix.split('/', 1)[0];
  if (bookIdPattern.test(firstSegment)) return `${prefix}/${encodeURIComponent(firstSegment)}`;
  return undefined;
}

export function resolveBookBase(pathname = window.location.pathname): string {
  return routeBookBase(pathname) || configuredBase;
}

function joinBase(base: string, path: string): string {
  return `${base}/${path.replace(/^\//, '')}`;
}

async function getJson<T>(base: string, path: string): Promise<T> {
  const response = await fetch(joinBase(base, path));
  if (!response.ok) throw new Error(`无法加载 ${path}（HTTP ${response.status}）`);
  return response.json() as Promise<T>;
}

function normalizeOutline(nodes: SourceOutlineNode[] | undefined): SourceOutlineNode[] {
  return (nodes || []).map(node => ({
    ...node,
    breadcrumb: Array.isArray(node.breadcrumb) ? node.breadcrumb : [],
    pageIds: Array.isArray(node.pageIds) ? node.pageIds : [],
    children: normalizeOutline(node.children),
  }));
}

export function normalizeStructure(value: WikiStructure): WikiStructure {
  return {
    ...value,
    pages: Array.isArray(value.pages) ? value.pages : [],
    sections: Array.isArray(value.sections) ? value.sections : [],
    sourceOutline: normalizeOutline(value.sourceOutline),
  };
}

export function formatPdfRange(start?: number, end?: number): string | undefined {
  if (typeof start !== 'number') return undefined;
  const last = typeof end === 'number' ? end : start;
  return start === last ? `PDF p. ${start}` : `PDF pp. ${start}-${last}`;
}

export function formatEpubLocator(record: Pick<EvidenceRecord, 'spine' | 'spine_index' | 'href' | 'fragment' | 'locator'>): string | undefined {
  const locator = record.locator || {};
  const href = record.href || (typeof locator.href === 'string' ? locator.href : undefined);
  const fragment = record.fragment || (typeof locator.fragment === 'string' ? locator.fragment : undefined);
  const spineIndex = typeof record.spine_index === 'number'
    ? record.spine_index
    : typeof locator.spine_index === 'number'
      ? locator.spine_index
      : undefined;
  const spineId = record.spine || (typeof locator.spine_id === 'string' ? locator.spine_id : undefined);
  const location = href ? `${href}${fragment ? `#${fragment}` : ''}` : undefined;
  const spine = typeof spineIndex === 'number' ? `Spine ${spineIndex}` : spineId ? `Spine ${spineId}` : undefined;
  return [spine, location].filter((value): value is string => Boolean(value)).join(' · ') || undefined;
}

export function formatConfidence(confidence?: number): string | undefined {
  if (typeof confidence !== 'number') return undefined;
  return `Confidence ${Math.round(confidence * 100)}%`;
}

export function formatSourceType(sourceType?: string): string | undefined {
  if (!sourceType) return undefined;
  return sourceType.replace(/[_-]+/g, ' ').replace(/\b\w/g, letter => letter.toUpperCase());
}

type LocatorCarrier = {
  physical_page_start?: number;
  physical_page_end?: number;
  spine?: string;
  spine_index?: number;
  href?: string;
  fragment?: string;
  locator?: Record<string, unknown>;
};

function asNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function asText(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined;
}

export function formatNativeLocator(record: LocatorCarrier): string | undefined {
  const locator = record.locator || {};
  const format = asText(locator.format)?.toUpperCase();
  const pdf = formatPdfRange(
    record.physical_page_start ?? asNumber(locator.physical_page_start),
    record.physical_page_end ?? asNumber(locator.physical_page_end),
  );
  const epub = formatEpubLocator(record);
  if (format === 'PDF') return pdf;
  if (format === 'EPUB') return epub;
  const range = (single: string, plural: string, start: unknown, end: unknown) => {
    const first = asNumber(start);
    const last = asNumber(end) ?? first;
    if (first === undefined) return undefined;
    return first === last ? `${single} ${first}` : `${plural} ${first}-${last}`;
  };
  if (format === 'TXT') return range('TXT line', 'TXT lines', locator.line_start, locator.line_end);
  if (format === 'DOC' || format === 'DOCX') return range(`${format} paragraph`, `${format} paragraphs`, locator.paragraph_start, locator.paragraph_end);
  if (format === 'HTML') {
    const href = asText(locator.href);
    const fragment = asText(locator.fragment);
    if (href || fragment) return `HTML ${href || 'document'}${fragment ? `#${fragment}` : ''}`;
    return range('HTML block', 'HTML blocks', locator.block_start, locator.block_end);
  }
  if (format === 'MOBI' || format === 'AZW' || format === 'AZW3') {
    const section = asText(locator.section);
    return section ? `${format} section ${section}` : format;
  }
  return pdf || epub;
}

export function formatLocator(record: EvidenceRecord): string {
  return record.display_label
    || formatNativeLocator(record)
    || record.source_title
    || record.book_title
    || 'Source reference';
}

const evidenceRevisionPattern = /^evr-[0-9a-f]{32}$/;

/**
 * Resolve a record only from a healthy Grounded v2 evidence projection.
 *
 * The optional revisions are deliberately positional for compatibility with
 * callers that previously supplied one expected book revision: the first is
 * the chat turn's pinned revision and the second is the loaded projection's
 * revision. A missing record revision is not treated as a match.
 */
export function resolveEvidence(
  id: string,
  evidence: EvidenceIndex,
  expectedTurnBookRevisionId?: string,
  expectedProjectionBookRevisionId?: string,
): EvidenceRecord | undefined {
  const normalized = typeof id === 'string' ? id.replace(/^onebookwiki:\/\/evidence\//, '') : '';
  if (
    evidence.contractVersion !== 'grounded-v2'
    || evidence.projectionStatus !== 'healthy'
    || !evidence.bookRevisionId
    || !evidenceRevisionPattern.test(normalized)
  ) return undefined;

  const record = evidence.evidence[normalized];
  if (!record) return undefined;
  if (
    record.evidence_id !== normalized
    || record.evidenceRevisionId !== normalized
    || !record.bookRevisionId
    || record.bookRevisionId !== evidence.bookRevisionId
  ) return undefined;

  const projectionRevisionId = expectedProjectionBookRevisionId || evidence.bookRevisionId;
  if (record.bookRevisionId !== projectionRevisionId) return undefined;
  if (expectedTurnBookRevisionId && record.bookRevisionId !== expectedTurnBookRevisionId) return undefined;
  return record;
}

export function citationToEvidenceRecord(
  citation: ChatCitation,
  evidence: EvidenceIndex,
  expectedTurnBookRevisionId?: string,
  expectedProjectionBookRevisionId?: string,
): EvidenceRecord | undefined {
  const evidenceId = citation.evidence_revision_id || citation.evidence_id;
  if (citation.evidence_revision_id && citation.evidence_id !== citation.evidence_revision_id) return undefined;
  const turnRevisionId = expectedTurnBookRevisionId || citation.book_revision_id;
  if (citation.book_revision_id && turnRevisionId && citation.book_revision_id !== turnRevisionId) return undefined;
  return resolveEvidence(evidenceId, evidence, turnRevisionId, expectedProjectionBookRevisionId);
}

export async function loadBook(base = resolveBookBase()): Promise<{ structure: WikiStructure; evidence: EvidenceIndex }> {
  const [structure, evidence] = await Promise.all([
    getJson<WikiStructure>(base, 'wiki/structure.json'),
    getJson<EvidenceIndex>(base, 'wiki/evidence.json'),
  ]);
  const normalizedStructure = normalizeStructure(structure);
  if (
    normalizedStructure.contractVersion !== 'grounded-v2'
    || normalizedStructure.projectionStatus !== 'healthy'
    || !normalizedStructure.bookRevisionId
    || evidence.contractVersion !== 'grounded-v2'
    || evidence.projectionStatus !== 'healthy'
    || evidence.bookRevisionId !== normalizedStructure.bookRevisionId
  ) {
    throw new Error('书籍知识投影版本无效或不一致');
  }
  return { structure: normalizedStructure, evidence };
}

export async function loadPage(page: WikiPage, base = resolveBookBase()): Promise<string> {
  const response = await fetch(joinBase(base, `wiki/${page.path}`));
  if (!response.ok) throw new Error(`无法加载页面 ${page.path}（HTTP ${response.status}）`);
  return response.text();
}

export function sectionPages(structure: WikiStructure, section: WikiSection): WikiPage[] {
  const pagesById = new Map(structure.pages.map(page => [page.id, page]));
  return section.pages.map(id => pagesById.get(id)).filter((page): page is WikiPage => Boolean(page));
}
