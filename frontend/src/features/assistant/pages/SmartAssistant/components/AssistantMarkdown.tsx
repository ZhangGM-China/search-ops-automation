/* eslint-disable react-refresh/only-export-components -- renderer returns message fragments, not a routable component */
import React from 'react';
import { Typography } from 'antd';

const { Paragraph } = Typography;
const SAFE_URL_PATTERN = /^(https?:\/\/|\/|data:image\/)/i;
const safeUrl = (url: string) => (SAFE_URL_PATTERN.test(url.trim()) ? url.trim() : '');
const renderInlineMarkdown = (content: string) => content.split(/(!\[[^\]]*\]\([^)]+\)|\[[^\]]+\]\([^)]+\)|`[^`]+`|\*\*[^*]+\*\*)/g).filter(Boolean).map((segment, index) => {
  const image = segment.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
  if (image) { const src = safeUrl(image[2]); return src ? <img key={`${src}-${index}`} src={src} alt={image[1]} className="ops-smart-assistant-markdown-image" /> : segment; }
  const link = segment.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
  if (link) { const href = safeUrl(link[2]); return href ? <a key={`${href}-${index}`} href={href} target="_blank" rel="noreferrer">{link[1]}</a> : segment; }
  if (segment.startsWith('`') && segment.endsWith('`')) return <code key={`${segment}-${index}`}>{segment.slice(1, -1)}</code>;
  if (segment.startsWith('**') && segment.endsWith('**')) return <strong key={`${segment}-${index}`}>{segment.slice(2, -2)}</strong>;
  return <React.Fragment key={`${segment}-${index}`}>{segment}</React.Fragment>;
});

const isMarkdownTableDivider = (line: string) => /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$/.test(line.trim());
const parseMarkdownTableRow = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());

const MermaidPreview: React.FC<{ source: string }> = ({ source }) => {
  const edges = source.split('\n').map((line) => line.trim()).flatMap((line) => { const match = line.match(/^([^\s-]+)(?:\[[^\]]*\])?\s*--+>?\s*([^\s;]+)(?:\[[^\]]*\])?/); return match ? [{ from: match[1], to: match[2] }] : []; });
  const nodes = [...new Set(edges.flatMap((edge) => [edge.from, edge.to]))];
  return <figure className="ops-smart-assistant-mermaid" aria-label="Mermaid 图表"><figcaption>Mermaid</figcaption>{nodes.length ? <div className="ops-smart-assistant-mermaid__flow">{nodes.map((node, index) => <React.Fragment key={node}>{index ? <span className="ops-smart-assistant-mermaid__arrow" aria-hidden="true">→</span> : null}<span className="ops-smart-assistant-mermaid__node">{node}</span></React.Fragment>)}</div> : null}<details><summary>查看图表源码</summary><pre><code>{source}</code></pre></details></figure>;
};

export const renderMarkdownLite = (content: string) => {
  const nodes: React.ReactNode[] = [];
  let paragraphLines: string[] = []; let unorderedItems: string[] = []; let orderedItems: string[] = []; let orderedStart = 1; let codeLines: string[] = []; let codeLanguage = ''; let inCodeBlock = false; let nodeIndex = 0;
  const flushParagraph = () => { if (!paragraphLines.length) return; nodes.push(<Paragraph key={`paragraph-${nodeIndex}`} className="ops-smart-assistant-bubble__content">{paragraphLines.map((line, index) => <React.Fragment key={`${line}-${index}`}>{renderInlineMarkdown(line)}{index < paragraphLines.length - 1 ? <br /> : null}</React.Fragment>)}</Paragraph>); nodeIndex += 1; paragraphLines = []; };
  const flushLists = () => { if (unorderedItems.length) { nodes.push(<ul key={`list-${nodeIndex}`} className="ops-smart-assistant-markdown-list">{unorderedItems.map((item, index) => <li key={`${item}-${index}`}>{renderInlineMarkdown(item)}</li>)}</ul>); nodeIndex += 1; unorderedItems = []; } if (orderedItems.length) { nodes.push(<ol key={`ordered-list-${nodeIndex}`} start={orderedStart} className="ops-smart-assistant-markdown-list">{orderedItems.map((item, index) => <li key={`${item}-${index}`}>{renderInlineMarkdown(item)}</li>)}</ol>); nodeIndex += 1; orderedItems = []; orderedStart = 1; } };
  const flushCode = () => { const source = codeLines.join('\n'); nodes.push(codeLanguage === 'mermaid' ? <MermaidPreview key={`mermaid-${nodeIndex}`} source={source} /> : <pre key={`code-${nodeIndex}`} className="ops-smart-assistant-markdown-code" data-language={codeLanguage || undefined}><code>{source}</code></pre>); nodeIndex += 1; codeLines = []; codeLanguage = ''; };
  const flushTable = (header: string[], rows: string[][]) => { nodes.push(<div key={`table-shell-${nodeIndex}`} className="ops-smart-assistant-markdown-table-shell"><table className="ops-smart-assistant-markdown-table"><thead><tr>{header.map((cell, index) => <th key={`${cell}-${index}`}>{renderInlineMarkdown(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={`row-${rowIndex}`}>{row.map((cell, cellIndex) => <td key={`${rowIndex}-${cellIndex}`}>{renderInlineMarkdown(cell)}</td>)}</tr>)}</tbody></table></div>); nodeIndex += 1; };
  const lines = content.split('\n');
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith('```')) { if (inCodeBlock) { flushCode(); inCodeBlock = false; } else { flushParagraph(); flushLists(); codeLanguage = line.trim().slice(3).trim().toLowerCase(); inCodeBlock = true; } continue; }
    if (inCodeBlock) { codeLines.push(line); continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)$/); if (heading) { flushParagraph(); flushLists(); const level = Math.min(heading[1].length + 1, 6); nodes.push(React.createElement(`h${level}`, { key: `heading-${nodeIndex}`, className: 'ops-smart-assistant-markdown-heading' }, renderInlineMarkdown(heading[2]))); nodeIndex += 1; continue; }
    const quote = line.match(/^>\s?(.*)$/); if (quote) { flushParagraph(); flushLists(); nodes.push(<blockquote key={`quote-${nodeIndex}`} className="ops-smart-assistant-markdown-quote">{renderInlineMarkdown(quote[1])}</blockquote>); nodeIndex += 1; continue; }
    const nextLine = lines[index + 1] || ''; if (line.includes('|') && isMarkdownTableDivider(nextLine)) { flushParagraph(); flushLists(); const header = parseMarkdownTableRow(line); const rows: string[][] = []; index += 2; while (index < lines.length && lines[index].includes('|') && lines[index].trim()) { rows.push(parseMarkdownTableRow(lines[index])); index += 1; } flushTable(header, rows); index -= 1; continue; }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/); if (bullet) { flushParagraph(); if (orderedItems.length) flushLists(); unorderedItems.push(bullet[1]); continue; }
    const ordered = line.match(/^\s*(\d+)\.\s+(.+)$/); if (ordered) { flushParagraph(); if (unorderedItems.length) flushLists(); if (!orderedItems.length) orderedStart = Number(ordered[1]); orderedItems.push(ordered[2]); continue; }
    if (!line.trim()) { flushParagraph(); flushLists(); continue; }
    flushLists(); paragraphLines.push(line);
  }
  if (inCodeBlock || codeLines.length) flushCode(); flushParagraph(); flushLists(); return nodes;
};
