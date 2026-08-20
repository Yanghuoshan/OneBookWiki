import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import MarkdownReader, { citationUrlTransform } from '../components/MarkdownReader';
import type { EvidenceIndex, EvidenceRecord, WikiPage } from '../types/wiki';

const evidenceId = 'evr-0123456789abcdef0123456789abcdef';
const revisionId = 'book-rev-1';
const page: WikiPage = { id: 'p2', title: 'Second page', path: 'chapter/two.md', kind: 'page' };
const record: EvidenceRecord = {
  evidence_id: evidenceId, evidenceRevisionId: evidenceId, bookRevisionId: revisionId,
  display_label: 'p. 12', quote: 'Evidence quote',
};
const evidence: EvidenceIndex = {
  contractVersion: 'grounded-v2', projectionStatus: 'healthy', bookRevisionId: revisionId,
  evidence: { [evidenceId]: record },
};
const pagesByPath = new Map([[page.path, page]]);
const baseProps = { evidence, pagePath: 'chapter/one.md', pagesByPath, onCitation: vi.fn(), onPageLink: vi.fn() };

describe('MarkdownReader', () => {
  it('only preserves canonical evidence URLs', () => {
    expect(citationUrlTransform(`onebookwiki://evidence/${evidenceId}`)).toBe(`onebookwiki://evidence/${evidenceId}`);
    expect(citationUrlTransform('javascript:alert(1)')).not.toContain('javascript');
  });

  it('renders citation links as chips and opens indexed evidence', () => {
    const onCitation = vi.fn();
    render(<MarkdownReader {...baseProps} onCitation={onCitation} content={`See [source](onebookwiki://evidence/${evidenceId}).`} />);
    const chip = screen.getByRole('button', { name: 'p. 12' });
    fireEvent.click(chip);
    expect(onCitation).toHaveBeenCalledWith(record);
  });

  it('turns relative markdown page links into page buttons', () => {
    const onPageLink = vi.fn();
    render(<MarkdownReader {...baseProps} onPageLink={onPageLink} content='Read [the next page](two.md#heading).' />);
    fireEvent.click(screen.getByRole('button', { name: 'the next page' }));
    expect(onPageLink).toHaveBeenCalledWith(page);
  });

  it('sanitizes raw HTML and unsafe links', () => {
    render(<MarkdownReader {...baseProps} content={'<script>alert("x")</script>\n\n<a href="javascript:alert(1)">unsafe</a> **safe**'} />);
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText('safe')).toBeInTheDocument();
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });
});
