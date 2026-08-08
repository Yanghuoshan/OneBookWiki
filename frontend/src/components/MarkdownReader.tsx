import { Fragment } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import type { EvidenceIndex, EvidenceRecord, WikiPage } from '../types/wiki';
import CitationChip from './CitationChip';

const sanitizeSchema = {
  ...defaultSchema,
  protocols: {
    ...defaultSchema.protocols,
    href: [...(defaultSchema.protocols?.href || []), 'onebookwiki'],
  },
};

const citationPattern = /^C\d+E\d+(?!\d)$/;

type Props = {
  content: string;
  evidence: EvidenceIndex;
  pagePath: string;
  pagesByPath: Map<string, WikiPage>;
  onCitation: (record: EvidenceRecord) => void;
  onPageLink: (page: WikiPage) => void;
};

function linkedPage(href: string | undefined, pagePath: string, pagesByPath: Map<string, WikiPage>): WikiPage | undefined {
  if (!href || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(href)) return undefined;
  const target = new URL(href, new URL(pagePath, 'https://onebookwiki.local/wiki/'));
  if (!target.pathname.toLowerCase().endsWith('.md')) return undefined;
  const path = decodeURIComponent(target.pathname.replace(/^\/wiki\//, ''));
  return pagesByPath.get(path);
}

export default function MarkdownReader({ content, evidence, pagePath, pagesByPath, onCitation, onPageLink }: Props) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeSanitize, sanitizeSchema]]}
      components={{
        a({ href, children, ...props }) {
          const evidenceMatch = href?.match(/^onebookwiki:\/\/evidence\/(C\d+E\d+)$/);
          if (evidenceMatch) return <CitationChip id={evidenceMatch[1]} evidence={evidence} onOpen={onCitation} />;
          const page = linkedPage(href, pagePath, pagesByPath);
          if (page) return <button type="button" className="markdown-link" onClick={() => onPageLink(page)}>{children}</button>;
          return <a href={href} {...props}>{children}</a>;
        },
        p({ children }) {
          const items = Array.isArray(children) ? children : [children];
          return <p>{items.map((child, index) => {
            if (typeof child !== 'string') return <Fragment key={index}>{child}</Fragment>;
            const parts = child.split(/(C\d+E\d+)/g);
            return <Fragment key={index}>{parts.map((part, partIndex) => citationPattern.test(part) ? <CitationChip key={partIndex} id={part} evidence={evidence} onOpen={onCitation} /> : part)}</Fragment>;
          })}</p>;
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
