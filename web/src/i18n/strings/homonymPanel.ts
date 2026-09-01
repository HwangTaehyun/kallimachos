import type { Dict, Loose } from '../lang';

const en = {
	headerSubtitle: 'Splits when one name refers to different things',

	introPre: 'If ',
	introMid1: ' is both your Obsidian vault and your 1Password vault, they merge into one node and ',
	introBold1: 'unrelated neighbors get mixed together',
	introMid2: '. Writing a rule splits them apart. ',
	introBold2: 'It never splits automatically',
	introMid3: ' —— splitting wrong is just as bad as merging wrong. After editing, click ',
	introPost: ' for it to take effect.',

	rebuildGraph: 'Rebuild graph',

	errNoLoad: "Couldn't load the homonyms file —— locking the editor.",
	errNoLoadStrong: 'Saving an empty screen would delete the file',
	errNoLoadEnd: '.',
	retry: 'Retry',

	contentLabel: 'Homonyms file content',
	save: 'Save',
	saving: 'Saving…',
	savedFallback: 'Saved',

	unsavedTitle: 'You have unsaved changes. Rebuild reads the **saved file**, so save first.',
	unsavedChanges: 'Unsaved changes',

	searching: 'Searching',
	findCandidates: 'Find candidates',
	weakSignals: 'Include weak signals (document clusters · many false positives)',

	sugPre: '★ marks entries where the summarizing LLM also supplied ',
	sugMid: ", so they can be used as-is. For the rest, you'll need to ",
	sugBold: 'read the excerpt and fill it in yourself',
	sugPost: '.',

	appendToEditor: 'Append to editor above',
} as const;

const ko: Loose<typeof en> = {
	headerSubtitle: '한 이름이 서로 다른 것을 가리킬 때 가른다',

	introPre: '',
	introMid1: ' 가 Obsidian 저장소이자 1Password 보관함이면 한 노드로 합쳐져 ',
	introBold1: '무관한 이웃이 섞인다',
	introMid2: '. 규칙을 적으면 갈린다. ',
	introBold2: '자동으로 가르지 않는다',
	introMid3: ' —— 잘못 가른 건 잘못 합친 것만큼 나쁘다. 고친 뒤에는 ',
	introPost: ' 를 눌러야 반영된다.',

	rebuildGraph: '그래프 다시 만들기',

	errNoLoad: '동음이의어 파일을 불러오지 못했습니다 —— 편집기를 잠급니다.',
	errNoLoadStrong: '빈 화면을 저장하면 파일이 사라지기 때문',
	errNoLoadEnd: '입니다.',
	retry: '다시 시도',

	contentLabel: '동음이의어 파일 내용',
	save: '저장',
	saving: '저장 중…',
	savedFallback: '저장했습니다',

	unsavedTitle: '저장하지 않은 변경이 있습니다. 다시 만들기는 **저장된 파일**을 읽으므로, 먼저 저장하세요.',
	unsavedChanges: '저장 안 된 변경 있음',

	searching: '찾는 중',
	findCandidates: '후보 찾기',
	weakSignals: '약한 신호까지 (문서 군집 · 오답 많음)',

	sugPre: '★ 는 요약 LLM 이 ',
	sugMid: ' 까지 준 것이라 그대로 쓸 수 있다. 나머지는 ',
	sugBold: '조각을 읽고 직접 채워야',
	sugPost: ' 한다.',

	appendToEditor: '위 편집기에 덧붙이기',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
