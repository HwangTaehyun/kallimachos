import type { Dict, Loose } from '../lang';

const en = {
	title: 'Stale status',
	allUpToDate: 'All up to date',
	behind: (n: number) => `${n} behind`,
	howMeasured: 'How this is measured',
	andMore: (n: number) => `… and ${n} more`,
	upToDate: 'Up to date',

	vaultLabel: 'vault ↔ index',
	vaultUnit: ' items',
	vaultHow: "Reads every .md in vault, computes the first 16 characters of its sha256, and compares against "
		+ "documents.content_hash. It judges by content, not mtime —— it is not fooled by a file that was merely "
		+ "touched, and it does not miss one whose editor never updates mtime.",
	vaultAdded: (p: string) => `Added ${p}`,
	vaultModified: (p: string) => `Modified ${p}`,
	vaultDeleted: (p: string) => `Deleted ${p}`,
	vaultFix: 'Incremental sync',
	vaultWhy: 'Searching now turns up nothing, or shows stale content',

	extractLabel: 'extraction ↔ current vault',
	extractUnit: ' items',
	extractHow: (lastExtracted: string) => `lr_extract records the vault hash it saw at extraction time in the `
		+ `doc_hashes field of lr_kg.json. It compares that against the current vault. Last extraction was ${lastExtracted}.`,
	extractWhy: 'Chunks reflect the new content, but entities and relationships are still old',

	refreshStaleKg: 'Refresh stale KG',

	staleDocsLabel: 'KG staleness flags',
	staleDocsUnit: (pct: string) => ` items (${pct}%)`,
	staleDocsHow: (warnPct: string) => `Incremental sync fixes the chunks and vectors, but not the KG (that needs `
		+ `an LLM). Instead it just marks the stale_docs table. Past ${warnPct}%, a refresh is recommended.`,
	staleDocsWhy: (warnPct: string, nowPct: string) => `Threshold ${warnPct}% · now ${nowPct}%`,

	artifactsLabel: 'artifacts ↔ DB',
	artifactsUnit: ' items',
	artifactsHow: (rebuiltAgo: string) => `Compares the artifact file's mtime against meta.built_at (rebuilt `
		+ `${rebuiltAgo}). An artifact older than the DB is showing an outdated graph.`,
	artifactsFix: 'Export graph',
	artifactsWhy: 'The graph view is showing old entities',
} as const;

const ko: Loose<typeof en> = {
	title: '낡음 현황',
	allUpToDate: '전부 최신',
	behind: (n: number) => `${n}가지가 뒤처짐`,
	howMeasured: '어떻게 재나',
	andMore: (n: number) => `… 외 ${n}건`,
	upToDate: '최신',

	vaultLabel: 'vault ↔ 색인',
	vaultUnit: '건',
	vaultHow: 'vault 의 .md 를 전부 읽어 sha256 앞 16자를 내고, documents.content_hash 와 비교한다. '
		+ 'mtime 이 아니라 내용으로 판정한다 —— touch 만 한 파일에 속지 않고, '
		+ '편집기가 mtime 을 안 바꿔도 놓치지 않는다.',
	vaultAdded: (p: string) => `추가 ${p}`,
	vaultModified: (p: string) => `수정 ${p}`,
	vaultDeleted: (p: string) => `삭제 ${p}`,
	vaultFix: '증분 동기화',
	vaultWhy: '지금 검색하면 안 나오거나 옛 내용이 나온다',

	extractLabel: '추출 ↔ 지금 vault',
	extractUnit: '건',
	extractHow: (lastExtracted: string) => `lr_extract 가 추출할 때 본 vault 해시를 lr_kg.json 의 doc_hashes 에 남긴다. `
		+ `그걸 지금 vault 와 비교한다. 마지막 추출은 ${lastExtracted}.`,
	extractWhy: '청크는 새 내용인데 엔티티·관계는 옛 내용이다',

	refreshStaleKg: '낡은 KG 갱신',

	staleDocsLabel: 'KG 낡음 표시',
	staleDocsUnit: (pct: string) => `건 (${pct}%)`,
	staleDocsHow: (warnPct: string) => `증분 동기화는 청크·벡터는 고치지만 KG 는 못 고친다 (LLM 이 필요하므로). `
		+ `대신 stale_docs 테이블에 표시만 남긴다. ${warnPct}% 를 넘으면 갱신을 권한다.`,
	staleDocsWhy: (warnPct: string, nowPct: string) => `임계 ${warnPct}% · 지금 ${nowPct}%`,

	artifactsLabel: '산출물 ↔ DB',
	artifactsUnit: '개',
	artifactsHow: (rebuiltAgo: string) => `산출물 파일의 mtime 을 meta.built_at (${rebuiltAgo} 재빌드) 과 비교한다. `
		+ 'DB 보다 오래됐으면 옛 그래프를 보여주고 있다는 뜻이다.',
	artifactsFix: '그래프 내보내기',
	artifactsWhy: '그래프 뷰가 옛 엔티티를 보여준다',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
