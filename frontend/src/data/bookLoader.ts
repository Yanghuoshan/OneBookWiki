import type { EvidenceIndex, EvidenceRecord, Locator, WikiPage, WikiSection, WikiStructure } from '../types/wiki';

const configuredBase = ((import.meta.env.VITE_ONEBOOKWIKI_BASE_URL as string | undefined) || '/book').replace(/\/$/, '');
const bookIdPattern = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;

function routeBookBase(pathname: string): string | undefined {
  const prefix = '/book';
  if (pathname !== prefix && !pathname.startsWith(`${prefix}/`)) return undefined;
  const suffix = pathname.slice(prefix.length).replace(/^\/+/, '');
  const firstSegment = suffix.split('/', 1)[0];
  if (!firstSegment || firstSegment === 'wiki') return prefix;
  if (bookIdPattern.test(firstSegment)) return `${prefix}/${encodeURIComponent(firstSegment)}`;
  return prefix;
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

export function normalizeStructure(value: WikiStructure): WikiStructure {
  const pages = Array.isArray(value.pages) ? value.pages : [];
  const sections = Array.isArray(value.sections) ? value.sections : [];
  const rootSections = value.rootSections?.length ? value.rootSections : sections.map(section => section.id);
  return {
    ...value,
    pages,
    sections,
    rootSections,
  };
}

export function formatLocator(record: EvidenceRecord): string {
  const locator: Locator = record.locator || (record.page ? { format: 'PDF', physical_page_start: record.page, physical_page_end: record.page } : {});
  const format = (locator.format || '').toUpperCase();
  if (format === 'PDF') {
    const start = locator.physical_page_start;
    const end = locator.physical_page_end ?? start;
    if (typeof start === 'number' && typeof end === 'number') return start === end ? `PDF p. ${start}` : `PDF pp. ${start}-${end}`;
    return `PDF Chapter ${record.chapter}`;
  }
  if (format === 'EPUB') {
    let label = `EPUB Ch. ${locator.chapter ?? record.chapter}`;
    if (locator.spine_index !== undefined) label += ` · Spine ${locator.spine_index}`;
    else if (locator.spine_id) label += ` · Spine ${locator.spine_id}`;
    if (locator.href) label += ` · ${locator.href}${locator.fragment ? `#${locator.fragment}` : ''}`;
    return label;
  }
  return `Chapter ${record.chapter} · lines ${record.start_line}-${record.end_line}`;
}

export function resolveEvidence(id: string, evidence: EvidenceIndex): EvidenceRecord | undefined {
  return evidence.evidence[id] || evidence.evidence[id.replace(/^onebookwiki:\/\/evidence\//, '')];
}

export async function loadBook(base = resolveBookBase()): Promise<{ structure: WikiStructure; evidence: EvidenceIndex }> {
  const [structure, evidence] = await Promise.all([
    getJson<WikiStructure>(base, 'wiki/structure.json'),
    getJson<EvidenceIndex>(base, 'wiki/evidence.json').catch(() => ({ evidence: {} })),
  ]);
  return { structure: normalizeStructure(structure), evidence };
}

export async function loadPage(page: WikiPage, base = resolveBookBase()): Promise<string> {
  const response = await fetch(joinBase(base, `wiki/${page.path}`));
  if (!response.ok) throw new Error(`无法加载页面 ${page.path}（HTTP ${response.status}）`);
  return response.text();
}

export function sectionPages(structure: WikiStructure, section: WikiSection): WikiPage[] {
  return section.pages.map(id => structure.pages.find(page => page.id === id)).filter((page): page is WikiPage => Boolean(page));
}
