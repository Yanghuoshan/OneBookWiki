import { useState, type CSSProperties } from 'react';
import type { SourceOutlineNode, WikiPage, WikiSection, WikiStructure } from '../types/wiki';
import { sectionPages } from '../data/bookLoader';

type Props = {
  structure: WikiStructure;
  currentPageId?: string;
  onSelect: (page: WikiPage) => void;
};

type OutlineNodeProps = {
  node: SourceOutlineNode;
  pagesById: Map<string, WikiPage>;
  currentPageId?: string;
  collapsed: Record<string, boolean>;
  onToggle: (id: string) => void;
  onSelect: (page: WikiPage) => void;
  depth: number;
};

function pageLabel(page: WikiPage): string {
  if ((page.partCount || 1) > 1) return `Part ${page.part || 1} of ${page.partCount}`;
  return page.sourceTitle || page.title;
}

function sourceNodeContainsPage(node: SourceOutlineNode, pageId?: string): boolean {
  if (!pageId) return false;
  return node.pageIds.includes(pageId) || node.children.some(child => sourceNodeContainsPage(child, pageId));
}

function SourceOutlineItem({ node, pagesById, currentPageId, collapsed, onToggle, onSelect, depth }: OutlineNodeProps) {
  const pages = node.pageIds.map(id => pagesById.get(id)).filter((page): page is WikiPage => Boolean(page));
  const hasChildren = node.children.length > 0 || pages.length > 1;
  const isCollapsed = collapsed[node.id];
  const descendantSelected = node.children.some(child => sourceNodeContainsPage(child, currentPageId));
  const isCurrent = pages.some(page => page.id === currentPageId) || descendantSelected;
  const primaryPage = pages.length === 1 ? pages[0] : undefined;

  return (
    <div className="outline-node" style={{ '--tree-depth': depth } as CSSProperties}>
      <div className={`outline-node-header ${isCurrent ? 'contains-selection' : ''}`}>
        {hasChildren ? (
          <button className="outline-toggle" onClick={() => onToggle(node.id)} aria-label={`${isCollapsed ? '展开' : '收起'} ${node.title}`} aria-expanded={!isCollapsed}>
            {isCollapsed ? '▸' : '▾'}
          </button>
        ) : <span className="outline-spacer" />}
        {primaryPage ? (
          <button className={`outline-node-title ${currentPageId === primaryPage.id ? 'selected' : ''}`} onClick={() => onSelect(primaryPage)}>{node.title}</button>
        ) : <span className="outline-node-title">{node.title}</span>}
      </div>
      {!isCollapsed && hasChildren && (
        <div className="outline-children">
          {pages.length > 1 && pages.map(page => (
            <button key={page.id} className={`tree-page outline-page ${currentPageId === page.id ? 'selected' : ''}`} onClick={() => onSelect(page)}>
              <span>{pageLabel(page)}</span>
            </button>
          ))}
          {node.children.map(child => (
            <SourceOutlineItem
              key={child.id}
              node={child}
              pagesById={pagesById}
              currentPageId={currentPageId}
              collapsed={collapsed}
              onToggle={onToggle}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export default function PageTree({ structure, currentPageId, onSelect }: Props) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const pagesById = new Map(structure.pages.map(page => [page.id, page]));
  const toggle = (id: string) => setCollapsed(value => ({ ...value, [id]: !value[id] }));

  return (
    <nav className="page-tree" aria-label="Book navigation">
      <section className="tree-group source-outline-group" aria-label="Original book outline">
        <p className="tree-group-label">ORIGINAL BOOK</p>
        {structure.sourceOutline.map(node => (
          <SourceOutlineItem
            key={node.id}
            node={node}
            pagesById={pagesById}
            currentPageId={currentPageId}
            collapsed={collapsed}
            onToggle={toggle}
            onSelect={onSelect}
            depth={0}
          />
        ))}
      </section>
      {structure.sections.length > 0 && (
        <section className="tree-group derived-sections-group" aria-label="Derived reading guides">
          <p className="tree-group-label">DERIVED GUIDES</p>
          {structure.sections.map((section: WikiSection) => {
            const isCollapsed = collapsed[section.id];
            return (
              <section key={section.id} className="tree-section">
                <button className="section-toggle" onClick={() => toggle(section.id)} aria-expanded={!isCollapsed}>
                  <span>{isCollapsed ? '▸' : '▾'}</span>{section.title}
                </button>
                {!isCollapsed && sectionPages(structure, section).map(page => (
                  <button key={page.id} className={`tree-page derived-page ${currentPageId === page.id ? 'selected' : ''}`} onClick={() => onSelect(page)}>
                    <span>{page.title}</span>
                  </button>
                ))}
              </section>
            );
          })}
        </section>
      )}
    </nav>
  );
}
