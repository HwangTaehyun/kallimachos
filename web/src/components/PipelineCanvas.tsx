import { useEffect, useRef, useState } from 'react';
import type { Estimate, LlmStatus, Run, Step } from '../api';
import { short } from '../estimate';
import { ago } from './StatusPanel';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/pipelineCanvas';

/**
 * Draws the main pipeline as **a node graph** (the n8n family).
 *
 * Why a canvas rather than a list
 *   The order of these 5 steps carries meaning —— distil → promote → extract → index → export.
 *   Drawn as a flat list, that order survives only in the single digit of `order`, and the screen
 *   becomes "five similar-looking grey rows".
 *
 * Reworked (2026-08-23) —— **the coloured left rail and the pile of chips** were removed from the cards.
 *   What the rail's colour carried (LLM, writes the DB) was in the chips too, so it was duplication,
 *   and with three chips per node, "can I press this right now" was invisible.  What is left is a
 *   single marker for what cannot be undone.
 */

const RESULT = {
	ok: { dot: 'bg-ok', text: 'text-ok' },
	failed: { dot: 'bg-crit', text: 'text-crit' },
	running: { dot: 'bg-live animate-pulse', text: 'text-live' },
	cancelled: { dot: 'bg-ink-500', text: 'text-ink-500' },
} as const;

export type NodeShared = {
	rec: Map<string, { why: string; severity: 'high' | 'medium' | 'low' }>;
	byId: Map<string, Step>;
	/** The live estimate.  When empty, each display falls back to `Step.minutes`'s fixed value. */
	est: Record<string, Estimate>;
	lastOf: (id: string) => Run | undefined;
	busy: boolean;
	llm: LlmStatus | null;
	onStart: (id: string, args?: string[]) => void;
	onSelect: (runId: string) => void;
	/** Opens the step's full text.  A node is 208px wide, so a description is cut at two lines. */
	onDetail: (s: Step) => void;
};

export function PipelineCanvas({ steps, highlight, ...shared }:
	{ steps: Step[]; /** Light up only these steps.  null means all of them normal. */ highlight?: Set<string> | null }
	& NodeShared) {
	const t = useT(S);
	const box = useRef<HTMLDivElement>(null);
	//  **Is there more to the right.**  A cut-off node looks like a node that does not exist ——
	//  the 5th node really was cut off, taking its run button with it (checked on screen 2026-08-21).
	const [more, setMore] = useState(false);
	useEffect(() => {
		const el = box.current;
		if (!el) return;
		const upd = () => setMore(el.scrollWidth - el.scrollLeft - el.clientWidth > 8);
		upd();
		el.addEventListener('scroll', upd, { passive: true });
		//  Some environments have no ResizeObserver (jsdom).  Without it, a window resize stands in.
		const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(upd) : null;
		if (ro) ro.observe(el);
		else window.addEventListener('resize', upd);
		return () => {
			el.removeEventListener('scroll', upd);
			if (ro) ro.disconnect();
			else window.removeEventListener('resize', upd);
		};
	}, [steps.length]);

	return (
		<div className="relative">
			<div ref={box} className="canvas-grid overflow-x-auto rounded-card px-4 py-5 ring-1 ring-ink-850">
				<div className="flex min-w-max items-stretch">
						{/*  A dot marks the flow's start and end (the same notation as Kestra's topology).
						    Without them the first node floats with no "where it comes from", and there is
						    no telling whether what follows the last node is cut off or finished. */}
						<Cap side="start" />
						<ol className="flex items-stretch" aria-label={t('mainFlow')}>
						{steps.map((s, i) => (
							<li key={s.id} className="flex items-stretch">
								{/*  The only flowing line is the one **entering the step currently running**.
								    The same reading as n8n: the arrow points at where execution is.
								    Flowing them all makes "the data is moving" a lie. */}
								{i > 0 && (
									<Connector
										flowing={shared.lastOf(s.id)?.status === 'running'}
										//  A line lights up **only when both ends are lit**.  Lighting one with only
										//  one end lit leaks the highlight outside the group.
										lit={!!highlight && highlight.has(s.id) && highlight.has(steps[i - 1]!.id)}
										dim={!!highlight}
									/>
								)}
								<Node step={s} {...shared} lit={highlight ? highlight.has(s.id) : null} />
							</li>
						))}
				</ol>
						<Cap side="end" />
					</div>
			</div>
			{more && (
				<div
					aria-hidden
					className="pointer-events-none absolute inset-y-px right-px w-20 rounded-r-card
					           bg-gradient-to-l from-ink-950 via-ink-950/70 to-transparent"
				/>
			)}
		</div>
	);
}

/**
 * The connector between nodes —— the same notation as Kestra's topology.
 *
 * It used to be a 1px solid line + an open chevron arrowhead, with port dots on the nodes' sides
 * as well, looking like `●———›●`.  The two dots cut off the line's ends, so **the line read as not
 * touching the nodes** (a user's point, 2026-08-23).
 *
 * Now it is a flowing dashed line + a filled triangle:
 *   · A dash means "something passing through" —— solid is structure, dashed is flow.
 *   · A filled triangle reads far better at a small size than an open chevron.
 *   · The viewBox matches the container's pixels 1:1 (48×12 → w-12 h-3) so the line does not blur.
 */
function Connector({ flowing = false, lit = false, dim = false }:
	{ flowing?: boolean; lit?: boolean; dim?: boolean }) {
	return (
		<div
			aria-hidden
			className={`flex w-12 shrink-0 items-center self-center
			            transition-[color,opacity] duration-150 ${
				flowing ? 'text-live' : lit ? 'text-accent' : 'text-ink-500'
			} ${dim && !lit ? 'opacity-25' : ''}`}
		>
			<svg viewBox="0 0 48 12" className="h-3 w-12" fill="none">
				<path
					d="M2 6H36"
					stroke="currentColor"
					strokeWidth="1.5"
					strokeDasharray="3 5"
					className={flowing ? 'gx-flow-fast' : 'gx-flow'}
				/>
				<path d="M36 2.2 45 6 36 9.8Z" fill="currentColor" />
			</svg>
		</div>
	);
}

/** The flow's start and end marker.  One dot says "it starts here" and "it ends here". */
function Cap({ side }: { side: 'start' | 'end' }) {
	return (
		<div aria-hidden className="flex w-9 shrink-0 items-center self-center text-ink-500">
			<svg viewBox="0 0 36 12" className="h-3 w-9" fill="none">
				{side === 'start' ? (
					<>
						<circle cx="3.5" cy="6" r="3" fill="currentColor" />
						<path d="M8.5 6H24" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 5" className="gx-flow" />
						<path d="M24 2.2 33 6 24 9.8Z" fill="currentColor" />
					</>
				) : (
					<>
						<path d="M3 6H27" stroke="currentColor" strokeWidth="1.5" strokeDasharray="3 5" className="gx-flow" />
						<circle cx="32.5" cy="6" r="3" fill="currentColor" />
					</>
				)}
			</svg>
		</div>
	);
}

function Node({ step: s, rec, est, lastOf, busy, llm, onStart, onSelect, onDetail, lit }:
	{ step: Step; /** true = lit · false = dimmed · null = normal */ lit?: boolean | null } & NodeShared) {
	const t = useT(S);
	const r = rec.get(s.id);
	const last = lastOf(s.id);
	const llmBlocked = s.needs_llm && llm !== null && !llm.ok;
	const live = last?.status === 'running';
	const res = last ? RESULT[last.status] : null;
	const io = [s.reads && t('reads')(s.reads), s.writes && t('writes')(s.writes)]
		.filter(Boolean).join('\n');

	return (
		<article
			title={io || undefined}
			//  While running, **the border rotates** (.gx-running in index.css).  A single static ring
			//  made it impossible to see at a glance which of five nodes was running.
			className={`relative flex w-52 shrink-0 flex-col rounded-card bg-ink-900
			            transition-[color,background-color,box-shadow,opacity] duration-150 ${
				live ? 'gx-running shadow-raised'
					: lit ? 'ring-1 ring-accent shadow-raised'
					: r ? 'ring-1 ring-accent/50'
					//  Even normally, the border rotates very slowly (.gx-idle in index.css).
					//  It is a material cue rather than a state, so it sits far below live/accent.
					: 'gx-idle ring-1 ring-ink-800 shadow-raised'
			} ${lit === false ? 'opacity-30' : ''}`}
		>
			{/*  The whole body is a button —— pressing a truncated description gives the full text.
			     A `title` tooltip alone was not enough: it takes a second to appear and the keyboard cannot reach it. */}
			<button
				type="button"
				onClick={() => onDetail(s)}
				className="flex-1 rounded-t-card p-3.5 text-left
				           transition-[color,background-color,box-shadow] duration-150
				           hover:bg-ink-850/60 focus-visible:bg-ink-850/60 focus-visible:outline-none"
			>
				<div className="flex items-baseline gap-2">
					<span className="text-2xs text-ink-700">{s.order}</span>
					<h3 className="truncate text-md font-semibold text-ink-100">{s.title}</h3>
					{r && <span className="ml-auto text-2xs font-medium text-accent">{t('recommended')}</span>}
				</div>
				<p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-ink-400">{s.desc}</p>

				<div className="mt-2.5 flex items-center gap-2 text-2xs text-ink-500">
					<span>{short(s, est[s.id])}</span>
					{s.writes_db && <span className="text-warn">· {t('overwritesDb')}</span>}
					{llmBlocked && <span className="text-crit">· {t('claudeUnavailable')}</span>}
				</div>
			</button>

			<footer className="flex items-center gap-2 border-t border-ink-850 px-3.5 py-2.5">
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
							{last.origin === 'cli' && <span className="text-ink-700"> · CLI</span>}
						</span>
					</button>
				) : (
					<span className="text-2xs text-ink-700">{t('noRunsYet')}</span>
				)}

				<button
					type="button"
					disabled={busy || llmBlocked}
					title={busy ? t('busyTitle')
						: llmBlocked ? (llm?.message ?? '') : undefined}
					onClick={() => onStart(s.id)}
					className="ml-auto shrink-0 rounded-control px-2.5 py-1 text-xs font-medium
					           text-ink-200 ring-1 ring-ink-700
					           transition-[color,background-color,box-shadow,scale] duration-150
					           enabled:hover:bg-ink-800 enabled:hover:text-ink-100
					           enabled:active:scale-[0.96]
					           disabled:cursor-not-allowed disabled:opacity-35"
				>
					{t('run')}
				</button>
			</footer>
		</article>
	);
}
