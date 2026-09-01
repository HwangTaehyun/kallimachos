import type { Dict, Loose } from '../lang';

/**
 * `fix` 값은 문장 안에 명령이 섞인 평문이다 —— 백틱으로 감싼 부분을
 * ErrorBanner 의 `renderFix()` 가 `<Cmd>` 로 다시 감싼다. JSX 를 그대로
 * 사전에 넣을 수 없다 (`Strings` 타입은 문자열만 돌려주는 함수만 허용한다).
 */
const en = {
	diskFullWhat: 'The disk is full, so the container could not create temporary files.',
	diskFullFix: "It's often the Docker VM side that's full. Check with `docker system df`, and start with what you can safely undo — the build cache rebuilds itself, so it's the safest: `docker builder prune`. For the knowledge DB, `just vacuum` clears out old versions.",
	claudeUnavailableWhat: 'The claude CLI is unavailable.',
	claudeUnavailableFix: 'Start `just relay` on the host, or run that step directly on the host.',
	apiUnreachableWhat: 'Could not reach the api.',
	apiUnreachableFix: 'Check whether the api is running with `docker compose ps`.',
	viewRawOutput: 'View raw output',
	dismiss: 'Dismiss',
} as const;

const ko: Loose<typeof en> = {
	diskFullWhat: '디스크가 가득 차서 컨테이너가 임시 파일을 만들지 못했습니다.',
	diskFullFix: 'Docker VM 쪽이 찬 경우가 많습니다. `docker system df` 로 확인하고, 되돌릴 수 있는 것부터 —— 빌드 캐시는 다시 만들어지므로 가장 안전합니다: `docker builder prune`. 지식 DB 쪽이라면 `just vacuum` 이 옛 판을 걷습니다.',
	claudeUnavailableWhat: 'claude CLI 를 쓸 수 없습니다.',
	claudeUnavailableFix: '호스트에서 `just relay` 를 띄우거나, 그 단계를 호스트에서 직접 돌리십시오.',
	apiUnreachableWhat: 'api 에 닿지 못했습니다.',
	apiUnreachableFix: '`docker compose ps` 로 api 가 떠 있는지 확인하십시오.',
	viewRawOutput: '원문 보기',
	dismiss: '닫기',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
