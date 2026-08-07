import type { EvidenceRecord } from '../types/wiki';
import { formatLocator } from '../data/bookLoader';

type Props = { record: EvidenceRecord | null; onClose: () => void };

export default function SourcePanel({ record, onClose }: Props) {
  if (!record) return null;
  return (
    <aside className="source-panel" aria-label="Source citation">
      <div className="source-header"><div><span className="eyebrow">SOURCE LOCATION</span><h2>{record.display_label || formatLocator(record)}</h2></div><button className="close-button" onClick={onClose} aria-label="关闭来源面板">×</button></div>
      <div className="source-card"><strong>{record.source_path}</strong><span>Chapter {record.chapter} · lines {record.start_line}-{record.end_line}</span></div>
      {record.excerpt && <blockquote>{record.excerpt}</blockquote>}
      <p className="source-note">PDF 引用使用 PDF 物理页序；EPUB 引用使用章节、spine 和 href 定位。</p>
    </aside>
  );
}
