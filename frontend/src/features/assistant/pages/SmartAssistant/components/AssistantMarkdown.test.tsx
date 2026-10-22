import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { renderMarkdownLite } from './AssistantMarkdown';

describe('renderMarkdownLite', () => {
  it('renders all markdown structures', () => {
    render(<div>{renderMarkdownLite('**bold** `code`\n\n- one\n- two\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n```\nconst x = 1\n```')}</div>);
    expect(screen.getByText('bold')).toBeInTheDocument();
    expect(screen.getByText('code')).toBeInTheDocument();
    expect(screen.getAllByRole('list')).toHaveLength(1);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('const x = 1')).toBeInTheDocument();
  });
  it('handles plain multiline text and unclosed code', () => {
    render(<div>{renderMarkdownLite('line one\nline two\n```\npartial')}</div>);
    expect(screen.getByText(/line one/)).toBeInTheDocument();
    expect(screen.getByText('partial')).toBeInTheDocument();
  });

  it('renders headings, links, quotes, ordered lists, images and mermaid blocks', () => {
    render(<div>{renderMarkdownLite('# 标题\n\n> 引用\n\n1. first\n2. second\n\n[文档](https://example.com/doc)\n\n![结果图](https://example.com/result.png)\n\n```mermaid\ngraph TD\nA-->B\n```')}</div>);

    expect(screen.getByRole('heading', { name: '标题' })).toBeInTheDocument();
    expect(screen.getByText('引用').closest('blockquote')).toBeInTheDocument();
    expect(screen.getByRole('list')).toHaveAttribute('start', '1');
    expect(screen.getByRole('link', { name: '文档' })).toHaveAttribute('href', 'https://example.com/doc');
    expect(screen.getByRole('img', { name: '结果图' })).toHaveAttribute('src', 'https://example.com/result.png');
    expect(screen.getByLabelText('Mermaid 图表')).toHaveTextContent('A');
    expect(screen.getByLabelText('Mermaid 图表')).toHaveTextContent('B');
  });
});
