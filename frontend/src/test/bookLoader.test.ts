import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  citationToEvidenceRecord,
  loadBook,
  resolveEvidence,
} from '../data/bookLoader';
import type { ChatCitation, EvidenceIndex, EvidenceRecord, WikiStructure } from '../types/wiki';

const evidenceId = 'evr-0123456789abcdef0123456789abcdef';
const revisionId = 'book-rev-1';

function record(overrides: Partial<EvidenceRecord> = {}): EvidenceRecord {
  return { evidence_id: evidenceId, evidenceRevisionId: evidenceId, bookRevisionId: revisionId, quote: 'Quoted text', ...overrides };
}

function index(overrides: Partial<EvidenceIndex> = {}): EvidenceIndex {
  return { contractVersion: 'grounded-v2', projectionStatus: 'healthy', bookRevisionId: revisionId, evidence: { [evidenceId]: record() }, ...overrides };
}

const structure: WikiStructure = {
  id: 'book-1',
  contractVersion: 'grounded-v2',
  projectionStatus: 'healthy',
  bookRevisionId: revisionId,
  title: 'Test book',
  pages: [],
  sections: [],
  sourceOutline: [],
};

const citation: ChatCitation = { evidence_id: evidenceId, book_revision_id: revisionId, quote: 'Citation quote' };

describe('bookLoader evidence helpers', () => {
  beforeEach(() => vi.restoreAllMocks());

  it('resolves canonical evidence URLs and rejects malformed or stale records', () => {
    const evidence = index();
    expect(resolveEvidence(evidenceId, evidence)).toEqual(record());
    expect(resolveEvidence(`onebookwiki://evidence/${evidenceId}`, evidence, revisionId)).toEqual(record());
    expect(resolveEvidence('not-an-evidence-id', evidence)).toBeUndefined();
    expect(resolveEvidence(evidenceId, evidence, 'book-rev-2')).toBeUndefined();
    expect(resolveEvidence(evidenceId, index({ bookRevisionId: 'book-rev-2' }), revisionId)).toBeUndefined();
  });

  it('converts a citation to its indexed record and never synthesizes a fallback for an unindexed citation', () => {
    expect(citationToEvidenceRecord(citation, index())).toEqual(record());
    const missing: ChatCitation = { ...citation, evidence_id: 'evr-ffffffffffffffffffffffffffffffff', quote: 'Fallback quote' };
    expect(citationToEvidenceRecord(missing, index())).toBeUndefined();
  });

  it('loads and validates both grounded-v2 projections', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      return new Response(url.endsWith('structure.json') ? JSON.stringify(structure) : JSON.stringify(index()), { status: 200 });
    });
    const result = await loadBook('/book/1');
    expect(result.structure).toEqual(structure);
    expect(result.evidence).toEqual(index());
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('rejects mismatched or unhealthy projections', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      return new Response(url.endsWith('structure.json') ? JSON.stringify(structure) : JSON.stringify(index({ projectionStatus: 'stale' as 'healthy' })), { status: 200 });
    });
    await expect(loadBook('/book/1')).rejects.toThrow('书籍知识投影版本无效或不一致');
  });
});
