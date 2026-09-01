import type { Dict, Loose } from '../lang';

const en = {
  heading: 'Something went wrong',
  bodyPre: 'Please paste the details below into an issue. Including the output of ',
  bodyPost: ' would help too.',
  retry: 'Retry',
} as const;

const ko: Loose<typeof en> = {
  heading: '화면을 그리다 멈췄습니다',
  bodyPre: '아래 내용을 이슈에 붙여 주십시오. ',
  bodyPost: ' 의 출력도 함께면 더 좋습니다.',
  retry: '다시 그려 보기',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
