import type { Dict } from '../lang';

const en = {
  reload: 'Reload',
  loadingStatus: 'Loading status',
  runTitle: (title: string) => `Run ${title}`,
  runCta: 'Run',
  lockedReloadTitle: 'Locked here to protect content you are editing',
  dbWarning: ' · Overwrites the knowledge DB. This cannot be undone.',
  unknownStepBody:
    'Information for this step is not available yet (the list is still loading, or failed to load). ' +
    'It is unknown how long this takes, or whether it overwrites the knowledge DB.',
};

const ko: Partial<typeof en> = {
  reload: '새로 읽기',
  loadingStatus: '상태를 읽는 중',
  runTitle: (title: string) => `${title} 실행`,
  runCta: '실행',
  lockedReloadTitle: '편집 중인 내용을 지키려고 여기서는 잠급니다',
  dbWarning: ' · 지식 DB 를 덮어씁니다. 되돌릴 수 없습니다.',
  unknownStepBody:
    '이 단계의 정보를 아직 못 읽었습니다 (목록을 불러오는 중이거나 실패). ' +
    '얼마나 걸리는지, 지식 DB 를 덮어쓰는지 알 수 없습니다.',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
