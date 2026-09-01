import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { StepList } from '../src/components/StepList';
import type { Group, Run, Step } from '../src/api';

beforeAll(() => {
	//  jsdom does not implement `<dialog>`'s showModal/close.  Without them the render throws.
	//  Only the minimum is imitated —— the open attribute and the close event.  What this test
	//  looks at is **what appears on screen**, not top-layer behaviour.
	const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
	if (!proto.showModal) {
		proto.showModal = function (this: HTMLDialogElement) { this.open = true; };
		proto.close = function (this: HTMLDialogElement) {
			this.open = false;
			this.dispatchEvent(new Event('close'));
		};
	}
});
afterEach(cleanup);

const LONG = 'An LLM extracts entities and relations from every 2,400-character chunk.  It is not called when the chunk hash matches.';

const STEPS: Step[] = [
	{ id: 'extract', title: 'Extract the knowledge graph', desc: LONG, group: 'main', order: 1,
		cmd: ['lr_extract.py'], minutes: 30, needs_llm: true, writes_db: false,
		reads: 'vault **/*.md', writes: '~/.kal/lr_kg.json' },
	{ id: 'index', title: 'Rebuild the knowledge DB', desc: 'Short.', group: 'main', order: 2,
		cmd: ['build_index.py'], minutes: 3, needs_llm: false, writes_db: true },
	{ id: 'refresh_kg', title: 'Refresh the stale KG', desc: 'The group description.', group: 'combo', order: 0,
		cmd: ['refresh.sh'], minutes: 35, needs_llm: false, writes_db: true,
		runs: ['extract', 'index'] },
];
const GROUPS: Group[] = [
	{ id: 'main', title: 'The whole flow', desc: '' },
	{ id: 'combo', title: 'Combined runs', desc: '' },
];
const RUN: Run = { id: 'r1', step: 'extract', started_at: 1, status: 'ok', lines: 0, origin: 'cli' };

const view = (over: Partial<Parameters<typeof StepList>[0]> = {}) => {
	const onStart = vi.fn(), onSelect = vi.fn();
	const r = render(
		<StepList est={{}} steps={STEPS} groups={GROUPS} status={null} runs={[RUN]}
			busy={false} llm={null} onStart={onStart} onSelect={onSelect} {...over} />,
	);
	return { ...r, onStart, onSelect };
};

const openNode = (title: string) =>
	fireEvent.click(screen.getByRole('heading', { name: title }).closest('button')!);

describe('StepDetail — the full text of a truncated description', () => {
	it('pressing the node body gives the full text', () => {
		//  A node is 208px wide, so the description is cut at two lines.  The part cut off is
		//  "it is not called", and that decides whether running this step twice is safe.
		view();
		expect(screen.queryByRole('dialog')).toBeNull();
		openNode('Extract the knowledge graph');
		const dlg = screen.getByRole('dialog');
		expect(dlg.textContent).toContain('It is not called');
	});

	it('it shows the read, write and command that were only in the tooltip', () => {
		//  A `title` tooltip needs a second of hovering and the keyboard cannot reach it at all.
		view();
		openNode('Extract the knowledge graph');
		const dlg = screen.getByRole('dialog');
		expect(dlg.textContent).toContain('vault **/*.md');
		expect(dlg.textContent).toContain('~/.kal/lr_kg.json');
		expect(dlg.textContent).toContain('lr_extract.py');
	});

	it('there is a way to the last run and its log', () => {
		const { onSelect } = view();
		openNode('Extract the knowledge graph');
		fireEvent.click(screen.getByRole('button', { name: /View log/ }));
		expect(onSelect).toHaveBeenCalledWith('r1');
	});

	it('running **closes first**, then starts', () => {
		//  Without closing, the confirmation stacks on top of this one and there is no telling which it confirms.
		const { onStart } = view();
		openNode('Extract the knowledge graph');
		fireEvent.click(screen.getByRole('dialog').querySelector('footer button:last-child')!);
		expect(onStart).toHaveBeenCalledWith('extract');
		//  Closing unmounts it entirely —— a stronger guarantee than `open=false`.
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('the node’s "run" button does not open the detail', () => {
		//  With the body a button and another button inside it, clicks leak easily.
		const { onStart } = view();
		const node = screen.getByRole('heading', { name: 'Rebuild the knowledge DB' }).closest('article')!;
		fireEvent.click([...node.querySelectorAll('button')].find((b) => b.textContent === 'Run')!);
		expect(onStart).toHaveBeenCalledWith('index');
		expect(screen.queryByRole('dialog')).toBeNull();
	});

	it('it opens from a group card too', () => {
		view();
		fireEvent.click(screen.getByRole('heading', { name: 'Refresh the stale KG' }).closest('button')!);
		const dlg = screen.getByRole('dialog');
		expect(dlg.textContent).toContain('The group description');
		//  A group also shows what it runs, by name
		expect(dlg.textContent).toContain('Extract the knowledge graph');
		expect(dlg.textContent).toContain('Rebuild the knowledge DB');
	});

	it('with no claude, running is locked and the reason is visible', () => {
		view({ llm: { ok: false, message: 'not logged in to claude' } });
		openNode('Extract the knowledge graph');
		const run = screen.getByRole('dialog').querySelector('footer button:last-child') as HTMLButtonElement;
		expect(run.disabled).toBe(true);
		expect(screen.getByRole('dialog').textContent).toContain('unavailable now');
	});

	it('focus goes to close —— not to run', () => {
		//  Pressing Enter the moment it opens must not just start a 30-minute job.
		view();
		openNode('Extract the knowledge graph');
		expect((document.activeElement as HTMLElement)?.getAttribute('aria-label')).toBe('Close');
	});
});
