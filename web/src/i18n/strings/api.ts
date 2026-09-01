import type { Dict, Loose } from '../lang';

const en = {
  graphReadFailed: (status: number) => `Couldn't read graph (HTTP ${status})`,
} as const;

const ko: Loose<typeof en> = {
  graphReadFailed: (status: number) => `그래프를 못 읽었습니다 (HTTP ${status})`,
};

export const S: Dict<typeof en> = { en, ko };
export default S;
