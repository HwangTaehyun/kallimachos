import type { Status } from '../api';
import { ago } from './StatusPanel';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/stalePanel';

/**
 * What has fallen behind —— and what to press about it.
 *
 * Reworked (2026-08-23)
 *   **Four rows used to sit there at the same size always**, and even a row with no problem carried
 *   "how is this computed ▸".  The screen was **explaining its own methodology** instead of showing
 *   the problem —— the flagship case of "it tells you how to look at it too much".
 *
 *   Now **only what has fallen behind is expanded.**  What is current folds into one line at the bottom.
 *   The method of computation is only asked about when there is a problem —— so it comes out only then.
 */
export function StalePanel({ status }: { status: Status }) {
	const t = useT(S);
	const { documents: d, kg, artifacts, db } = status;
	const vaultDrift = d.added.length + d.modified.length + d.deleted.length;

	const rows = [
		{
			key: 'vault',
			label: t('vaultLabel'),
			count: vaultDrift,
			unit: t('vaultUnit'),
			how: t('vaultHow'),
			detail: [
				...d.added.map((p) => t('vaultAdded')(p)),
				...d.modified.map((p) => t('vaultModified')(p)),
				...d.deleted.map((p) => t('vaultDeleted')(p)),
			],
			fix: t('vaultFix'),
			why: t('vaultWhy'),
		},
		{
			key: 'extract',
			label: t('extractLabel'),
			count: kg.extract_drift.length,
			unit: t('extractUnit'),
			how: t('extractHow')(ago(kg.extracted_at)),
			detail: kg.extract_drift,
			fix: t('refreshStaleKg'),
			why: t('extractWhy'),
		},
		{
			key: 'stale_docs',
			label: t('staleDocsLabel'),
			count: kg.stale.length,
			unit: t('staleDocsUnit')((kg.stale_ratio * 100).toFixed(1)),
			how: t('staleDocsHow')((kg.warn_ratio * 100).toFixed(0)),
			detail: kg.stale.map((s) => `${s.path} — ${s.reason}`),
			fix: t('refreshStaleKg'),
			why: t('staleDocsWhy')((kg.warn_ratio * 100).toFixed(0), (kg.stale_ratio * 100).toFixed(1)),
		},
		{
			key: 'artifacts',
			label: t('artifactsLabel'),
			count: artifacts.stale.length,
			unit: t('artifactsUnit'),
			how: t('artifactsHow')(ago(db.built_at)),
			detail: artifacts.stale.map((k) => `${k} — ${ago(artifacts.mtimes[k] ?? 0)}`),
			fix: t('artifactsFix'),
			why: t('artifactsWhy'),
		},
	];

	const dirty = rows.filter((r) => r.count > 0);
	const clean = rows.filter((r) => r.count === 0);

	return (
		<section aria-labelledby="stale-h">
			<header className="mb-3 flex items-baseline gap-3">
				<h2 id="stale-h" className="text-lg font-semibold text-ink-100">{t('title')}</h2>
				<span className={`text-xs ${dirty.length ? 'text-warn' : 'text-ok'}`}>
					{dirty.length === 0 ? t('allUpToDate') : t('behind')(dirty.length)}
				</span>
			</header>

			{dirty.length > 0 && (
				<ul className="flex flex-col gap-2.5">
					{dirty.map((r) => (
						<li key={r.key} className="rounded-card bg-ink-900/70 p-4 ring-1 ring-warn/25">
							<div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
								<h3 className="text-md font-semibold text-ink-100">{r.label}</h3>
								<span className="text-md font-semibold text-warn">
									{r.count.toLocaleString('ko-KR')}{r.unit}
								</span>
								<span className="text-xs text-ink-500">→ {r.fix}</span>
							</div>
							<p className="mt-1 text-xs leading-relaxed text-ink-400">{r.why}</p>

							{/*  The method of computation only becomes interesting **when there is a problem**.
							    Attached to a current row as well, the screen becomes a methodology manual. */}
							<details className="group mt-2">
								<summary className="cursor-pointer list-none text-2xs text-ink-500 hover:text-ink-200">
									{t('howMeasured')}
									<span className="ml-1 text-ink-700 group-open:hidden">▸</span>
									<span className="ml-1 hidden text-ink-700 group-open:inline">▾</span>
								</summary>
								<p className="mt-1.5 text-2xs leading-relaxed text-ink-500">{r.how}</p>
								{r.detail.length > 0 && (
									<ul className="mono mt-2 max-h-40 overflow-y-auto text-2xs text-ink-500">
										{r.detail.slice(0, 200).map((x) => (
											<li key={x} className="truncate py-0.5">{x}</li>
										))}
										{r.detail.length > 200 && (
											<li className="py-0.5">{t('andMore')(r.detail.length - 200)}</li>
										)}
									</ul>
								)}
							</details>
						</li>
					))}
				</ul>
			)}

			{clean.length > 0 && (
				//  What is current needs **one line**.  The name alone conveys "not missed, just fine",
				//  and the space goes to what has a problem.
				<p className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-500 ${
					dirty.length ? 'mt-3' : ''
				}`}>
					<span aria-hidden className="h-1.5 w-1.5 rounded-full bg-ok" />
					<span className="text-ink-400">{t('upToDate')}</span>
					{clean.map((r) => <span key={r.key}>{r.label}</span>)}
				</p>
			)}
		</section>
	);
}
