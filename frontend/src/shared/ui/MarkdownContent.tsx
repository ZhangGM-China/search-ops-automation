import React from 'react';
import { Typography } from 'antd';

const { Paragraph } = Typography;

type MarkdownContentProps = {
  content: string;
  className?: string;
};

const inlineTokenPattern = /(\[[^\]]+\]\(https?:\/\/[^)]+\)|`[^`]+`|\*\*[^*]+\*\*)/g;

const renderInlineMarkdown = (content: string) => content
  .split(inlineTokenPattern)
  .filter(Boolean)
  .map((segment, index) => {
    const linkMatch = segment.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/);
    if (linkMatch) {
      return (
        <a key={`${segment}-${index}`} href={linkMatch[2]} target="_blank" rel="noreferrer">
          {linkMatch[1]}
        </a>
      );
    }
    if (segment.startsWith('`') && segment.endsWith('`')) {
      return <code key={`${segment}-${index}`}>{segment.slice(1, -1)}</code>;
    }
    if (segment.startsWith('**') && segment.endsWith('**')) {
      return <strong key={`${segment}-${index}`}>{segment.slice(2, -2)}</strong>;
    }
    return <React.Fragment key={`${segment}-${index}`}>{segment}</React.Fragment>;
  });

const isMarkdownTableDivider = (line: string) => /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$/.test(line.trim());

const parseMarkdownTableRow = (line: string) => line
  .trim()
  .replace(/^\|/, '')
  .replace(/\|$/, '')
  .split('|')
  .map((cell) => cell.trim());

const MarkdownContent: React.FC<MarkdownContentProps> = ({ content, className }) => {
  const nodes: React.ReactNode[] = [];
  let paragraphLines: string[] = [];
  let listItems: string[] = [];
  let orderedList = false;
  let codeLines: string[] = [];
  let inCodeBlock = false;
  let nodeIndex = 0;
  const lines = String(content || '').split('\n');

  const flushParagraph = () => {
    if (!paragraphLines.length) return;
    nodes.push(
      <Paragraph key={`paragraph-${nodeIndex}`} className="ops-smart-assistant-bubble__content">
        {paragraphLines.map((line, lineIndex) => (
          <React.Fragment key={`${line}-${lineIndex}`}>
            {renderInlineMarkdown(line)}
            {lineIndex < paragraphLines.length - 1 ? <br /> : null}
          </React.Fragment>
        ))}
      </Paragraph>,
    );
    nodeIndex += 1;
    paragraphLines = [];
  };

  const flushList = () => {
    if (!listItems.length) return;
    const TagName = orderedList ? 'ol' : 'ul';
    nodes.push(
      <TagName key={`list-${nodeIndex}`} className="ops-smart-assistant-markdown-list">
        {listItems.map((item, itemIndex) => (
          <li key={`${item}-${itemIndex}`}>{renderInlineMarkdown(item)}</li>
        ))}
      </TagName>,
    );
    nodeIndex += 1;
    listItems = [];
    orderedList = false;
  };

  const flushCode = () => {
    nodes.push(
      <pre key={`code-${nodeIndex}`} className="ops-smart-assistant-markdown-code">
        <code>{codeLines.join('\n')}</code>
      </pre>,
    );
    nodeIndex += 1;
    codeLines = [];
  };

  const flushTable = (header: string[], rows: string[][]) => {
    nodes.push(
      <div key={`table-shell-${nodeIndex}`} className="ops-smart-assistant-markdown-table-shell">
        <table className="ops-smart-assistant-markdown-table">
          <thead>
            <tr>
              {header.map((cell, index) => <th key={`${cell}-${index}`}>{renderInlineMarkdown(cell)}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`row-${rowIndex}`}>
                {row.map((cell, cellIndex) => (
                  <td key={`${rowIndex}-${cellIndex}`}>{renderInlineMarkdown(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>,
    );
    nodeIndex += 1;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith('```')) {
      if (inCodeBlock) {
        flushCode();
        inCodeBlock = false;
      } else {
        flushParagraph();
        flushList();
        inCodeBlock = true;
      }
      continue;
    }
    if (inCodeBlock) {
      codeLines.push(line);
      continue;
    }

    const nextLine = lines[index + 1] || '';
    if (line.includes('|') && isMarkdownTableDivider(nextLine)) {
      flushParagraph();
      flushList();
      const header = parseMarkdownTableRow(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|')) {
        rows.push(parseMarkdownTableRow(lines[index]));
        index += 1;
      }
      index -= 1;
      flushTable(header, rows);
      continue;
    }

    const trimmedLine = line.trim();
    if (!trimmedLine) {
      flushParagraph();
      flushList();
      continue;
    }

    const headingMatch = trimmedLine.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const HeadingTag = `h${Math.min(headingMatch[1].length + 2, 6)}` as keyof React.JSX.IntrinsicElements;
      nodes.push(
        <HeadingTag key={`heading-${nodeIndex}`} className="ops-markdown-heading">
          {renderInlineMarkdown(headingMatch[2])}
        </HeadingTag>,
      );
      nodeIndex += 1;
      continue;
    }

    const unorderedMatch = trimmedLine.match(/^[-*]\s+(.+)$/);
    const orderedMatch = trimmedLine.match(/^\d+\.\s+(.+)$/);
    if (unorderedMatch || orderedMatch) {
      flushParagraph();
      const nextOrdered = Boolean(orderedMatch);
      if (listItems.length && orderedList !== nextOrdered) flushList();
      orderedList = nextOrdered;
      listItems.push((orderedMatch || unorderedMatch)?.[1] || '');
      continue;
    }

    flushList();
    paragraphLines.push(line);
  }

  if (inCodeBlock) flushCode();
  flushParagraph();
  flushList();

  return <div className={`ops-markdown-content${className ? ` ${className}` : ''}`}>{nodes}</div>;
};

export default MarkdownContent;
