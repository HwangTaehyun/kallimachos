/**
 * The loading indicator.
 *
 * Why text alone is not enough —— "reading status…" is **indistinguishable from stuck.**
 * Both of this screen's fetches really are slow: `/api/status` hashes the whole vault and
 * `/api/config` starts Python + LanceDB (measured 1.5 seconds).  With only one motionless line of
 * text through that, a person presses reload at about 3 seconds.
 *
 * One moving thing conveys "it is running".  Two things are honoured in exchange:
 *   · `role="status"` + `aria-live` —— a screen reader has to know too.
 *   · `prefers-reduced-motion` —— it stops for anyone who asked for less motion.
 *     The text remains then, so no information is lost.
 */

export function Loading({ label, size = 'md' }: { label: string; size?: 'sm' | 'md' }) {
	const box = size === 'sm' ? 'h-3.5 w-3.5' : 'h-5 w-5';
	const text = size === 'sm' ? 'text-xs' : 'text-sm';
	return (
		<span role="status" aria-live="polite" className={`inline-flex items-center gap-2 ${text} text-ink-400`}>
			<Spinner className={box} />
			{label}
		</span>
	);
}

/** For waiting on a whole section of the screen.  Centred, with padding. */
export function LoadingBlock({ label }: { label: string }) {
	return (
		<div className="flex items-center justify-center py-10">
			<Loading label={label} />
		</div>
	);
}

function Spinner({ className }: { className: string }) {
	return (
		<svg className={`${className} gx-spin shrink-0`} viewBox="0 0 24 24" fill="none" aria-hidden>
			{/* The faint background ring —— without it there is no seeing how far the spinning piece goes */}
			<circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" opacity="0.2" />
			<path
				d="M21 12a9 9 0 0 0-9-9"
				stroke="currentColor"
				strokeWidth="2"
				strokeLinecap="round"
			/>
		</svg>
	);
}
