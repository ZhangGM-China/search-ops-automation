import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import MarkdownContent from './MarkdownContent';

describe('MarkdownContent', () => {
  it('renders headings, inline tokens and lists', () => {
    render(<MarkdownContent className="custom" content={'# 标题\n普通 **加粗** `代码` [链接](https://example.com)\n\n- 一\n- 二\n\n1. 三'} />);
    expect(screen.getByRole('heading', { name: '标题' })).toBeInTheDocument();
    expect(screen.getByText('加粗')).toBeInTheDocument();
    expect(screen.getByText('代码')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '链接' })).toHaveAttribute('href', 'https://example.com');
    expect(screen.getAllByRole('list')).toHaveLength(2);
  });
  it('renders fenced code and markdown tables', () => {
    render(<MarkdownContent content={'```\nconst x = 1\n```\n\n| 名称 | 值 |\n| --- | --- |\n| A | 1 |'} />);
    expect(screen.getByText('const x = 1')).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByText('A')).toBeInTheDocument();
  });
  it('handles empty content', () => {
    const { container } = render(<MarkdownContent content="" />);
    expect(container.querySelector('.ops-markdown-content')).toBeInTheDocument();
  });
});
