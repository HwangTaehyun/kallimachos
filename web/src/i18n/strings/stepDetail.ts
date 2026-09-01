import type { Dict, Loose } from '../lang';

const en = {
	overwritesDb: '· overwrites the knowledge DB',
	requiresClaude: '· requires claude',
	unavailableNow: ' (unavailable now)',
	close: 'Close',
	runs: 'Runs',
	reads: 'Reads',
	writes: 'Writes',
	command: 'Command',
	lastRun: 'Last run',
	viewLog: 'View log',
	neverRun: 'Never',
	resultRunning: 'Running',
	resultOk: 'Success',
	resultFailed: 'Failed',
	resultCancelled: 'Cancelled',
	anotherStepRunning: 'Another step is running — only one runs at a time',
	run: 'Run',
} as const;

const ko: Loose<typeof en> = {
	overwritesDb: '· 지식 DB 를 덮어씀',
	requiresClaude: '· claude 필요',
	unavailableNow: ' (지금 못 씀)',
	close: '닫기',
	runs: '도는 단계',
	reads: '읽음',
	writes: '씀',
	command: '명령',
	lastRun: '마지막 실행',
	viewLog: '로그 보기',
	neverRun: '기록 없음',
	resultRunning: '실행 중',
	resultOk: '성공',
	resultFailed: '실패',
	resultCancelled: '취소됨',
	anotherStepRunning: '다른 단계가 실행 중입니다 — 하나씩만 돕니다',
	run: '실행',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
