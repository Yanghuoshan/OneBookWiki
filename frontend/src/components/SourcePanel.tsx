import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import type { EvidenceRecord } from '../types/wiki';
import { formatEpubLocator, formatLocator, formatPdfRange, formatSourceType } from '../data/bookLoader';

type Props = {
  record: EvidenceRecord;
  width: number;
  minWidth: number;
  maxWidth: number;
  onWidthChange: (width: number) => void;
  onClose: () => void;
};

type DragState = { pointerId: number; startClientX: number; startWidth: number };

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum);
}

export default function SourcePanel({ record, width, minWidth, maxWidth, onWidthChange, onClose }: Props) {
  const [isResizing, setIsResizing] = useState(false);
  const handleRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const title = record.source_title || record.breadcrumb?.at(-1) || record.book_title || formatLocator(record);
  const details = Array.from(new Set([
    record.book_title,
    record.breadcrumb?.length ? record.breadcrumb.join(' › ') : undefined,
    formatSourceType(record.source_type),
    formatPdfRange(record.physical_page_start, record.physical_page_end),
    formatEpubLocator(record),
    formatLocator(record),
  ].filter((detail): detail is string => Boolean(detail))));
  const excerptLines = record.excerpt?.split('\n') || [];
  const hasLineRange = Boolean(
    record.excerpt !== undefined
    && !record.excerpt_truncated
    && Number.isInteger(record.excerpt_start_line)
    && Number.isInteger(record.excerpt_end_line)
    && Number.isInteger(record.start_line)
    && Number.isInteger(record.end_line)
    && record.excerpt_start_line! <= record.excerpt_end_line!
    && record.start_line! <= record.end_line!
    && record.excerpt_end_line === record.excerpt_start_line! + excerptLines.length - 1,
  );
  const sourceRange = record.source_path && Number.isInteger(record.start_line) && Number.isInteger(record.end_line)
    ? `${record.source_path} · lines ${record.start_line}-${record.end_line}`
    : undefined;

  const endResize = (target?: HTMLDivElement, pointerId?: number) => {
    if (target && typeof pointerId === 'number' && target.hasPointerCapture(pointerId)) {
      target.releasePointerCapture(pointerId);
    }
    dragRef.current = null;
    setIsResizing(false);
  };

  useEffect(() => () => endResize(handleRef.current || undefined, dragRef.current?.pointerId), []);

  const updateWidth = (value: number) => onWidthChange(clamp(value, minWidth, maxWidth));

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { pointerId: event.pointerId, startClientX: event.clientX, startWidth: width };
    setIsResizing(true);
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    updateWidth(drag.startWidth + drag.startClientX - event.clientX);
  };

  const onPointerEnd = (event: PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    endResize(event.currentTarget, event.pointerId);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      updateWidth(width + step);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      updateWidth(width - step);
    } else if (event.key === 'Home') {
      event.preventDefault();
      updateWidth(minWidth);
    } else if (event.key === 'End') {
      event.preventDefault();
      updateWidth(maxWidth);
    }
  };

  return (
    <aside id="source-panel" className="source-panel" data-resizing={isResizing} aria-label="Source citation">
      <div
        ref={handleRef}
        className="source-resize-handle"
        role="separator"
        tabIndex={0}
        aria-label="调整来源面板宽度"
        aria-orientation="vertical"
        aria-controls="source-panel"
        aria-valuemin={minWidth}
        aria-valuemax={maxWidth}
        aria-valuenow={Math.round(width)}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        onLostPointerCapture={onPointerEnd}
        onKeyDown={onKeyDown}
      />
      <div className="source-header"><div><span className="eyebrow">SOURCE LOCATION</span><h2>{title}</h2></div><button type="button" className="close-button" onClick={onClose} aria-label="关闭来源面板">×</button></div>
      {details.length > 0 && <div className="source-card">{details.map(detail => <span key={detail}>{detail}</span>)}</div>}
      {sourceRange && <p className="source-range">{sourceRange}</p>}
      {hasLineRange ? (
        <div className="evidence-lines" aria-label="Evidence excerpt with source line numbers">
          {excerptLines.map((text, index) => {
            const lineNumber = record.excerpt_start_line! + index;
            const isStart = lineNumber === record.start_line;
            const isEnd = lineNumber === record.end_line;
            const marker = isStart && isEnd ? 'single' : isStart ? 'start' : isEnd ? 'end' : undefined;
            return (
              <div key={lineNumber} className="evidence-line" data-marker={marker}>
                <span className="evidence-line-number" aria-hidden="true">{lineNumber}</span>
                <span className="evidence-line-text">
                  {text || ' '}
                  {isStart && <span className="screen-reader-text"> Evidence start line.</span>}
                  {isEnd && <span className="screen-reader-text"> Evidence end line.</span>}
                </span>
              </div>
            );
          })}
        </div>
      ) : record.excerpt ? <blockquote>{record.excerpt}</blockquote> : null}
    </aside>
  );
}
