import type { Dict, Loose } from '../lang';

const en = {
	humanConfirmedOnly: 'Human-confirmed only',
	loadFailedPrefix: "Couldn't load the alias file — the editor is locked, because",
	loadFailedStrong: 'saving a blank screen would erase the file',
	loadFailedSuffix: '.',
	retry: 'Retry',
	aliasContentLabel: 'Alias file content',
	saving: 'Saving…',
	save: 'Save',
	unsavedApplyHint: 'You have unsaved changes. Applying reads the saved file, so save first.',
	applyToGraph: 'Apply to graph (rebuild KG)',
	applyFastHint: 'Skips extraction and only rebuilds the index + graph JSON',
	applyAliasesOnly: 'Apply aliases only (fast)',
	unsavedChanges: 'Unsaved changes',
	applyAliasesOnlyLabel: 'Apply aliases only',
	applyAliasesOnlyDesc:
		' skips extraction (step 3) — aliases are applied by the merge in the indexing step, so that alone reflects in the graph. The tradeoff: descriptions for newly merged entities become stitched-together fragments instead of an LLM re-summary.',
	savedNotePrefix: 'Saved — ',
	aliasHintPrefix: 'Case, hyphen, and whitespace differences are already absorbed automatically. Add entries here only for',
	aliasHintStrong: ' different writing systems (Hangul ↔ Roman) or abbreviations',
	aliasHintSuffix: '.',
	aliasCandidates: 'Alias candidates',
	findingCandidates: 'Finding candidates',
	pairsCount: (n: number) => `${n} pairs · not merged automatically`,
	includeSubstringRules: 'Include substring rules (more false positives)',
	failedLoadCandidates: 'Failed to load candidates: ',
	add: 'Add',
	noCandidates: 'No candidates.',
} as const;

const ko: Loose<typeof en> = {
	humanConfirmedOnly: '사람이 확정한 것만',
	loadFailedPrefix: '별칭 파일을 불러오지 못했습니다 —— 편집기를 잠급니다.',
	loadFailedStrong: '빈 화면을 저장하면 파일이 사라지기 때문',
	loadFailedSuffix: '입니다.',
	retry: '다시 시도',
	aliasContentLabel: '별칭 파일 내용',
	saving: '저장 중…',
	save: '저장',
	unsavedApplyHint: '저장하지 않은 변경이 있습니다. 반영은 저장된 파일을 읽으므로 먼저 저장하세요.',
	applyToGraph: '그래프에 반영 (KG 재빌드)',
	applyFastHint: '추출을 건너뛰고 색인 재빌드 + 그래프 JSON 만 다시 만듭니다',
	applyAliasesOnly: '별칭만 반영 (빠름)',
	unsavedChanges: '저장 안 된 변경 있음',
	applyAliasesOnlyLabel: '별칭만 반영',
	applyAliasesOnlyDesc:
		'은 추출(3단계)을 건너뜁니다 —— 별칭은 색인 단계의 병합이 적용하므로 그것만으로 그래프에 반영됩니다. 대신 새로 합쳐진 엔티티의 설명이 LLM 재요약 없이 조각을 이어붙인 것이 됩니다.',
	savedNotePrefix: '저장했습니다 — ',
	aliasHintPrefix: '대소문자·하이픈·공백 차이는 이미 자동으로 흡수됩니다. 여기 적을 것은',
	aliasHintStrong: ' 표기 체계가 다르거나(한글↔로마자) 약어인 경우',
	aliasHintSuffix: '입니다.',
	aliasCandidates: '별칭 후보',
	findingCandidates: '후보를 찾는 중',
	pairsCount: (n: number) => `${n}쌍 · 자동으로 합치지 않습니다`,
	includeSubstringRules: '포함 규칙까지 (오답 많음)',
	failedLoadCandidates: '후보를 불러오지 못했습니다: ',
	add: '추가',
	noCandidates: '후보가 없습니다.',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
