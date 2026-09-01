/**
 * Estimated time in human words.
 *
 * Why its own file —— **because the fallback rule lives here**.  With no estimate from the server
 * the screen has to fall back to `Step.minutes`'s fixed value, and scattering that branch across
 * four components guarantees one is missed.  The missed one shows up not as an error but as **a
 * wrong number**, so nobody notices.
 */
import type { Estimate, Step } from './api';
import { tr } from './i18n/lang';
import S from './i18n/strings/estimate';

/** Seconds → "instant" · "42s" · "3 min" · "1.5 hours" */
export function human(sec: number): string {
	const t = tr(S);
	if (sec < 1) return t('instant');
	if (sec < 60) return t('seconds')(Math.round(sec));
	if (sec < 3600) return t('minutes')(Math.round(sec / 60));
	return t('hours')((sec / 3600).toFixed(1));
}

/** The short label attached to the list and the canvas.  With no estimate it falls back to the fixed value. */
export function short(step: Step, e?: Estimate): string {
	return e ? human(e.seconds) : tr(S)('minutes')(step.minutes);
}

/**
 * The one line for a confirmation dialog's body.
 *
 * It says **the count before the time**.  "About 30 minutes" gives no clue where it went wrong,
 * while "3 LLM calls" is something the user can judge.
 */
export function sentence(step: Step, e?: Estimate): string {
	const t = tr(S);
	if (!e) return t('sentenceFallback')(step.minutes);
	const head = e.units !== null ? t('workUnits')(e.units.toLocaleString(), e.unit) : '';
	return `${head}${t('estimated')(human(e.seconds), e.basis)}`;
}

/** Is it heavy enough to warrant a confirmation.  It asks when it overwrites the DB or takes over a minute. */
export function heavy(step: Step, e?: Estimate): boolean {
	if (step.writes_db) return true;
	//  ⚠ **Touching the user's vault always asks.**  It used to look only at `writes_db` and the
	//     duration, and `promote` is neither while it deletes and rewrites documents inside the
	//     vault —— the one step that touches the vault was the one step running without a
	//     confirmation (2026-08-28 deep review).  Undoing it means `git revert`, and that cannot
	//     recover an untracked file.
	if (step.writes_vault) return true;
	return e ? e.seconds >= 60 : step.minutes >= 5;
}
