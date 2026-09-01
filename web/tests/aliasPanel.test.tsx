import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';

/** Matching the real `Suggestion` shape.  A row splits the text as `{a} <span>({a_docs})</span>`,
 *  so it is found by regex rather than an exact match —— not knowing that broke the test twice. */
const sug = (a: string, b: string) => ({
  type: 't', reason: 'r', a, a_docs: 3, b, b_docs: 2, already_aliased: false,
});

/**
 * The first test in `web/`.
 *
 * The 2026-08-21 review found 6 usability defects and **every one was in `web/src`, where no check
 * could catch any of them** —— because this directory had zero test files.
 * One of them is a data loss: a failed load opens the editor **empty**, one character unlocks save,
 * and pressing it overwrites the real aliases.yml with that character.
 * The server passes `yaml.safe_load("x")`.
 *
 * What is locked down here is that defect.  And the other five with it.
 */
const mockApi = {
  aliases: vi.fn(),
  saveAliases: vi.fn(),   // ⚠ the real name —— written as putAliases it became an assertion holding nothing
  suggestions: vi.fn(),
};
vi.mock('../src/api', () => ({ api: mockApi }));

const load = async () => (await import('../src/components/AliasPanel')).AliasPanel;

const props = {
  onApply: () => {}, onApplyFast: () => {}, busy: false,
  otherDirty: false, onDirty: () => {},
};

beforeEach(() => {
  vi.resetModules();
  mockApi.aliases.mockReset();
  mockApi.saveAliases.mockReset().mockResolvedValue({ saved: true, note: '' });
  mockApi.suggestions.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

const saveBtn = () => screen.getByRole('button', { name: /^Save/ });
const textarea = () => screen.getByLabelText(/Alias file content/) as HTMLTextAreaElement;

describe('AliasPanel — stopping a failed load from wiping the file', () => {
  it('the mock has the same names as the real api surface', async () => {
    //  ★ Spying on a method that does not exist makes `not.toHaveBeenCalled()` always true —— which
    //    really happened, written as `putAliases`.  A name mismatch breaks here first.
    const real = await vi.importActual<typeof import('../src/api')>('../src/api');
    for (const k of Object.keys(mockApi)) {
      expect(Object.keys(real.api)).toContain(k);
    }
  });

  it('a rejected load locks the editor and save', async () => {
    mockApi.aliases.mockRejectedValue(new Error('500'));
    const AliasPanel = await load();
    render(<AliasPanel {...props} />);

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(textarea().disabled).toBe(true);
    expect((saveBtn() as HTMLButtonElement).disabled).toBe(true);
    //  ★ One character must not unlock save —— that is the path that wipes the file
    fireEvent.change(textarea(), { target: { value: 'x' } });
    expect((saveBtn() as HTMLButtonElement).disabled).toBe(true);
    expect(mockApi.saveAliases).not.toHaveBeenCalled();
  });

  it('a successful load opens it —— an empty file is treated as normal', async () => {
    mockApi.aliases.mockResolvedValue({ content: '' });
    const AliasPanel = await load();
    render(<AliasPanel {...props} />);

    await waitFor(() => expect(textarea().disabled).toBe(false));
    expect(screen.queryByRole('alert')).toBeNull();   // an empty file ≠ a failure
    expect((saveBtn() as HTMLButtonElement).disabled).toBe(true);   // nothing edited yet
    fireEvent.change(textarea(), { target: { value: 'Taehyun Hwang: [taehyun]' } });
    await waitFor(() => expect((saveBtn() as HTMLButtonElement).disabled).toBe(false));
  });

  it('loaded content is shown as-is', async () => {
    mockApi.aliases.mockResolvedValue({ content: 'Taehyun Hwang: [taehyun]\n' });
    const AliasPanel = await load();
    render(<AliasPanel {...props} />);
    await waitFor(() => expect(textarea().value).toBe('Taehyun Hwang: [taehyun]\n'));
  });
});

describe('AliasPanel — the candidate list', () => {
  const deferred = <T,>() => {
    let resolve!: (v: T) => void;
    const p = new Promise<T>((r) => { resolve = r; });
    return { p, resolve };
  };

  it('an earlier request arriving late cannot overwrite the screen', () => {
    //  ★ The real race: the 1st (weak=false) is **slow** and the 2nd (weak=true) arrives first.
    //    Without a generation guard, the late 1st overwrites the newest result and the checkbox is
    //    on while the list is the non-weak one, **permanently**.
    //    (This test was written backwards at first —— the later arrival was the newest, so no guard was exercised.)
    const slowFirst = deferred<unknown[]>();
    mockApi.aliases.mockResolvedValue({ content: '' });
    mockApi.suggestions
      .mockImplementationOnce(() => slowFirst.p)                                   // ① slow
      .mockImplementationOnce(() => Promise.resolve([sug('newest', 'result')]));

    return (async () => {
      const AliasPanel = await load();
      render(<AliasPanel {...props} />);
      //  Confirm the mount effect (①) has gone out before toggling —— otherwise the two requests'
      //  order differs from run to run.
      await waitFor(() => expect(mockApi.suggestions).toHaveBeenCalledTimes(1));
      fireEvent.click(screen.getByLabelText(/Include substring rules/));   // ② the request, arriving at once
      await waitFor(() => expect(mockApi.suggestions).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(screen.getByText(/newest/)).toBeTruthy());

      slowFirst.resolve([sug('late', 'old')]);      // ① arrives late
      await new Promise((r) => setTimeout(r, 10));

      expect(screen.queryByText(/late/)).toBeNull();      // it must not overwrite
      expect(screen.getByText(/newest/)).toBeTruthy();
    })();
  });

  it('a candidate-fetch failure appears by the candidates, not under the save button', async () => {
    mockApi.aliases.mockResolvedValue({ content: 'ok\n' });
    mockApi.suggestions.mockRejectedValue(new Error('python blew up'));
    const AliasPanel = await load();
    render(<AliasPanel {...props} />);
    await waitFor(() => expect(screen.getByText(/Failed to load candidates/)).toBeTruthy());
    //  The editor has to be fine —— a candidate failure must not block saving
    expect(textarea().disabled).toBe(false);
  });

  it('a locked editor locks the add button too —— there is nowhere to put it', async () => {
    mockApi.aliases.mockRejectedValue(new Error('500'));
    mockApi.suggestions.mockResolvedValue([sug('a', 'b')]);
    const AliasPanel = await load();
    render(<AliasPanel {...props} />);
    await waitFor(() => expect(textarea().disabled).toBe(true));
    const add = screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
  });
});
