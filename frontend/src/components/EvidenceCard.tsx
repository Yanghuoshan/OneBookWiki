import type { EvidenceRecord } from '../types/wiki';
import { formatLocator, formatNativeLocator } from '../data/bookLoader';

type Props = {
  record: EvidenceRecord;
  cardId: string;
  highlighted?: boolean;
};

export default function EvidenceCard({ record, cardId, highlighted = false }: Props) {
  const label = formatLocator(record);
  const title = record.source_title || record.breadcrumb?.at(-1) || record.book_title;
  const location = formatNativeLocator(record);
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

  return (
    <div id={cardId} className="evidence-card" data-highlighted={highlighted} data-evidence-id={record.evidence_id}>
      <div className="evidence-card-header">
        <span className="evidence-card-label">{label}</span>
        {title && title !== label && <span className="evidence-card-title">{title}</span>}
      </div>
      {location && <p className="evidence-card-location">{location}</p>}
      {hasLineRange ? (
        <div className="evidence-lines evidence-lines--compact" aria-label="Evidence excerpt with source line numbers">
          {excerptLines.map((text, index) => {
            const lineNumber = record.excerpt_start_line! + index;
            const isStart = lineNumber === record.start_line;
            const isEnd = lineNumber === record.end_line;
            const marker = isStart && isEnd ? 'single' : isStart ? 'start' : isEnd ? 'end' : undefined;
            return (
              <div key={lineNumber} className="evidence-line" data-marker={marker}>
                <span className="evidence-line-number" aria-hidden="true">{lineNumber}</span>
                <span className="evidence-line-text">{text || ' '}</span>
              </div>
            );
          })}
        </div>
      ) : record.excerpt || record.quote ? (
        <blockquote>{record.excerpt || record.quote}</blockquote>
      ) : (
        <p className="evidence-card-empty">来源详情不可用</p>
      )}
    </div>
  );
}
