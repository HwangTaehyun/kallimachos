import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { PipelineCanvas, type NodeShared } from '../src/components/PipelineCanvas';
import type { Run, Step } from '../src/api';

afterEach(cleanup);

const step = (id: string, order: number): Step => ({
	id, title: id, desc: '', group: 'main', order,
	cmd: [id], minutes: 1, needs_llm: false, writes_db: false,
});
const STEPS = [step('a', 1), step('b', 2), step('c', 3)];

const run = (id: string, stepId: string, status: Run['status']): Run => ({
	id, step: stepId, started_at: 1, status, lines: 0,
});

const shared = (runs: Run[]): NodeShared => ({
	rec: new Map(),
	byId: new Map(STEPS.map((s) => [s.id, s])),
	lastOf: (id) => runs.find((r) => r.step === id),
	busy: runs.some((r) => r.status === 'running'),
	llm: { ok: true, message: '' },
	onStart: () => {},
	onSelect: () => {},
});

/**
 * The pipeline canvas's **motion has to be true.**
 *
 * The rotating border and the flowing dashes are not there to be pretty; they say "this is running
 * right now".  Attached anywhere, the screen lies —— a stopped pipeline that looks like it is
 * working is worse than no indicator at all.
 */
describe('PipelineCanvas — the running indicator', () => {
	it('only the running step gets a rotating border', () => {
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} {...shared([run('r1', 'b', 'running')])} />,
		);
		const nodes = [...container.querySelectorAll('article')];
		expect(nodes).toHaveLength(3);
		expect(nodes.map((n) => n.classList.contains('gx-running'))).toEqual([false, true, false]);
	});

	it('with nothing running, no border rotates', () => {
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} {...shared([run('r1', 'b', 'ok')])} />,
		);
		expect(container.querySelectorAll('.gx-running')).toHaveLength(0);
	});

	it('the only fast-flowing line is **the one entering the running step**', () => {
		//  With b running, only the a→b line speeds up.  b→c has sent nothing yet.
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} {...shared([run('r1', 'b', 'running')])} />,
		);
		const conns = [...container.querySelectorAll('li svg')];
		expect(conns).toHaveLength(2);                       // 2 lines between 3 nodes
		expect(conns.map((c) => !!c.querySelector('.gx-flow-fast'))).toEqual([true, false]);
	});

	it('the dashes flow even normally (just slowly)', () => {
		//  Five motionless lines read as cards laid side by side —— the "this is a flow" marker has
		//  to be there whether or not anything is running.  What is running is separated by **speed and colour**.
		const { container } = render(<PipelineCanvas est={{}} steps={STEPS} {...shared([])} />);
		const conns = [...container.querySelectorAll('li svg')];
		expect(conns.map((c) => !!c.querySelector('.gx-flow'))).toEqual([true, true]);
		expect(container.querySelectorAll('.gx-flow-fast')).toHaveLength(0);
	});

	it('with the first step running there is no fast line (nothing precedes it)', () => {
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} {...shared([run('r1', 'a', 'running')])} />,
		);
		expect(container.querySelectorAll('.gx-flow-fast')).toHaveLength(0);
		expect(container.querySelectorAll('.gx-running')).toHaveLength(1);
	});

	it('it marks the flow’s start and end with a dot', () => {
		//  Without them the first node floats with no "where it comes from", and there is no telling
		//  whether what follows the last node is cut off or finished.
		const { container } = render(<PipelineCanvas est={{}} steps={STEPS} {...shared([])} />);
		expect(container.querySelectorAll('svg circle')).toHaveLength(2);
		//  ★ The caps are **outside** the list —— they are not steps, so they must not be li
		expect(container.querySelectorAll('li svg circle')).toHaveLength(0);
	});

	it('it puts no port dots on the nodes’ sides', () => {
		//  Two dots cut off the line's ends, so **the line read as not touching the nodes**.
		const { container } = render(<PipelineCanvas est={{}} steps={STEPS} {...shared([])} />);
		expect(container.querySelectorAll('article span[class*="-left-1"]')).toHaveLength(0);
		expect(container.querySelectorAll('article span[class*="-right-1"]')).toHaveLength(0);
	});

	it('the arrowhead **points right**', () => {
		//  With only a 1px grey line, five nodes read as cards merely laid side by side.
		//
		//  ⚠ This test used to count `path` elements alone.  Emptied to `d=""` the count is
		//    unchanged and it passes —— it really did miss a mutation deleting the arrowhead.
		//    It looks at **the shape**: of the three points, the middle one (the tip) has to be furthest right.
		const { container } = render(<PipelineCanvas est={{}} steps={STEPS} {...shared([])} />);
		const svgs = [...container.querySelectorAll('li svg')];
		expect(svgs).toHaveLength(2);
		for (const svg of svgs) {
			const paths = [...svg.querySelectorAll('path')];
			expect(paths).toHaveLength(2);                     // the line + the arrowhead
			const nums = (paths[1]!.getAttribute('d') ?? '').match(/-?\d+(\.\d+)?/g)?.map(Number) ?? [];
			expect(nums.length).toBeGreaterThanOrEqual(6);     // 3 points = 6 coordinates
			const xs = nums.filter((_, i) => i % 2 === 0);
			const ys = nums.filter((_, i) => i % 2 === 1);
			expect(Math.max(...xs)).toBe(xs[1]);               // the tip is the rightmost
			expect(ys[0]).toBeLessThan(ys[1]!);                // top → middle → bottom
			expect(ys[2]).toBeGreaterThan(ys[1]!);
		}
	});
});

describe('PipelineCanvas — group highlighting', () => {
	it('only the steps in the highlight set are lit and the rest dim', () => {
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} highlight={new Set(['b', 'c'])} {...shared([])} />,
		);
		const nodes = [...container.querySelectorAll('article')];
		expect(nodes.map((n) => n.className.includes('opacity-30'))).toEqual([true, false, false]);
		expect(nodes.map((n) => n.className.includes('ring-accent'))).toEqual([false, true, true]);
	});

	it('a line lights **only when both ends are lit**', () => {
		//  Lighting one with only one end lit leaks the highlight outside the group.
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} highlight={new Set(['b', 'c'])} {...shared([])} />,
		);
		const conns = [...container.querySelectorAll('li > div[aria-hidden]')];
		expect(conns).toHaveLength(2);
		expect(conns.map((c) => c.className.includes('text-accent'))).toEqual([false, true]);
		//  a→b dims along with a, because a is dark
		expect(conns.map((c) => c.className.includes('opacity-25'))).toEqual([true, false]);
	});

	it('with no highlight, nothing dims', () => {
		const { container } = render(<PipelineCanvas est={{}} steps={STEPS} highlight={null} {...shared([])} />);
		expect(container.querySelectorAll('.opacity-30')).toHaveLength(0);
		expect(container.querySelectorAll('.opacity-25')).toHaveLength(0);
	});

	it('the running indicator wins over the highlight', () => {
		//  A highlight lasts only while the mouse is over it; running is a fact.
		const { container } = render(
			<PipelineCanvas est={{}} steps={STEPS} highlight={new Set(['a'])} {...shared([run('r1', 'b', 'running')])} />,
		);
		const nodes = [...container.querySelectorAll('article')];
		expect(nodes[1]!.className).toContain('gx-running');
	});
});
