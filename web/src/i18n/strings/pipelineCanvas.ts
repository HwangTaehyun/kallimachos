import type { Dict, Loose } from '../lang';
import stepList from './stepList';

const en = {
	...stepList.en,
	mainFlow: 'Main pipeline flow',
} as const;

const ko: Loose<typeof en> = {
	...stepList.ko,
	mainFlow: '주 파이프라인 흐름',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
