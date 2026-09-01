import { useEffect, useRef } from 'react';
import type { Estimate, LlmStatus, Run, Step } from '../api';
import { short } from '../estimate';
import { ago } from './StatusPanel';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/stepDetail';

/**
 * **The full text** of one step.
 *
 * Why it is needed —— a canvas node is 208px wide and its description is cut by `line-clamp-2`.
 * In "an LLM extracts entities and relations from every 2,400-character chunk.  If the chunk hash
 * matches …", the part cut off is **"it is not called"**, and that decides whether running this
 * step twice is safe.  Widening the node stops five fitting on screen, and leaving it cut loses
 * the most important thing said.  So it is folded by default and **pressing shows all of it**.
 *
 * It uses `<dialog>` + `showModal()` —— the focus trap, Esc to close and the inert background come
 * from the browser (the same reason as Confirm.tsx).
 */
export function StepDetail({ step: s, groupTitle, byId, est, last, busy, llm, onStart, onSelectRun, onClose }: {
	step: Step;
	groupTitle?: string;
	byId: Map<string, Step>;
	/** The live estimate.  Without it, it falls back to the fixed value. */
	est: Record<string, Estimate>;
	last?: Run;
	busy: boolean;
	llm: LlmStatus | null;
	onStart: (id: string) => void;
	onSelectRun: (runId: string) => void;
	onClose: () => void;
}) {
	const ref = useRef<HTMLDialogElement>(null);
	const closeRef = useRef<HTMLButtonElement>(null);
	const llmBlocked = s.needs_llm && llm !== null && !llm.ok;
	const t = useT(S);

	useEffect(() => {
		ref.current?.showModal();
		//  Focus goes to **close**, not to run.  Pressing Enter the moment it opens must not just
		//  start a 30-minute job (the same judgement as Confirm.tsx).
		closeRef.current?.focus();
	}, []);

	const close = () => ref.current?.close();

	return (
		<dialog
			ref={ref}
			aria-labelledby="stepdetail-title"
			onClose={onClose}
			onCancel={onClose}
			className="m-auto w-[min(38rem,calc(100vw-2rem))] rounded-card border border-ink-700
			           bg-ink-900 p-0 text-ink-100 backdrop:bg-black/60 backdrop:backdrop-blur-sm"
		>
			<header className="flex items-start gap-3 border-b border-ink-850 px-5 py-4">
				<div className="min-w-0 flex-1">
					<div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
						{s.order > 0 && <span className="text-2xs text-ink-600">{s.order}</span>}
						<h2 id="stepdetail-title" className="text-md font-semibold">{s.title}</h2>
						{groupTitle && <span className="text-2xs text-ink-500">{groupTitle}</span>}
					</div>
					<p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-ink-400">
						<span>{short(s, est[s.id])}</span>
						{s.writes_db && <span className="text-warn">{t('overwritesDb')}</span>}
						{s.needs_llm && (
							<span className={llmBlocked ? 'text-crit' : ''}>
								{t('requiresClaude')}{llmBlocked ? t('unavailableNow') : ''}
							</span>
						)}
					</p>
				</div>
				<button
					ref={closeRef}
					type="button"
					onClick={close}
					aria-label={t('close')}
					className="-mr-1 -mt-1 shrink-0 rounded-control px-2 py-1 text-sm text-ink-500
					           transition-[color,background-color,box-shadow,scale] duration-150
					           hover:bg-ink-850 hover:text-ink-100 active:scale-[0.96]"
				>
					✕
				</button>
			</header>

			<div className="flex flex-col gap-4 px-5 py-4">
				{/*  ★ The full, untruncated text.  This is why the dialog exists. */}
				<p className="whitespace-pre-line text-sm leading-relaxed text-ink-200">{s.desc}</p>

				{s.runs && s.runs.length > 0 && (
					<Field label={t('runs')}>
						<span className="leading-relaxed">
							{s.runs.map((id) => byId.get(id)?.title ?? id).join('  →  ')}
						</span>
					</Field>
				)}

				{/*  Read and write used to live only in the node's `title` tooltip —— which needs a
				    second of hovering and the keyboard cannot reach at all. */}
				{s.reads && <Field label={t('reads')}><Mono>{s.reads}</Mono></Field>}
				{s.writes && <Field label={t('writes')}><Mono>{s.writes}</Mono></Field>}
				<Field label={t('command')}><Mono>{s.cmd.join(' ')}</Mono></Field>

				<Field label={t('lastRun')}>
					{last ? (
						<button
							type="button"
							onClick={() => { onSelectRun(last.id); close(); }}
							className="underline-offset-2 hover:text-ink-100 hover:underline"
						>
							{t(RESULT_KEY[last.status])} · {ago(last.started_at)}
							{last.origin === 'cli' && ' · CLI'}
							<span className="ml-1.5 text-2xs text-ink-600">{t('viewLog')}</span>
						</button>
					) : (
						<span className="text-ink-600">{t('neverRun')}</span>
					)}
				</Field>
			</div>

			<footer className="flex items-center justify-end gap-2 border-t border-ink-850 px-5 py-3.5">
				<button
					type="button"
					onClick={close}
					className="rounded-control px-3 py-1.5 text-xs text-ink-400
					           transition-[color,background-color,box-shadow,scale] duration-150
					           hover:bg-ink-850 hover:text-ink-100 active:scale-[0.96]"
				>
					{t('close')}
				</button>
				<button
					type="button"
					disabled={busy || llmBlocked}
					title={busy ? t('anotherStepRunning')
						: llmBlocked ? (llm?.message ?? '') : undefined}
					//  ⚠ It closes first.  `onStart` opens a confirmation, and opening one with this
					//    dialog still open stacks two modals with no telling which one the second confirms.
					onClick={() => { close(); onStart(s.id); }}
					className="rounded-control px-3 py-1.5 text-xs font-medium text-ink-100 ring-1 ring-accent/60
					           transition-[color,background-color,box-shadow,scale] duration-150
					           enabled:hover:bg-accent/10 enabled:active:scale-[0.96]
					           disabled:cursor-not-allowed disabled:opacity-35"
				>
					{t('run')}
				</button>
			</footer>
		</dialog>
	);
}

const RESULT_KEY: Record<Run['status'], 'resultRunning' | 'resultOk' | 'resultFailed' | 'resultCancelled'> = {
	running: 'resultRunning', ok: 'resultOk', failed: 'resultFailed', cancelled: 'resultCancelled',
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
	return (
		<div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
			<span className="w-20 shrink-0 text-ink-500">{label}</span>
			<span className="min-w-0 flex-1 text-ink-300">{children}</span>
		</div>
	);
}

function Mono({ children }: { children: React.ReactNode }) {
	return <code className="break-all font-mono text-2xs text-ink-200">{children}</code>;
}
