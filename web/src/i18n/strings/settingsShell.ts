import type { Dict, Loose } from '../lang';

const en = {
	navLabel: 'Settings navigation',
	backToApp: 'Back to app',
	searchPlaceholder: 'Search settings',
	searchAriaLabel: 'Search settings',
	noMatches: (q: string) => `No settings match "${q}".`,
	settingsFallback: 'Settings',
} as const;

const ko: Loose<typeof en> = {
	navLabel: '설정 목차',
	backToApp: '앱으로 돌아가기',
	searchPlaceholder: '설정 검색',
	searchAriaLabel: '설정 검색',
	noMatches: (q: string) => `「${q}」에 맞는 설정이 없습니다.`,
	settingsFallback: '설정',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
