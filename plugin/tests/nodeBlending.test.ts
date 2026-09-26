import { describe, expect, it, vi } from 'vitest';

//  The renderer is built for real —— only what needs a GPU is stood in for.  three's WebGLRenderer
//  cannot be constructed without a GL context, and the postprocessing passes allocate their render
//  targets through it; everything else (scene, geometry, materials) is plain JavaScript.
const stubs = vi.hoisted(() => {
	class Pass {
		enabled = true;
		strength = 0;
		radius = 0;
		threshold = 0;
		setSize(): void {}
		dispose(): void {}
	}
	class Composer {
		passes: unknown[] = [];
		addPass(p: unknown): void {
			this.passes.push(p);
		}
		setSize(): void {}
		setPixelRatio(): void {}
		render(): void {}
		dispose(): void {}
	}
	return { Pass, Composer };
});
vi.mock('three', async (importOriginal) => {
	const three = await importOriginal<typeof import('three')>();
	class WebGLRenderer {
		domElement = {};
		info = { autoReset: true, render: { calls: 0, points: 0, lines: 0, triangles: 0 }, reset(): void {} };
		toneMapping = 0;
		toneMappingExposure = 1;
		setPixelRatio(): void {}
		getPixelRatio(): number {
			return 1;
		}
		setSize(): void {}
		compile(): void {}
		dispose(): void {}
		forceContextLoss(): void {}
	}
	return { ...three, WebGLRenderer };
});
vi.mock('three/examples/jsm/postprocessing/EffectComposer.js', () => ({ EffectComposer: stubs.Composer }));
vi.mock('three/examples/jsm/postprocessing/RenderPass.js', () => ({ RenderPass: stubs.Pass }));
vi.mock('three/examples/jsm/postprocessing/UnrealBloomPass.js', () => ({ UnrealBloomPass: stubs.Pass }));
vi.mock('three/examples/jsm/postprocessing/OutputPass.js', () => ({ OutputPass: stubs.Pass }));

import { AdditiveBlending, NormalBlending, type ShaderMaterial } from 'three';
import { AggregateRenderer } from '../src/render/AggregateRenderer';
import { DAYLIGHT, DEEP_SPACE } from '../src/render/presets';
import type { GraphData, GraphNode } from '../src/types';

(globalThis as { window?: unknown }).window ??= { devicePixelRatio: 1 };

const node = (id: string): GraphNode => ({
	id, name: id, folderTop: 'concept', degree: 1, inDegree: 1, outDegree: 0, fileSize: 0, tags: [], unresolved: false, tag: false,
});
const graph: GraphData = { nodes: [node('a'), node('b')], links: [{ source: 0, target: 1 }] };
const positions = new Float32Array([0, 0, 0, 10, 0, 0]);
const make = () => new AggregateRenderer({ appendChild(): void {} } as unknown as HTMLElement, 50);
const material = (r: AggregateRenderer) => (r as unknown as { nodeMaterial: ShaderMaterial }).nodeMaterial;
const glow = (r: AggregateRenderer) => material(r).uniforms['uGlow']?.value as number | undefined;

describe('additive glow on the node material', () => {
	//  Every open rebuilds the data once more, after the settings were applied —— GraphStore's first
	//  ghost read changes its key from '' to '[]' and fires onChanged → setData (2026-09-26 review).
	//  setData makes a new material; if it does not re-apply the blending, the glow is gone on open.
	it('survives a data rebuild', () => {
		const r = make();
		r.setData(graph, positions);
		r.setNodeBlending(true);
		expect(material(r).blending).toBe(AdditiveBlending);
		r.setData(graph, positions);
		expect(material(r).blending).toBe(AdditiveBlending);
		expect(glow(r)).toBe(1);
	});

	it('is applied when it was asked for before the first data arrived', () => {
		const r = make();
		r.setNodeBlending(true);
		r.setData(graph, positions);
		expect(material(r).blending).toBe(AdditiveBlending);
	});

	it('gives way to daylight, and comes back with deep space —— across a rebuild too', () => {
		const r = make();
		r.setData(graph, positions);
		r.setNodeBlending(true);
		r.applyTokens(DAYLIGHT, 0.3);
		expect(material(r).blending).toBe(NormalBlending);
		expect(glow(r)).toBe(0);
		r.setData(graph, positions);
		expect(material(r).blending).toBe(NormalBlending);
		r.applyTokens(DEEP_SPACE, 0.3);
		expect(material(r).blending).toBe(AdditiveBlending);
		expect(glow(r)).toBe(1);
	});

	it('stays off for every other preset', () => {
		const r = make();
		r.setData(graph, positions);
		r.setNodeBlending(false);
		r.setData(graph, positions);
		expect(material(r).blending).toBe(NormalBlending);
		expect(glow(r)).toBe(0);
	});
});
