import type { Dict, Loose } from '../lang';

const en = {
	instant: 'instant',
	seconds: (n: number) => `${n}s`,
	minutes: (n: number) => `${n}min`,
	hours: (h: string) => `${h}h`,
	sentenceFallback: (n: number) => `Takes about ${n}min`,
	workUnits: (units: string, unit: string) => `Work: ${units} ${unit} · `,
	estimated: (sec: string, basis: string) => `estimated ${sec} (${basis})`,
} as const;

const ko: Loose<typeof en> = {
	instant: '즉시',
	seconds: (n: number) => `${n}초`,
	minutes: (n: number) => `${n}분`,
	hours: (h: string) => `${h}시간`,
	sentenceFallback: (n: number) => `약 ${n}분 걸립니다`,
	workUnits: (units: string, unit: string) => `할 일 ${units} ${unit} · `,
	estimated: (sec: string, basis: string) => `예상 ${sec} (${basis})`,
};

export const S: Dict<typeof en> = { en, ko };
export default S;
