import type { Dict, Loose } from '../lang';

const en = {
	never: 'Never',
	justNow: 'just now',
	mAgo: (n: number) => `${n}m ago`,
	hAgo: (n: number) => `${n}h ago`,
	dAgo: (n: number) => `${n}d ago`,
	severityNow: 'Now',
	severitySoon: 'Soon',
	severityLater: 'Later',
	knowledgeDb: 'Knowledge DB',
	lastChecked: (agoStr: string) => `Last checked: ${agoStr}`,
	documents: 'Documents',
	entities: 'Entities',
	relations: (n: string) => `Relations ${n}`,
	chunks: 'Chunks',
	disk: 'Disk',
	rebuilt: (agoStr: string) => `Rebuilt ${agoStr}`,
	whatToRunNext: 'What to run next',
} as const;

const ko: Loose<typeof en> = {
	never: '기록 없음',
	justNow: '방금',
	mAgo: (n: number) => `${n}분 전`,
	hAgo: (n: number) => `${n}시간 전`,
	dAgo: (n: number) => `${n}일 전`,
	severityNow: '지금',
	severitySoon: '곧',
	severityLater: '여유',
	knowledgeDb: '지식 DB',
	lastChecked: (agoStr: string) => `${agoStr} 확인`,
	documents: '문서',
	entities: '엔티티',
	relations: (n: string) => `관계 ${n}`,
	chunks: '청크',
	disk: '디스크',
	rebuilt: (agoStr: string) => `재빌드 ${agoStr}`,
	whatToRunNext: '다음에 무엇을 돌릴까',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
