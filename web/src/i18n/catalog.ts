import type { Group, Step } from '../api';
import type { Lang } from './lang';

/**
 * The pipeline catalogue's (/api/steps) **Korean display text**.
 *
 * The catalogue's single source is `src/status.py` and the text there is **English** (2026-09-01:
 * English being the default, the CLI and `just status` have to be English too, so the py holds
 * English).  This holds only the Korean overrides for those ids —— en passes the API's source text
 * through unchanged.  So the text never exists in two copies: English once in Python, Korean once here.
 *
 * ⚠ An unknown id falls back to the API's source text (English).  Add a new step to Python and the Korean screen
 *   영어로 나오므로 여기도 같이 채운다 —— tests/catalog.test.ts 가 id 집합을 본다.
 *
 * `index` 의 설명에 든 청크 크기는 **설정을 따라간다** (py 가 `{chunk_chars}` 를 채운다).
 * 그래서 그 항목만 API 문구에서 숫자를 받아 쓴다 —— 숫자를 박아 두면 청크 크기를 바꿨을 때
 * 한국어 설명만 옛 값을 말한다.
 */
type Text = { title: string; desc: string | ((apiDesc: string) => string) };

export const GROUPS_KO: Record<string, Text> = {
	main: { title: '전체 흐름', desc: 'vault 의 글이 검색 가능한 지식그래프가 되기까지. 순서가 있다.' },
	combo: { title: '묶음 실행', desc: '위 단계들을 순서대로 다시 돌린다.' },
	partial: { title: '부분 갱신', desc: '일부만 손본다. 빠른 대신 남는 것이 있다.' },
	check: { title: '검사', desc: '읽기만 한다. 아무것도 바꾸지 않는다.' },
};

const firstNumber = (s: string, fallback: string): string => s.match(/\d[\d,]*/)?.[0] ?? fallback;

export const STEPS_KO: Record<string, Text> = {
	distill: { title: '세션 정제', desc: '~/.claude 대화 로그 → brain-ingest 규격 문서. 크레덴셜 마스킹 포함.' },
	promote: { title: 'vault 편입', desc: '정제본을 vault 로 옮기고 커밋한다.' },
	extract: { title: '지식그래프 추출', desc: '2,400자 청크마다 LLM 으로 엔티티·관계를 뽑는다. 청크 해시가 같으면 안 부른다.' },
	index: {
		title: '지식 DB 재구축',
		desc: (api) => `vault 를 ${firstNumber(api, '500')}자로 잘라 임베딩하고, lr_kg.json 을 읽어 엔티티를 병합·배치한다. 끝에 옛 LanceDB 버전을 청소한다.`,
	},
	export: { title: '그래프 내보내기', desc: 'Louvain 군집 + LLM 라벨을 붙여 뷰어 산출물을 만든다. 플러그인 번들은 호스트에서만 다시 만들어진다 (컨테이너에 plugin/ 이 없다).' },
	refresh_kg: { title: '낡은 KG 갱신', desc: '낡은 문서의 KG 를 되살린다. 추출 캐시가 살아 있어 바뀐 청크만 LLM 을 부른다.' },
	apply_aliases: { title: '별칭만 반영 (빠름)', desc: 'aliases.yml 만 그래프에 반영한다. 추출을 건너뛰어 빠른 대신, 새로 합쳐진 엔티티의 설명이 LLM 재요약 없이 조각을 이어붙인 것이 된다.' },
	rebuild_all: { title: '전체 재빌드', desc: '정제부터 내보내기까지 전부. 가중치 튜닝·평가까지 돈다.' },
	sync: { title: '증분 동기화', desc: '바뀐 문서만 청크·벡터·역색인을 갱신한다. **KG 는 못 고친다** —— LLM 이 필요하므로 stale_docs 에 표시만 남기고 넘어간다.' },
	verify: { title: '문서 수치·링크 검증', desc: '문서에 적힌 수치가 DB 와 맞는지, 상대 링크가 살아 있는지. 아무것도 바꾸지 않는다.' },
};


const apply = <T extends { id: string; title: string; desc: string }>(it: T, dict: Record<string, Text>): T => {
	const d = dict[it.id];
	if (!d) return it;
	return { ...it, title: d.title, desc: typeof d.desc === 'function' ? d.desc(it.desc) : d.desc };
};
const pick = <T extends { id: string; title: string; desc: string }>(items: T[], ko: Record<string, Text>, lang: Lang): T[] =>
	lang === 'ko' ? items.map((it) => apply(it, ko)) : items;

export function localizeCatalog<S extends Step, G extends Group>(r: { steps: S[]; groups: G[] }, lang: Lang): { steps: S[]; groups: G[] } {
	return { steps: pick(r.steps, STEPS_KO, lang), groups: pick(r.groups, GROUPS_KO, lang) };
}
