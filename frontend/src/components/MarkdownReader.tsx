import { Fragment } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import type { EvidenceIndex, EvidenceRecord } from '../types/wiki';

const sanitizeSchema = {
  ...defaultSchema,
  protocols: {
    ...defaultSchema.protocols,
    href: [...(defaultSchema.protocols?.href || []), 'onebookwiki'],
  },
};
import CitationChip from './CitationChip';

type Props = { content: string; evidence: EvidenceIndex; onCitation: (record: EvidenceRecord) => void };
const citationPattern = /^C\d+E\d+(?!\d)$/;

export default function MarkdownReader({ content, evidence, onCitation }: Props) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeSanitize, sanitizeSchema]]}
      components={{
        a({ href, children, ...props }) {
          const match = href?.match(/^onebookwiki:\/\/evidence\/(C\d+E\d+)$/);
          if (match) return <CitationChip id={match[1]} evidence={evidence} onOpen={onCitation} />;
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
