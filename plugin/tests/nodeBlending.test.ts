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

import { AdditiveBlending, NormalBlending, type Material, type ShaderMaterial } from 'three';
import { AggregateRenderer } from '../src/render/AggregateRenderer';
import { DAYLIGHT, DEEP_SPACE } from '../src/render/presets';
import { NODE_FRAGMENT_SHADER, NODE_VERTEX_SHADER } from '../src/render/shaders';
import { mergeSettings } from '../src/settings';
import type { GraphData, GraphNode } from '../src/types';

vi.stubGlobal('window', { devicePixelRatio: 1 });

const node = (id: string): GraphNode => ({
	id, name: id, folderTop: 'concept', degree: 1, inDegree: 1, outDegree: 0, fileSize: 0, tags: [], unresolved: false, tag: false,
});
const graph: GraphData = { nodes: [node('a'), node('b')], links: [{ source: 0, target: 1 }] };
const positions = new Float32Array([0, 0, 0, 10, 0, 0]);
const make = () => new AggregateRenderer({ appendChild(): void {} } as unknown as HTMLElement, 50);
const material = (r: AggregateRenderer) => (r as unknown as { nodeMaterial: ShaderMaterial }).nodeMaterial;
const glow = (r: AggregateRenderer) => material(r).uniforms['uGlow']?.value as number | undefined;
const links = (r: AggregateRenderer) => (r as unknown as { linkMaterial: Material }).linkMaterial;

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

//  A uniform the shader never declares is silently ignored by WebGL —— renaming `uGlow` in the GLSL alone kept every
//  test above green while the glow path went dead (2026-09-26 round-2 review).  Every uniform the material sets must
//  appear in the shader source.
describe('node material uniforms', () => {
	it('every uniform the material sets is declared in the shaders', () => {
		const r = make();
		r.setData(graph, positions);
		const src = NODE_VERTEX_SHADER + NODE_FRAGMENT_SHADER;
		for (const k of Object.keys(material(r).uniforms)) expect(src, k).toMatch(new RegExp(`uniform\\s+\\w+\\s+${k}\\b`));
	});
});

//  Daylight has no bloom: the kallimachos preset's faint links need a floor there, and a slider at 0 must still mean none.
describe('link opacity in daylight', () => {
	it('never drops below the floor, unless the links are switched off', () => {
		const r = make();
		r.setData(graph, positions);
		r.setLinkOpacity(0.02);
		r.applyTokens(DAYLIGHT, 0);
		expect(links(r).opacity).toBeGreaterThanOrEqual(0.045);
		r.applyTokens(DEEP_SPACE, 0.3);
		expect(links(r).opacity).toBeCloseTo(0.02, 5);
		r.applyTokens(DAYLIGHT, 0);
		r.setLinkOpacity(0);
		expect(links(r).opacity).toBe(0);
	});
});

//  Custom presets come from disk: anything but a real `true` is no glow, for every reader.
describe('custom presets from disk', () => {
	it('normalise additiveGlow to a boolean', () => {
		const base = { id: 'custom-x', name: 'x', starfield: false, theme: 'kallimachos', space: {}, bloom: { strength: 0, radius: 0, threshold: 0 },
			physics: { repel: 1, linkDistance: 1, linkStrength: 1, centerPull: 0, flatten: 0, coreGravity: 0, spiral: 0 },
			look: { nodeSize: 1, linkOpacity: 0.1, linkCurve: 0, twinkle: 0, sizeBy: 'degree' } };
		const s = mergeSettings({ customPresets: [{ ...base, additiveGlow: 'yes' }, { ...base, id: 'custom-y', additiveGlow: true }] });
		expect(s.customPresets.map((p) => p.additiveGlow)).toEqual([false, true]);
	});
});

