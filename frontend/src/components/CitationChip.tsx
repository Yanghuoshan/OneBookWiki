import type { EvidenceIndex, EvidenceRecord } from '../types/wiki';
import { formatLocator, resolveEvidence } from '../data/bookLoader';

type Props = {
  id: string;
  evidence: EvidenceIndex;
  onOpen: (record: EvidenceRecord) => void;
};

export default function CitationChip({ id, evidence, onOpen }: Props) {
  const record = resolveEvidence(id, evidence);
  if (!record) return <span className="citation-missing">来源不可用</span>;
  return <button className="citation-chip" onClick={() => onOpen(record)} title="打开来源定位">{record.display_label || formatLocator(record)}</button>;
}
