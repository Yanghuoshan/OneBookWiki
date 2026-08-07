import { useState } from 'react';
import type { WikiPage, WikiSection, WikiStructure } from '../types/wiki';
import { sectionPages } from '../data/bookLoader';

type Props = {
  structure: WikiStructure;
  currentPageId?: string;
  onSelect: (page: WikiPage) => void;
};

export default function PageTree({ structure, currentPageId, onSelect }: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const sections = structure.sections.length ? structure.sections : [{ id: 'all', title: 'Pages', pages: structure.pages.map(page => page.id) }];
  return (
    <nav className="page-tree" aria-label="Wiki pages">
      {sections.map((section: WikiSection) => {
        const isCollapsed = collapsed[section.id];
        return (
          <section key={section.id} className="tree-section">
            <button className="section-toggle" onClick={() => setCollapsed(value => ({ ...value, [section.id]: !value[section.id] }))} aria-expanded={!isCollapsed}>
              <span>{isCollapsed ? '▸' : '▾'}</span>{section.title}
            </button>
            {!isCollapsed && sectionPages(structure, section).map(page => (
              <button key={page.id} className={`tree-page ${currentPageId === page.id ? 'selected' : ''}`} onClick={() => onSelect(page)}>
                <span className="page-kind">{page.kind === 'chapter' ? `CH ${page.chapter ?? ''}` : page.kind.toUpperCase()}</span>
                <span>{page.title}</span>
              </button>
            ))}
          </section>
        );
      })}
    </nav>
  );
}
