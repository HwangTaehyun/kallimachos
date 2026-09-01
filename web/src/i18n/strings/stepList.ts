import type { Dict, Loose } from '../lang';

const en = {
	running: 'Running',
	ok: 'Success',
	failed: 'Failed',
	cancelled: 'Cancelled',
	anotherRunning: 'Another step is running',
	busyTitle: 'Another step is running — only one runs at a time',
	runAll: 'Run all',
	reads: (n: string) => `Reads ${n}`,
	writes: (n: string) => `Writes ${n}`,
	recommended: 'Recommended',
	overwritesDb: 'Overwrites DB',
	claudeUnavailable: 'claude unavailable',
	lastRun: 'Last run',
	noRunsYet: 'No runs yet',
	run: 'Run',
} as const;

const ko: Loose<typeof en> = {
	running: '실행 중',
	ok: '성공',
	failed: '실패',
	cancelled: '취소됨',
	anotherRunning: '다른 단계가 실행 중',
	busyTitle: '다른 단계가 실행 중입니다 — 하나씩만 돕니다',
	runAll: '전체 실행',
	reads: (n: string) => `읽음 ${n}`,
	writes: (n: string) => `씀 ${n}`,
	recommended: '추천',
	overwritesDb: 'DB 를 덮어씀',
	claudeUnavailable: 'claude 없음',
	lastRun: '마지막 실행',
	noRunsYet: '기록 없음',
	run: '실행',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
