import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConfigPanel } from '../src/components/ConfigPanel';
import { api, type ConfigView } from '../src/api';

vi.mock('../src/api', async () => {
  const actual = await vi.importActual<typeof import('../src/api')>('../src/api');
  return { ...actual, api: { config: vi.fn(), saveConfig: vi.fn() } };
});
const mockApi = api as unknown as {
  config: ReturnType<typeof vi.fn>;
  saveConfig: ReturnType<typeof vi.fn>;
};

/** The server's response shape is owned by `src/kal_config.py --json`.  The fixture here copies
 *  that shape exactly —— only the values are cut down from a real response. */
const view = (over: Partial<ConfigView> = {}): ConfigView => ({
  config_path: '/data/kal/config.json',
  in_container: true,
  paths: [{
    key: 'vault', label: 'the note root to index', value: '/vault', source: 'env',
    env: 'KAL_VAULT', default: '/home/u/notes', exists: true, editable: false,
    how: 'edit `.env` and `docker compose up -d`',
  }],
  settings: [
    {
      key: 'chunk_chars', value: 500, source: 'default', default: 500, type: 'int',
      env: 'KAL_CHUNK_CHARS', reindex: true, min: 100, max: 4000,
      label: 'chunk size (chars)', help: 'how many characters to cut a document into.',
    },
    {
      key: 'bm25_k1', value: 1.2, source: 'env', default: 1.2, type: 'float',
      env: 'KAL_BM25_K1', reindex: true, min: 0.1, max: 3,
      label: 'BM25 k1', help: 'term-frequency saturation.',
    },
  ],
  drift: [],
  error: '',
  ...over,
});

//  ⚠ With `globals: false`, RTL's automatic cleanup is not registered —— without this, the previous
//    test's DOM remains and breaks with "Found multiple elements".  The sibling tests state it too.
afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.config.mockResolvedValue(view());
});

describe('ConfigPanel — does it separate what can and cannot be changed', () => {
  it('a value set by an environment variable is **locked, with the reason visible**', async () => {
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    //  A locked field must not be merely grey —— why it cannot be touched has to be written, or it
    //  reads as broken.  If the reason disappears, this test breaks.
    const locked = await screen.findByLabelText('BM25 k1');
    expect((locked as HTMLInputElement).disabled).toBe(true);
    //  Matching on the environment-variable word alone would also catch the summary at the bottom
    //  ("1 value set by an environment variable is locked").  Only the **reason** sentence is picked.
    expect(screen.getByText(/can't be changed here/)).toBeTruthy();
    expect(screen.getByText('KAL_BM25_K1')).toBeTruthy();

    const open = screen.getByLabelText('chunk size (chars)');
    expect((open as HTMLInputElement).disabled).toBe(false);
  });

  it('a path shows **how to change it** rather than an input', async () => {
    render(<ConfigPanel busy={false} onReindex={() => {}} show="paths" />);
    expect(await screen.findByText('/vault')).toBeTruthy();
    //  Give a path an input and it becomes "a field where pressing does nothing" —— it is a bind
    //  mount in a container, so changing the value alone leaves a path that does not exist.
    expect(screen.queryByLabelText('the note root to index')).toBeNull();
    expect(screen.getByText(/docker compose up -d/)).toBeTruthy();
  });

  it('a group description appears even with no title', async () => {
    //  ⚠ Wrapping the header in `title &&` made **a group given a description but no title lose
    //    the description too.**  "It cannot be changed from the screen in a container" had vanished
    //    from the screen, and without that line a grey path just reads as broken.
    render(<ConfigPanel busy={false} onReindex={() => {}} show="paths" />);
    expect(await screen.findByText(/Can't be changed from the UI in the container/)).toBeTruthy();
  });

  it('no chip on a default, only on what was touched', async () => {
    //  On a fresh install **everything** is a default, so a "default" chip puts an identical chip on
    //  every row and distinguishes nothing.  A marker means something only on the exception.
    const { container } = render(<ConfigPanel busy={false} onReindex={() => {}} />);
    await screen.findByLabelText('chunk size (chars)');
    //  ⚠ It must not be checked by text —— a chip rendering as `default` instead of the localised
    //    word still leaves a queryByText on the localised word null, and it passes (measured: this
    //    test failed to catch that mutation).  It looks at **how many chips there are**.
    const chips = [...container.querySelectorAll('[data-source]')].map((e) => e.getAttribute('data-source'));
    expect(chips).toEqual(['env']);   // the default rows carry no chip
    //  A value an environment variable won is still shown —— that is the exception.
    expect(screen.getByText('env: KAL_BM25_K1')).toBeTruthy();
  });

  it('it splits **into groups** by whether a re-index is needed (rather than a chip per row)', async () => {
    //  As chips it puts one on seven of eight rows —— colouring seven to distinguish one, which is
    //  backwards, and the warning colour becomes a background pattern.
    mockApi.config.mockResolvedValue(view({
      settings: [
        ...view().settings,
        {
          key: 'search_preset', value: 'default', source: 'default', default: 'default',
          type: 'str', env: 'KAL_SEARCH_PRESET', reindex: false, min: null, max: null,
          label: 'the search weight preset', help: 'used only at query time.',
        },
      ],
    }));
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    expect(await screen.findByText('Requires rebuilding the knowledge DB')).toBeTruthy();
    expect(screen.getByText('Applied immediately')).toBeTruthy();
    //  ★ The heading appears **once**, not once per row.
    //
    //  ⚠ This used to look for a Korean chip string that exists nowhere in
    //    web/src —— always null, always passing, testing nothing.  A per-row chip regression
    //    would have sailed straight through.  Counting the heading can actually fail.
    expect(screen.getAllByText('Requires rebuilding the knowledge DB')).toHaveLength(1);
  });

  it('a group with no values needing it does not appear at all', async () => {
    //  An empty group heading alone reads as "it is here but I cannot see it".
    render(<ConfigPanel busy={false} onReindex={() => {}} />);   // the fixture has reindex: true for both
    expect(await screen.findByText('Requires rebuilding the knowledge DB')).toBeTruthy();
    expect(screen.queryByText('Applied immediately')).toBeNull();
  });

  it('a string field is wider than a number field', async () => {
    //  `intfloat/multilingual-e5-small` was truncated to `intfloat/multiling…`, so **the screen
    //  could not say which model it was**.
    mockApi.config.mockResolvedValue(view({
      settings: [
        ...view().settings,
        {
          key: 'embedding_model', value: 'intfloat/multilingual-e5-small',
          source: 'default', default: 'intfloat/multilingual-e5-small', type: 'str',
          env: 'KAL_EMBEDDING_MODEL', reindex: true, min: null, max: null,
          label: 'the embedding model', help: 'changing it changes the vector dimension.',
        },
      ],
    }));
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    const str = await screen.findByLabelText('the embedding model');
    const num = screen.getByLabelText('chunk size (chars)');
    expect(str.className).toContain('w-72');
    expect(num.className).toContain('w-40');
  });

  it('paths and tuning values are **different pages**', async () => {
    //  Stacked on one screen it reads as "everything here can be changed" —— a path cannot be.
    //  The contents list separated them, so the render has to separate them too.
    const { unmount } = render(<ConfigPanel busy={false} onReindex={() => {}} show="tuning" />);
    expect(await screen.findByLabelText('chunk size (chars)')).toBeTruthy();
    expect(screen.queryByText('/vault')).toBeNull();
    unmount();

    render(<ConfigPanel busy={false} onReindex={() => {}} show="paths" />);
    expect(await screen.findByText('/vault')).toBeTruthy();
    expect(screen.queryByLabelText('chunk size (chars)')).toBeNull();
  });

  it('save is locked when nothing changed and unlocks when something does', async () => {
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    const save = await screen.findByRole('button', { name: /^Save/ });
    expect((save as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText('chunk size (chars)'), { target: { value: '900' } });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    expect(save.textContent).toContain('1');       // the number changed

    //  Returning to the same value has to lock it again —— otherwise a PUT goes out with nothing
    //  changed and the server writes it into the file as "an explicitly set value".
    fireEvent.change(screen.getByLabelText('chunk size (chars)'), { target: { value: '500' } });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(true));
  });

  it('saving sends **only the changed keys**', async () => {
    mockApi.saveConfig.mockResolvedValue(view());
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    fireEvent.change(await screen.findByLabelText('chunk size (chars)'), { target: { value: '900' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    await waitFor(() => expect(mockApi.saveConfig).toHaveBeenCalledTimes(1));
    //  Sending untouched keys too makes the server record them all as "set values" in the file ——
    //  and a later change to the defaults leaves this deployment tied to the old ones.
    expect(mockApi.saveConfig).toHaveBeenCalledWith({ chunk_chars: '900' });
  });

  it('a server rejection shows **its reason verbatim**', async () => {
    mockApi.saveConfig.mockRejectedValue(new Error('chunk_overlap: must be smaller than the chunk size'));
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    fireEvent.change(await screen.findByLabelText('chunk size (chars)'), { target: { value: '900' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    //  Replaced with words like "save failed", the user has no idea what to fix.
    expect(await screen.findByText(/must be smaller than the chunk size/)).toBeTruthy();
  });

  it('a mismatch against the DB shows a banner and a re-index button', async () => {
    mockApi.config.mockResolvedValue(view({
      drift: [{ key: 'chunk_chars', now: '900', db: '500' }],
    }));
    const onReindex = vi.fn();
    render(<ConfigPanel busy={false} onReindex={onReindex} />);
    expect(await screen.findByText(/differ from the knowledge DB/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Rebuild now/ }));
    expect(onReindex).toHaveBeenCalledTimes(1);
  });

  it('the re-index button is locked while another step is running', async () => {
    mockApi.config.mockResolvedValue(view({
      drift: [{ key: 'chunk_chars', now: '900', db: '500' }],
    }));
    render(<ConfigPanel busy={true} onReindex={() => {}} />);
    const b = await screen.findByRole('button', { name: /Rebuild now/ });
    expect((b as HTMLButtonElement).disabled).toBe(true);
  });

  it('a corrupted config.json does not quietly show the defaults', async () => {
    mockApi.config.mockResolvedValue(view({ error: 'could not read config.json' }));
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText(/could not read/)).toBeTruthy();
  });
});

describe('ConfigPanel — does a late response erase the user input', () => {
  it('what was typed survives a second config response arriving', async () => {
    //  StrictMode runs the effect twice in development and `/api/config` takes 1.5 seconds because
    //  of Python + LanceDB.  What was typed in that window must not disappear ——
    //  measured (2026-08-22), all three fields went back to their original values, with no error.
    let resolveSecond: (v: ConfigView) => void = () => {};
    mockApi.config
      .mockResolvedValueOnce(view())
      .mockReturnValueOnce(new Promise<ConfigView>((r) => { resolveSecond = r; }));

    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    const input = await screen.findByLabelText('chunk size (chars)');
    fireEvent.change(input, { target: { value: '900' } });
    const save = screen.getByRole('button', { name: /^Save/ });
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));

    //  now the late response arrives
    resolveSecond(view());
    await waitFor(() => expect(mockApi.config).toHaveBeenCalledTimes(1));
    //  ↑ This component calls it once per mount, but StrictMode and a re-fetch can bring a second.
    //    The point is below: the value has to survive.
    expect((input as HTMLInputElement).value).toBe('900');
    expect((save as HTMLButtonElement).disabled).toBe(false);
  });

  it('a successful save clears the draft', async () => {
    mockApi.saveConfig.mockResolvedValue(view({
      settings: [{ ...view().settings[0]!, value: 900, source: 'file' }, view().settings[1]!],
    }));
    render(<ConfigPanel busy={false} onReindex={() => {}} />);
    fireEvent.change(await screen.findByLabelText('chunk size (chars)'), { target: { value: '900' } });
    fireEvent.click(screen.getByRole('button', { name: /^Save/ }));
    //  After saving, the server's value shows and dirty clears —— otherwise it looks like
    //  "saved and still looking unsaved".
    await waitFor(() =>
      expect((screen.getByRole('button', { name: /^Save/ }) as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByLabelText('chunk size (chars)') as HTMLInputElement).value).toBe('900');
  });
});

/**  Comparing the DB's origin —— the "Paths" screen.
 *
 *  Why it is needed: the `paths` above are "where this server is set to read".  Indexing also runs
 *  from the CLI (`KAL_VAULT=/other just run index`), and then **the same DB is rebuilt from a
 *  different vault**.  The screen keeps showing the old path knowing nothing about it, and
 *  "Status" reads every document as 'deleted' —— with the cause nowhere on screen.
 *
 *  What this check holds is one thing: "when they diverge, does it say so".  Quietly saying
 *  "match" is worse than saying nothing.
 */
describe('ConfigPanel — the vault this DB was built from', () => {
  const st = (built: string, now: string) =>
    ({ db: { vault_path: built, vault_now: now } }) as never;

  it('says so when they diverge, and shows what is being read now alongside', async () => {
    mockApi.config.mockResolvedValue(view());
    render(<ConfigPanel show="paths" busy={false} onReindex={() => {}}
      status={st('/Users/me/other-notes', '/Users/me/notes')} />);
    expect(await screen.findByText('Differs from current path')).toBeTruthy();
    expect(screen.getByText(/\/Users\/me\/other-notes/)).toBeTruthy();
    //  When they diverge it has to say **what must not be trusted**
    expect(screen.getByText(/can't be trusted/)).toBeTruthy();
  });

  it('says match when they are the same', async () => {
    mockApi.config.mockResolvedValue(view());
    render(<ConfigPanel show="paths" busy={false} onReindex={() => {}}
      status={st('/Users/me/notes', '/Users/me/notes')} />);
    expect(await screen.findByText('Match')).toBeTruthy();
    expect(screen.queryByText('Differs from current path')).toBeNull();
  });

  it('says nothing while status is absent —— it does not assert what it does not know', async () => {
    mockApi.config.mockResolvedValue(view());
    render(<ConfigPanel show="paths" busy={false} onReindex={() => {}} status={null} />);
    await screen.findByText('the note root to index');
    expect(screen.queryByText('Match')).toBeNull();
    expect(screen.queryByText('Differs from current path')).toBeNull();
  });

  it('does not assert when an old DB leaves vault_now empty', async () => {
    mockApi.config.mockResolvedValue(view());
    render(<ConfigPanel show="paths" busy={false} onReindex={() => {}}
      status={st('/Users/me/notes', '')} />);
    expect(await screen.findByText('Match')).toBeTruthy();
    expect(screen.queryByText('Differs from current path')).toBeNull();
  });
});
