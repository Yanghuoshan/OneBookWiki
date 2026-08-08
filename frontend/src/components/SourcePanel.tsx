import type { EvidenceRecord } from '../types/wiki';
import { formatEpubLocator, formatLocator, formatPdfRange, formatSourceType } from '../data/bookLoader';

type Props = { record: EvidenceRecord | null; onClose: () => void };

export default function SourcePanel({ record, onClose }: Props) {
  if (!record) return null;
  const title = record.source_title || record.breadcrumb?.at(-1) || record.book_title || formatLocator(record);
  const details = Array.from(new Set([
    record.book_title,
    record.breadcrumb?.length ? record.breadcrumb.join(' › ') : undefined,
    formatSourceType(record.source_type),
    formatPdfRange(record.physical_page_start, record.physical_page_end),
    formatEpubLocator(record),
    formatLocator(record),
  ].filter((detail): detail is string => Boolean(detail))));

  return (
    <aside className="source-panel" aria-label="Source citation">
      <div className="source-header"><div><span className="eyebrow">SOURCE LOCATION</span><h2>{title}</h2></div><button className="close-button" onClick={onClose} aria-label="关闭来源面板">×</button></div>
      {details.length > 0 && <div className="source-card">{details.map(detail => <span key={detail}>{detail}</span>)}</div>}
      {record.excerpt && <blockquote>{record.excerpt}</blockquote>}
    </aside>
  );
}
