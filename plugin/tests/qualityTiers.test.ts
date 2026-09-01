import { describe, expect, it } from 'vitest';
import { effectivePixelRatio } from '../src/quality/tiers';

describe('effectivePixelRatio', () => {
	it('keeps the device value when the device DPR is below the tier cap', () => {
		expect(effectivePixelRatio(1, 2)).toBe(1);
	});

	it('applies the tier cap when the device DPR is above it', () => {
		expect(effectivePixelRatio(3, 1.5)).toBe(1.5);
	});
});
