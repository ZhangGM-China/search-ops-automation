import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import ExperimentHistoryList from './ExperimentHistoryList';

describe('ExperimentHistoryList', () => {
  it('delegates status, date and page filters to the server', () => {
    const onChange = vi.fn();
    render(<ExperimentHistoryList items={[{ id: 1, brand: 'atlas', status: 'ended', treatmentCount: 20, controlCount: 20 }]} total={45} page={1} pageSize={20} filters={{ page: 1, pageSize: 20 }} onOpen={vi.fn()} onChange={onChange} />);
    fireEvent.mouseDown(screen.getByLabelText('实验状态'));
    const option = screen.getAllByText('已结束').find((node) => node.closest('.ant-select-item-option'));
    fireEvent.click(option!);
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: 'ended', page: 1 }));
    fireEvent.click(screen.getByTitle('2'));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ page: 2 }));
  });
});
