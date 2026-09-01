import type { Dict, Loose } from '../lang';

const en = {
  title: 'Run log',
  idle: 'Output streams here once you run a step',
  startedAt: (ago: string) => `Started ${ago}`,
  stick: 'Stick to bottom',
  stop: 'Stop',
  waiting: 'Waiting for output…',
  empty: 'Nothing has run yet.',
} as const;

const ko: Loose<typeof en> = {
  title: '실행 로그',
  idle: '단계를 실행하면 여기에 흐릅니다',
  startedAt: (ago: string) => `${ago} 시작`,
  stick: '바닥 따라가기',
  stop: '중지',
  waiting: '출력을 기다리는 중…',
  empty: '아직 실행한 것이 없습니다.',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
