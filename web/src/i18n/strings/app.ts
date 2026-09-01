import type { Dict, Loose } from '../lang';

const en = {
  toGraphTitle: 'To knowledge graph',
  settings: 'Settings',
  graphBreadcrumb: 'Knowledge graph',
  graph: 'Graph',
} as const;

const ko: Loose<typeof en> = {
  toGraphTitle: '지식그래프로',
  settings: '설정',
  graphBreadcrumb: '지식그래프',
  graph: '그래프',
};

export const S: Dict<typeof en> = { en, ko };
export default S;
