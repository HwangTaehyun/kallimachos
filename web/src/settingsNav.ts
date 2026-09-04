/**
 * The settings screen's contents list.
 *
 * Why data rather than JSX —— **because search walks this list**.  With the items scattered
 * through the sidebar's JSX, search would have to dig through the DOM or hold a second copy of
 * the list, and that copy would inevitably go stale.
 *
 * `keywords` is for people searching in words outside the label.  Someone typing "bm25" does not
 * know it lives inside "Chunking · Search" —— knowing that is search's job.
 */

import type { Lang } from './i18n/lang';

export type NavItem = {
	id: string;
	label: string;
	/** One line under the page title.  It does not appear in the sidebar —— putting it there turns the list into paragraphs. */
	desc: string;
	keywords: string[];
	/** A screen that needs width rather than a form (the pipeline canvas and so on) */
	wide?: boolean;
};

export type NavGroup = { title: string; items: NavItem[] };

/** Two values, en/ko.  The source (NAV_I18N) holds label, desc and title in this shape, and getNav() unfolds it per language. */
type Localized = { en: string; ko: string };
type NavItemRaw = Omit<NavItem, 'label' | 'desc'> & { label: Localized; desc: Localized };
type NavGroupRaw = { title: Localized; items: NavItemRaw[] };

const NAV_I18N: NavGroupRaw[] = [
	{
		title: { en: 'Operations', ko: '운영' },
		items: [
			{
				id: 'pipeline',
				label: { en: 'Pipeline', ko: '파이프라인' },
				desc: {
					en: 'Choose what to run, and watch it run.',
					ko: '무엇을 돌릴지 고르고, 돌아가는 것을 지켜본다.',
				},
				keywords: ['실행', '단계', '로그', 'run', 'step', '동기화', '추출', '색인', '내보내기'],
				wide: true,
			},
			{
				id: 'status',
				label: { en: 'Status', ko: '상태' },
				desc: {
					en: 'What is in it now, and what has gone stale.',
					ko: '지금 무엇이 들어 있고, 무엇이 뒤처졌나.',
				},
				keywords: ['문서', '엔티티', '청크 수', '디스크', '낡음', 'stale', 'drift'],
			},
		],
	},
	{
		title: { en: 'Knowledge graph', ko: '지식 그래프' },
		items: [
			{
				id: 'aliases',
				label: { en: 'Entity aliases', ko: '엔티티 별칭' },
				desc: {
					en: 'Merge cases where the same thing goes by different names.',
					ko: '같은 것을 다른 이름으로 부른 경우를 하나로 묶는다.',
				},
				keywords: ['alias', '동의어', '합치기', 'aliases.yml'],
			},
			{
				id: 'homonyms',
				label: { en: 'Homonyms', ko: '동음이의어' },
				desc: {
					en: 'Split cases where different things share the same name.',
					ko: '다른 것을 같은 이름으로 부른 경우를 갈라 놓는다.',
				},
				keywords: ['homonym', '분리', '가르기', 'homonyms.yml'],
			},
		],
	},
	{
		title: { en: 'Knowledge DB', ko: '지식 DB' },
		items: [
			{
				id: 'tuning',
				label: { en: 'Chunking · Search', ko: '청크 · 검색' },
				desc: {
					en: 'Document chunk size and search weighting. Changing these usually means rebuilding the knowledge DB.',
					ko: '문서를 자르는 크기와 검색 가중치. 바꾸면 대개 지식 DB 를 다시 만들어야 한다.',
				},
				keywords: ['청크', 'chunk', '겹침', 'overlap', 'bm25', 'ngram', '임베딩', 'embedding', '모델', '프리셋', 'k1', '색인', 'index', '재색인'],
			},
			{
				id: 'paths',
				label: { en: 'Paths', ko: '경로' },
				desc: {
					en: 'Where it reads from, and writes to.',
					ko: '어디를 읽고 어디에 쓰는가.',
				},
				keywords: ['vault', 'lancedb', 'kal', '폴더', 'path', '환경변수', 'env', '색인', 'db'],
			},
		],
	},
	{
		title: { en: 'General', ko: '일반' },
		items: [
			{
				id: 'language',
				label: { en: 'Language', ko: '언어' },
				desc: {
					en: 'Interface language for the local web UI. English is the default.',
					ko: '로컬 웹 UI 의 화면 언어. 기본값은 영어다.',
				},
				keywords: ['언어', '한국어', 'english', 'korean', 'lang', 'locale', 'i18n'],
			},
		],
	},
];

/** Flattens NAV_I18N to one language.  No value is missing from `ko`: the item list is fixed,
 *  so every entry carries both spellings and `en` is filled in ahead of time. */
export function getNav(lang: Lang): NavGroup[] {
	const pick = (s: Localized) => (lang === 'ko' ? s.ko : s.en);
	return NAV_I18N.map((g) => ({
		title: pick(g.title),
		items: g.items.map((it) => ({ ...it, label: pick(it.label), desc: pick(it.desc) })),
	}));
}

/** The navigation flattened to English —— the default for search and lookup.  Anywhere that
 *  knows the language (SettingsShell) calls `getNav(lang)` instead. */
export const NAV: NavGroup[] = getNav('en');

export const DEFAULT_ITEM = 'pipeline';

/** id → item, or `undefined` when there is none (a route that has gone stale). */
export function findItem(id: string, nav: NavGroup[] = NAV): NavItem | undefined {
	for (const g of nav) for (const it of g.items) if (it.id === id) return it;
	return undefined;
}

/**
 * Filters the navigation by a search term.  An empty string returns everything.
 *
 * A group left with no items **disappears whole**.  Leaving the heading behind reads as
 * "it is here and hidden" rather than "it is not here", which is the opposite of the truth.
 */
export function filterNav(q: string, nav: NavGroup[] = NAV): NavGroup[] {
	const t = q.trim().toLowerCase();
	if (!t) return nav;
	return nav
		.map((g) => ({
			title: g.title,
			items: g.items.filter((it) =>
				[it.label, it.desc, g.title, ...it.keywords]
					.some((s) => s.toLowerCase().includes(t)),
			),
		}))
		.filter((g) => g.items.length > 0);
}
