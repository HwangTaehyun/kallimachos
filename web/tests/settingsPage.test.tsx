import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent, act } from '@testing-library/react';
import STATUS from './fixtures-status.json';

//  Copied straight from a real `status.py --json` (with only the personal paths substituted).  A
//  thin hand-invented object made the component throw on `Object.entries(undefined)` —— guessing
//  the shape means the test checks my imagination rather than the product.

/**
 * Holds the two defects round 5 found.  Both are cases of **the code contradicting its comment**.
 *   · polling hijacks the run the user chose —— the comment said "it is left alone"
 *   · the next poll erases an operation failure message —— the banner is gone in under a second
 */
const mockApi = {
  status: vi.fn(), runs: vi.fn(), steps: vi.fn(), llm: vi.fn(),
  //  The estimate is supplementary —— an empty object makes the screen fall back to the fixed values.
  estimate: vi.fn(async () => ({})),
  start: vi.fn(), cancel: vi.fn(), aliases: vi.fn(), homonyms: vi.fn(),
  suggestions: vi.fn(), homonymSuggestions: vi.fn(),
};
vi.mock('../src/api', () => ({ api: mockApi, streamLog: () => () => {} }));

const RUNNING = { id: 'r-running', step: 'sync', status: 'running', started: '2026-08-21T00:00:00Z' };
const DONE = { id: 'r-done', step: 'export', status: 'ok', started: '2026-08-20T00:00:00Z' };

beforeEach(() => {
  vi.resetModules();
  Object.values(mockApi).forEach((f) => f.mockReset());
  mockApi.status.mockResolvedValue(STATUS);
  mockApi.runs.mockResolvedValue([RUNNING, DONE]);
  //  ⚠ `api.steps()` is `{steps, groups}`, not an array.  Left as an array it became
  //    `setSteps(undefined)` and StepList threw —— the mock was wrong, not the product.
  //    Do not guess the shape.
  mockApi.steps.mockResolvedValue({ steps: [], groups: [] });
  mockApi.llm.mockResolvedValue({ ok: true, message: '' });
  mockApi.aliases.mockResolvedValue({ content: '' });
  mockApi.homonyms.mockResolvedValue({ content: '' });
  mockApi.suggestions.mockResolvedValue([]);
  mockApi.homonymSuggestions.mockResolvedValue([]);
});
afterEach(cleanup);

const load = async () => (await import('../src/components/SettingsPage')).SettingsPage;

describe('SettingsPage — does an operation failure message survive', () => {
  it('the next poll does not erase a stop failure message', async () => {
    mockApi.cancel.mockRejectedValue(new Error('not running'));
    const SettingsPage = await load();
    render(<SettingsPage />);
    await waitFor(() => expect(mockApi.runs).toHaveBeenCalled());

    const stop = await screen.findByRole('button', { name: /Stop/ });
    await act(async () => { fireEvent.click(stop); });
    //  ★ The banner has to appear, and the refresh right after it must not clear it
    await waitFor(() => expect(screen.getByText(/not running/)).toBeTruthy());
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByText(/not running/)).toBeTruthy();
    //  It goes away only on close
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    await waitFor(() => expect(screen.queryByText(/not running/)).toBeNull());
  });
});

describe('SettingsPage — does polling hijack the user’s choice', () => {
  //  The comment said "if the user is looking at a past run they chose, it is left alone" and the
  //  code did the opposite: a chosen run that had **finished** went back to the running one every
  //  5 seconds, and `useLogStream` cleared the log with `setLines([])` —— a past log was unreadable.
  const STEP = {
    id: 'export', title: 'Export the graph', desc: '', group: 'main', order: 0,
    cmd: ['export_all.sh'], minutes: 2, needs_llm: false, writes_db: false,
    reads: [], writes: [],
  };
  const GROUP = { id: 'main', title: 'The main path', desc: '' };

  it('polling does not override a run chosen directly', async () => {
    vi.useFakeTimers();
    try {
      mockApi.steps.mockResolvedValue({ steps: [STEP], groups: [GROUP] });
      const SettingsPage = await load();
      render(<SettingsPage />);
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });

      //  polling is attached to the running run
      const stop = screen.queryByRole('button', { name: /Stop/ });
      expect(stop).toBeTruthy();

      //  the user chooses a **finished** run
      fireEvent.click(screen.getByRole('button', { name: /Last run/ }));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.queryByRole('button', { name: /Stop/ })).toBeNull();   // a finished run has no stop

      //  ★ the selection has to survive several polls
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.queryByRole('button', { name: /Stop/ })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
