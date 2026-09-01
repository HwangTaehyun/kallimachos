import type { Dict, Loose } from '../lang';

const en = {
	bundleNotFound: 'Galaxy bundle not found — run just build-galaxy',
	couldntLoad: (url: string) => `Couldn't load ${url} — did you run just build-galaxy?`,
	fetchingGraph: 'Fetching the knowledge graph… (~6MB, first load takes a moment)',
	computingLayout: 'Computing layout… (unfolding thousands of entities)',
	couldntDraw: "Couldn't draw the graph",
	runExport: 'Run export from Settings',
} as const;

const ko: Loose<typeof en> = {
	bundleNotFound: '갤럭시 번들이 없습니다 — just build-galaxy 를 돌리십시오',
	couldntLoad: (url: string) => `${url} 를 못 읽었습니다 — just build-galaxy 를 돌리셨습니까?`,
	fetchingGraph: '지식그래프를 받는 중… (6MB 대라 처음 한 번은 걸립니다)',
	computingLayout: '배치 계산 중… (엔티티 수천 개를 펼칩니다)',
	couldntDraw: '그래프를 못 그렸습니다',
	runExport: '설정에서 내보내기 돌리기',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
