import { useState } from 'react';
import type { Estimate, Group, LlmStatus, Run, Status, Step } from '../api';
import { PipelineCanvas, type NodeShared } from './PipelineCanvas';
import { StepDetail } from './StepDetail';
import { ago } from './StatusPanel';
import { short } from '../estimate';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/stepList';

/**
 * The pipeline step list.
 *
 * Reworked (2026-08-23) —— "it tells you how to look at it too much; the UI/UX is not good"
 *
 * What was wrong
 *   ① **The explanation overwhelmed the operation.**  Every group carried an explanatory paragraph
 *      and every card four chips (`~35 min` `writes the DB` `LLM` `recommended`).  On an operating
 *      screen there was more to read than to do.
 *   ② **The hierarchy existed only in borders.**  Everything was the same `rounded-card border`,
 *      so nothing in the shape said what mattered.
 *   ③ **The coloured left rail** —— a vertical bar on every card.  The same information was in the
 *      chips, so it was duplication, and it is the signature pattern of "a screen made by an AI".
 *
 * What changed
 *   · The chips kept are **what cannot be undone** (writes the DB) and **what is blocked** (no LLM).
 *     The duration became meta beside the title rather than a chip —— it changes a judgement but is not a warning.
 *   · The read and write paths are developer information and moved to the card's `title`.  Not
 *     removed from the screen, but shown **only when wanted**.
 *   · Grouping is by background and spacing rather than borders.  A border is left only on **what
 *     matters right now** (running, recommended), so a border there means something.
 */

const RESULT = {
	running: { dot: 'bg-live animate-pulse', text: 'text-live' },
	ok: { dot: 'bg-ok', text: 'text-ok' },
	failed: { dot: 'bg-crit', text: 'text-crit' },
	cancelled: { dot: 'bg-ink-500', text: 'text-ink-500' },
} as const;

export function StepList({ steps, groups, est, status, runs, busy, llm, onStart, onSelect }: {
	steps: Step[];
	groups: Group[];
	/** The live estimate.  Without it each display falls back to the fixed value. */
	est: Record<string, Estimate>;
	status: Status | null;
	runs: Run[];
	busy: boolean;
	llm: LlmStatus | null;
	onStart: (id: string, args?: string[]) => void;
	onSelect: (runId: string) => void;
}) {
	const t = useT(S);
	const rec = new Map(status?.recommend.map((r) => [r.step, r]) ?? []);
	const byId = new Map(steps.map((s) => [s.id, s]));
	const lastOf = (id: string) =>
		runs.filter((r) => r.step === id).sort((x, y) => y.started_at - x.started_at)[0];

	//  Where a description is truncated (a 208px node, a 2-column card), give a way to see it whole.
	const [detail, setDetail] = useState<Step | null>(null);
	const shared: NodeShared = { rec, byId, est, lastOf, busy, llm, onStart, onSelect, onDetail: setDetail };

	//  Hovering a group card lights up **the steps that group runs, in the flow above**.
	//  What "refresh the stale KG" re-runs is also written in words (one line under the card),
	//  but which of the five steps it is has to be seen in the picture above.  Rather than copying
	//  the words onto the picture, the two are joined.
	const [hot, setHot] = useState<Set<string> | null>(null);
	const hoverRuns = (ids: string[] | undefined) =>
		setHot(ids && ids.length > 0 ? new Set(ids) : null);

	//  "Run everything" —— the rebuild_all combo goes at the head of the flow.  Running the six
	//  steps in order is the thing most often needed on this screen, and it was buried as one of
	//  the three group cards below.
	const all = steps.find((x) => x.id === 'rebuild_all');

	return (
		<div className="flex flex-col gap-10">
			{detail && (
				<StepDetail
					step={detail}
					groupTitle={groups.find((g) => g.id === detail.group)?.title}
					byId={byId}
					est={est}
					last={lastOf(detail.id)}
					busy={busy}
					llm={llm}
					onStart={onStart}
					onSelectRun={onSelect}
					onClose={() => setDetail(null)}
				/>
			)}
			{groups.map((g) => {
				const mine = steps
					.filter((s) => s.group === g.id)
					.sort((a, b) => a.order - b.order || a.title.localeCompare(b.title));
				if (mine.length === 0) return null;

				return (
					<section key={g.id} aria-labelledby={`grp-${g.id}`}>
						{/*  The group title alone.  The explanatory paragraph goes to `title` —— it is not text to read every time.
						    The running indicator stays: it is the only clue why a button is locked. */}
						<header className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-2">
							<h2
								id={`grp-${g.id}`}
								title={g.desc}
								className="text-lg font-semibold text-ink-100"
							>
								{g.title}
							</h2>
							{g.id === 'main' && busy && (
								<span className="flex items-center gap-1.5 text-2xs text-live">
									<span aria-hidden className="h-1.5 w-1.5 animate-pulse rounded-full bg-live" />
									{t('anotherRunning')}
								</span>
							)}
							{g.id === 'main' && all && (
								<button
									type="button"
									disabled={busy}
									title={busy ? t('busyTitle') : all.desc}
									onMouseEnter={() => hoverRuns(all.runs)}
									onMouseLeave={() => hoverRuns(undefined)}
									onFocus={() => hoverRuns(all.runs)}
									onBlur={() => hoverRuns(undefined)}
									onClick={() => onStart(all.id)}
									className="ml-auto shrink-0 rounded-control px-3 py-1.5 text-xs font-medium
									           text-ink-100 ring-1 ring-accent/60
									           transition-[color,background-color,box-shadow,scale] duration-150
									           enabled:hover:bg-accent/10 enabled:active:scale-[0.96]
									           disabled:cursor-not-allowed disabled:opacity-35"
								>
									{t('runAll')} <span className="font-normal text-ink-400">· {short(all, est[all.id])}</span>
								</button>
							)}
						</header>

						{g.id === 'main' ? (
							<PipelineCanvas steps={mine} highlight={hot} {...shared} />
						) : (
							<ul className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-3">
								{mine.map((s) => (
									<Card key={s.id} step={s} onHoverRuns={hoverRuns} {...shared} />
								))}
							</ul>
						)}
					</section>
				);
			})}
		</div>
	);
}

function Card({ step: s, est, rec, byId, lastOf, busy, llm, onStart, onSelect, onDetail, onHoverRuns }:
	{ step: Step; onHoverRuns: (ids: string[] | undefined) => void } & NodeShared) {
	const t = useT(S);
	const r = rec.get(s.id);
	const last = lastOf(s.id);
	const llmBlocked = s.needs_llm && llm !== null && !llm.ok;
	const live = last?.status === 'running';
	const res = last ? RESULT[last.status] : null;

	//  A path is developer information.  Always on screen it is noise, and removed it cannot be
	//  found —— so it moved to hover.  An empty value like `(none)` was fixed to an empty string in status.py.
	const io = [s.reads && t('reads')(s.reads), s.writes && t('writes')(s.writes)]
		.filter(Boolean).join('\n');

	return (
		<li
			//  It responds to **focus** as well as the mouse —— someone tabbing through has to see
			//  the same connection.
			onMouseEnter={() => onHoverRuns(s.runs)}
			onMouseLeave={() => onHoverRuns(undefined)}
			onFocus={() => onHoverRuns(s.runs)}
			onBlur={() => onHoverRuns(undefined)}
			//  `relative` is the reference for .gx-idle's ::before —— without it the ring positions
			//  itself against the page rather than the card.
			className={`relative flex flex-col rounded-card bg-ink-900/70 transition-[color,background-color,box-shadow] ${
				live ? 'ring-1 ring-live/50'
					: r ? 'ring-1 ring-accent/40'
					: 'gx-idle shadow-card'
			}`}
		>
			<button
				type="button"
				onClick={() => onDetail(s)}
				title={io || undefined}
				className="flex-1 rounded-t-card p-4 text-left
				           transition-[color,background-color,box-shadow] duration-150
				           hover:bg-ink-850/50 focus-visible:bg-ink-850/50 focus-visible:outline-none"
			>
				<div className="flex items-baseline gap-2">
					<h3 className="text-md font-semibold text-ink-100">{s.title}</h3>
					{/*  The duration is not a chip —— it is a fact, not a warning.  Made a chip it reads
					    with the same weight as "writes the DB" and buries the real warning. */}
					<span className="text-2xs text-ink-500">{short(s, est[s.id])}</span>
					{r && (
						<span className="ml-auto text-2xs font-medium text-accent">{t('recommended')}</span>
					)}
				</div>

				<p className="mt-1.5 text-xs leading-relaxed text-ink-400">{s.desc}</p>

				{(s.writes_db || llmBlocked) && (
					<div className="mt-2.5 flex flex-wrap items-center gap-1.5">
						{/*  The chips left are just two: **cannot be undone** and **cannot run now**. */}
						{s.writes_db && (
							<span className="rounded-control bg-warn/15 px-1.5 py-0.5 text-2xs font-medium text-warn">
								{t('overwritesDb')}
							</span>
						)}
						{llmBlocked && (
							<span className="rounded-control bg-crit/15 px-1.5 py-0.5 text-2xs font-medium text-crit">
								{t('claudeUnavailable')}
							</span>
						)}
					</div>
				)}

				{s.runs && s.runs.length > 0 && (
					//  What the group re-runs —— the point of this screen, so it stays.  But stripped of
					//  the chip, as one line of text.  With many chips, none of them get read.
					<p className="mt-2.5 text-2xs leading-relaxed text-ink-500">
						{s.runs.map((id) => byId.get(id)?.title ?? id).join('  →  ')}
					</p>
				)}
			</button>

			<footer className="flex items-center gap-2 border-t border-ink-850 px-4 py-2.5">
				{last ? (
					<button
						type="button"
						onClick={() => onSelect(last.id)}
						className="flex min-w-0 items-center gap-1.5 text-2xs text-ink-500
						           underline-offset-2 hover:text-ink-200 hover:underline"
					>
						<span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${res!.dot}`} />
						<span className="truncate">
							{t('lastRun')} <span className={res!.text}>{t(last.status)}</span> · {ago(last.started_at)}
							{/* It says where it was run —— only web-UI runs used to be recorded, so a CLI
							    success today still showed yesterday's failure as the last one. */}
							{last.origin === 'cli' && <span className="text-ink-700"> · CLI</span>}
						</span>
					</button>
				) : (
					<span className="text-2xs text-ink-700">{t('noRunsYet')}</span>
				)}

				<button
					type="button"
					disabled={busy || llmBlocked}
					// While something runs, buttons outside the main group are locked too, and the reason was
					// written only in the main group —— other sections were simply grey with no explanation.  (r4-ux)
					title={busy ? t('busyTitle')
						: llmBlocked ? (llm?.message ?? '') : undefined}
					onClick={() => onStart(s.id)}
					className="ml-auto shrink-0 rounded-control px-3 py-1 text-xs font-medium
					           text-ink-200 ring-1 ring-ink-700
					           transition-[color,background-color,box-shadow,scale] duration-150
					           enabled:hover:bg-ink-800 enabled:hover:text-ink-100
					           enabled:active:scale-[0.96]
					           disabled:cursor-not-allowed disabled:opacity-35"
				>
					{t('run')}
				</button>
			</footer>
		</li>
	);
}
