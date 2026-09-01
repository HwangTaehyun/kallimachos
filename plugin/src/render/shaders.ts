// Nodes = a THREE.Points in one draw call + a glowing-orb shader (the NASA "luminous orb" recipe)
// Deep-space mode: a white-hot core + soft edges; the light "daylight" mode: a solid ink disc + a dark rim (bloom off)
// aDim: non-neighbours fade out in focus mode (0.12..1)

import { CURVE_BOW } from './linkCurves';
import { REVEAL_ACTIVE_SPAN, REVEAL_DELAY_SPAN } from './reveal';

const REVEAL_POSITION_GLSL = /* glsl */ `
vec3 revealPosition(vec3 targetPosition) {
	if (uRevealActive < 0.5) return targetPosition;
	float delay = (length(targetPosition) / max(uRevealMaxRadius, 0.0001)) * ${REVEAL_DELAY_SPAN.toFixed(2)};
	float localProgress = clamp((uRevealProgress - delay) / ${REVEAL_ACTIVE_SPAN.toFixed(2)}, 0.0, 1.0);
	float scale = 1.0 - pow(1.0 - localProgress, 3.0);
	return targetPosition * scale;
}
`;

export const NODE_VERTEX_SHADER = /* glsl */ `
attribute float aSize;
attribute float aGhost;
attribute float aDim;
varying vec3 vColor;
varying float vGhost;
varying float vDim;
uniform float uPixelScale; // drawingBufferHeight / (2·tan(fov/2))
uniform float uSizeMul; // the control panel's "node size" multiplier
uniform float uMaxPoint; // a device-pixel clamp: stops full-screen giant sprites blowing the fill rate while flying through a cluster (M3)
uniform float uRevealActive;
uniform float uRevealProgress;
uniform float uRevealMaxRadius;

${REVEAL_POSITION_GLSL}

void main() {
	vColor = color;
	vGhost = aGhost;
	vDim = aDim;
	vec4 mv = modelViewMatrix * vec4(revealPosition(position), 1.0);
	gl_PointSize = min(aSize * uSizeMul * uPixelScale / max(-mv.z, 1.0), uMaxPoint);
	gl_Position = projectionMatrix * mv;
}
`;

export const NODE_FRAGMENT_SHADER = /* glsl */ `
varying vec3 vColor;
varying float vGhost;
varying float vDim;
uniform float uLightMode; // 0 = deep space (a white-hot core), 1 = daylight (an ink disc + a rim)

void main() {
	vec2 uv = gl_PointCoord - 0.5;
	float d = length(uv);

	float core = smoothstep(0.18, 0.0, d) * 0.55 * (1.0 - vGhost) * (1.0 - uLightMode);
	vec3 col = mix(vColor, vec3(1.0), core);

	// Daylight: a 1px dark rim on the outer edge, so a node "sits on the paper"
	float rim = smoothstep(0.40, 0.46, d) * smoothstep(0.50, 0.46, d);
	col = mix(col, col * 0.72, rim * uLightMode);

	float alpha = smoothstep(0.5, 0.42, d) * mix(1.0, 0.45, vGhost) * vDim;
	if (alpha < 0.01) discard;
	gl_FragColor = vec4(col, alpha);
}
`;

const REVEAL_LINK_VERTEX_DECLARATIONS = /* glsl */ `
attribute vec3 aSourcePosition;
attribute vec3 aTargetPosition;
attribute float aCurveT;
uniform float uLinkCurvature;
uniform float uRevealActive;
uniform float uRevealProgress;
uniform float uRevealMaxRadius;

${REVEAL_POSITION_GLSL}
`;

const REVEAL_LINK_BEGIN_VERTEX = /* glsl */ `
	vec3 source = revealPosition(aSourcePosition);
	vec3 target = revealPosition(aTargetPosition);
	vec3 midpoint = (source + target) * 0.5;
	vec3 edge = target - source;
	float edgeLength = length(edge);
	float midpointRadius = length(midpoint);
	vec3 direction;
	if (midpointRadius > 0.001) {
		direction = midpoint / midpointRadius;
	} else {
		float perpendicularLength = length(vec2(edge.z, edge.x));
		direction = perpendicularLength > 0.000001
			? vec3(edge.z / perpendicularLength, 0.0, -edge.x / perpendicularLength)
			: vec3(1.0, 0.0, 0.0);
	}
	vec3 control = midpoint + direction * (uLinkCurvature * ${CURVE_BOW.toFixed(2)} * edgeLength);
	float inverseT = 1.0 - aCurveT;
	vec3 curvePosition =
		inverseT * inverseT * source +
		2.0 * inverseT * aCurveT * control +
		aCurveT * aCurveT * target;
	vec3 transformed = curvePosition;
`;

export interface RevealLineShader {
	uniforms: Record<string, { value: unknown }>;
	vertexShader: string;
	fragmentShader: string;
}

export interface RevealLineUniforms {
	uLinkCurvature: { value: number };
	uRevealActive: { value: number };
	uRevealProgress: { value: number };
	uRevealMaxRadius: { value: number };
}

/**
 * Replaces only the vertex-position entry point of Three r184's native line material.  Colour, opacity, dashes, fog,
 * tone mapping and the output colorspace all stay in LineBasic/LineDashedMaterial's original pipeline,
 * so switching back to the ordinary material when the animation ends produces no colour jump.
 */
export function patchRevealLineShader(shader: RevealLineShader, uniforms: RevealLineUniforms): void {
	const mainAnchor = 'void main() {';
	const positionAnchor = '#include <begin_vertex>';
	if (!shader.vertexShader.includes(mainAnchor) || !shader.vertexShader.includes(positionAnchor)) {
		throw new Error('Three line shader anchors changed; reveal patch cannot be applied safely');
	}
	Object.assign(shader.uniforms, uniforms);
	shader.vertexShader = shader.vertexShader
		.replace(mainAnchor, `${REVEAL_LINK_VERTEX_DECLARATIONS}\n${mainAnchor}`)
		.replace(positionAnchor, REVEAL_LINK_BEGIN_VERTEX);
}
