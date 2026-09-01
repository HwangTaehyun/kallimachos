import type { Dict, Loose } from '../lang';

const en = {
  cancel: 'Cancel',
} as const;

const ko: Loose<typeof en> = {
  cancel: '취소',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
