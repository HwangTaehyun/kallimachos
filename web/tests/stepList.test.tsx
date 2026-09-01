import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { StepList } from '../src/components/StepList';
import type { Group, Run, Step } from '../src/api';

afterEach(cleanup);

const step = (o: Partial<Step> & { id: string }): Step => ({
	title: o.id, desc: '', group: 'main', order: 0, cmd: [o.id], minutes: 1,
	needs_llm: false, writes_db: false, ...o,
});

const STEPS: Step[] = [
	step({ id: 'distill', title: 'Distil the sessions', order: 1 }),
	step({ id: 'extract', title: 'Extract the knowledge graph', order: 2 }),
	step({ id: 'index', title: 'Rebuild the knowledge DB', order: 3 }),
	step({ id: 'export', title: 'Export the graph', order: 4 }),
	step({ id: 'refresh_kg', title: 'Refresh the stale KG', group: 'combo', minutes: 35,
		runs: ['extract', 'index', 'export'] }),
	step({ id: 'rebuild_all', title: 'Rebuild everything', group: 'combo', minutes: 60,
		desc: 'Everything from distilling to exporting.',
		runs: ['distill', 'extract', 'index', 'export'] }),
];
const GROUPS: Group[] = [
	{ id: 'main', title: 'The whole flow', desc: '' },
	{ id: 'combo', title: 'Combined runs', desc: '' },
];

const view = (over: Partial<Parameters<typeof StepList>[0]> = {}) => {
	const onStart = vi.fn();
	const r = render(
		<StepList est={{}} steps={STEPS} groups={GROUPS} status={null} runs={[] as Run[]}
			busy={false} llm={null} onStart={onStart} onSelect={() => {}} {...over} />,
	);
	return { ...r, onStart };
};

const dimmed = (c: HTMLElement) =>
	[...c.querySelectorAll('article')].map((n) => n.className.includes('opacity-30'));

describe('StepList — run everything', () => {
	it('"run everything" sits at the head of the flow and runs rebuild_all', () => {
		//  Running the six steps in order is the thing most often needed, and it was buried as one
		//  of the three group cards.
		const { onStart } = view();
		const btn = screen.getByRole('button', { name: /Run all/ });
		fireEvent.click(btn);
		expect(onStart).toHaveBeenCalledWith('rebuild_all');
	});

	it('it states the duration alongside', () => {
		view();
		expect(screen.getByRole('button', { name: /Run all/ }).textContent).toContain('60min');
	});

	it('it is locked while another step is running', () => {
		view({ busy: true });
		expect((screen.getByRole('button', { name: /Run all/ }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('with no rebuild_all there is no button either', () => {
		//  A button pointing at a step that does not exist does nothing when pressed —— better not shown at all.
		view({ steps: STEPS.filter((s) => s.id !== 'rebuild_all') });
		expect(screen.queryByRole('button', { name: /Run all/ })).toBeNull();
	});
});

describe('StepList — hovering a group lights up the flow', () => {
	it('only the steps that group runs are lit', () => {
		const { container } = view();
		expect(dimmed(container)).toEqual([false, false, false, false]);   // normally

		fireEvent.mouseEnter(screen.getByText('Refresh the stale KG').closest('li')!);
		//  refresh_kg = extract · index · export → only distill is dimmed
		expect(dimmed(container)).toEqual([true, false, false, false]);
	});

	it('moving the mouse away restores it', () => {
		const { container } = view();
		const card = screen.getByText('Refresh the stale KG').closest('li')!;
		fireEvent.mouseEnter(card);
		expect(dimmed(container)).toEqual([true, false, false, false]);
		fireEvent.mouseLeave(card);
		expect(dimmed(container)).toEqual([false, false, false, false]);
	});

	it('it responds to keyboard focus the same way', () => {
		//  Mouse-only means whoever tabs through never sees this connection.
		const { container } = view();
		fireEvent.focus(screen.getByText('Refresh the stale KG').closest('li')!);
		expect(dimmed(container)).toEqual([true, false, false, false]);
	});

	it('hovering "run everything" lights all of them', () => {
		const { container } = view();
		fireEvent.mouseEnter(screen.getByRole('button', { name: /Run all/ }));
		expect(dimmed(container)).toEqual([false, false, false, false]);
		expect([...container.querySelectorAll('article')]
			.every((n) => n.className.includes('ring-accent'))).toBe(true);
	});
});
