import type { Status } from '../api';
import { tr, useT } from '../i18n/lang';
import S from '../i18n/strings/statusPanel';

const fmt = new Intl.NumberFormat('ko-KR');

export function ago(ts: number): string {
	const t = tr(S);
	if (!ts) return t('never');
	const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
	if (s < 60) return t('justNow');
	if (s < 3600) return t('mAgo')(Math.floor(s / 60));
	if (s < 86400) return t('hAgo')(Math.floor(s / 3600));
	return t('dAgo')(Math.floor(s / 86400));
}

const SEV = {
	high: { chip: 'bg-crit/20 text-crit', ring: 'ring-crit/30', labelKey: 'severityNow' },
	medium: { chip: 'bg-warn/20 text-warn', ring: 'ring-warn/30', labelKey: 'severitySoon' },
	low: { chip: 'bg-ink-800 text-ink-500', ring: 'ring-ink-850', labelKey: 'severityLater' },
} as const;

/**
 * What state the DB is in now, and what to run next.
 *
 * Reworked (2026-08-23) —— the numbers were raised to be **the star of the screen**.
 *   The four numbers used to sit at `text-lg` among the other text, and every card border was the
 *   same, so there was no deciding what to look at first.  The reason for opening this screen is
 *   "how much has accumulated", and that did not catch the eye.
 *
 *   The borders came off and the numbers grew.  A border is used **only when it means something** ——
 *   today that is the recommendations' urgency alone.
 */
export function StatusPanel({ status }: { status: Status }) {
	const t = useT(S);
	// The detail of the staleness verdict is StalePanel's job —— showing the same thing in two places means neither is read.
	const { db, documents: d, kg } = status;

	return (
		<section aria-labelledby="status-h" className="flex flex-col gap-5">
			<header className="flex items-baseline gap-3">
				<h2 id="status-h" className="text-lg font-semibold text-ink-100">{t('knowledgeDb')}</h2>
				<span className="text-xs text-ink-500">{t('lastChecked')(ago(status.checked_at))}</span>
			</header>

			<dl className="grid grid-cols-2 gap-x-6 gap-y-5 sm:grid-cols-4">
				<Stat label={t('documents')} value={fmt.format(d.indexed)}
					sub={Object.entries(d.origin).map(([k, v]) => `${k} ${v}`).join(' · ')} />
				<Stat label={t('entities')} value={fmt.format(kg.entities)}
					sub={t('relations')(fmt.format(kg.relations))} />
				<Stat label={t('chunks')} value={fmt.format(db.tables.chunks ?? 0)}
					sub={db.embedding_model.split('/').pop() ?? ''} />
				<Stat label={t('disk')} value={`${Math.round(db.disk_bytes / 1e6)}MB`}
					sub={t('rebuilt')(ago(db.built_at))} />
			</dl>

			{status.recommend.length > 0 && (
				<div>
					<h3 className="mb-2 text-xs font-medium text-ink-500">{t('whatToRunNext')}</h3>
					<ul className="flex flex-col gap-1.5">
						{status.recommend.map((r, i) => (
							//  ⚠ The key used to be `r.step`.  **A piece of advice with no step has an empty
							//    string for step** (status.py collects those separately), so two or more collide.
							<li
								key={r.step || `loose-${i}`}
								className={`flex items-start gap-2.5 rounded-card bg-ink-900/60 px-3 py-2.5 ring-1 ${SEV[r.severity].ring}`}
							>
								{/*  The urgency as **visible text**.  As `sr-only` it left the dot's colour alone to
								    separate "now" from "when there is time" —— information carried by colour only. */}
								<span className={`mt-px shrink-0 rounded-control px-1.5 py-0.5 text-2xs font-semibold ${SEV[r.severity].chip}`}>
									{t(SEV[r.severity].labelKey)}
								</span>
								<span className="text-sm text-ink-200">{r.why}</span>
							</li>
						))}
					</ul>
				</div>
			)}
		</section>
	);
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
	return (
		<div>
			<dt className="text-2xs text-ink-500">{label}</dt>
			{/*  The numbers are this screen's answer.  24px/semibold, bigger than anything else. */}
			<dd className="mt-0.5 text-xl font-semibold text-ink-100">{value}</dd>
			{sub && <dd className="mt-0.5 text-2xs leading-relaxed text-ink-500">{sub}</dd>}
		</div>
	);
}
