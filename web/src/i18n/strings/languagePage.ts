import type { Dict, Loose } from '../lang';

const en = {
  heading: 'Interface language',
  applies: 'Applies immediately and is remembered in this browser only.',
  hintEn: 'Default',
  hintKo: 'Korean',
} as const;

const ko: Loose<typeof en> = {
  heading: 'UI 언어',
  applies: '고르면 즉시 적용되며, 이 브라우저에만 저장됩니다.',
  hintEn: '기본값',
  hintKo: '한국어 전체',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
