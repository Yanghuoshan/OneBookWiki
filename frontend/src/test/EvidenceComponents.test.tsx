import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import EvidenceCard from '../components/EvidenceCard';
import SourcePanel from '../components/SourcePanel';
import type { EvidenceRecord } from '../types/wiki';

const baseRecord: EvidenceRecord = {
  evidence_id: 'evr-0123456789abcdef0123456789abcdef',
  book_title: 'Book', source_title: 'Source', source_path: 'source.txt', start_line: 10, end_line: 11,
  excerpt: 'first line\nsecond line', excerpt_start_line: 10, excerpt_end_line: 11,
};

describe('EvidenceCard and SourcePanel', () => {
  it('renders source line numbers and range markers in an evidence card', () => {
    render(<EvidenceCard record={baseRecord} cardId="evidence-1" />);
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText('11')).toBeInTheDocument();
    expect(screen.getByText('first line')).toBeInTheDocument();
    expect(screen.getByText('second line').parentElement).toHaveAttribute('data-marker', 'end');
  });

  it('renders the excerpt in a blockquote when line metadata is not usable', () => {
    render(<EvidenceCard record={{ ...baseRecord, excerpt_truncated: true }} cardId="evidence-2" />);
    expect(screen.getByRole('blockquote')).toHaveTextContent('first line second line');
  });

  it('clamps keyboard resize operations and closes the source panel', () => {
    const onWidthChange = vi.fn();
    const onClose = vi.fn();
    render(<SourcePanel record={baseRecord} width={320} minWidth={240} maxWidth={600} onWidthChange={onWidthChange} onClose={onClose} />);
    const handle = screen.getByRole('separator', { name: '调整来源面板宽度' });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expect(onWidthChange).toHaveBeenCalledWith(336);
    fireEvent.keyDown(handle, { key: 'Home' });
    expect(onWidthChange).toHaveBeenCalledWith(240);
    fireEvent.keyDown(handle, { key: 'End', shiftKey: true });
    expect(onWidthChange).toHaveBeenCalledWith(600);
    fireEvent.click(screen.getByRole('button', { name: '关闭来源面板' }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('updates width from pointer dragging and exposes resizing state', () => {
    const onWidthChange = vi.fn();
    render(<SourcePanel record={baseRecord} width={320} minWidth={240} maxWidth={600} onWidthChange={onWidthChange} onClose={vi.fn()} />);
    const handle = screen.getByRole('separator', { name: '调整来源面板宽度' });
    fireEvent.pointerDown(handle, { pointerId: 7, clientX: 100, button: 0, isPrimary: true });
    expect(screen.getByRole('complementary')).toHaveAttribute('data-resizing', 'true');
    fireEvent.pointerMove(handle, { pointerId: 7, clientX: 50 });
    expect(onWidthChange).toHaveBeenCalledWith(370);
    fireEvent.pointerUp(handle, { pointerId: 7 });
    expect(screen.getByRole('complementary')).toHaveAttribute('data-resizing', 'false');
  });
});
