import type { ReactNode } from 'react';

/**
 * One settings row —— **what it is on the left, and what changes it on the right.**
 *
 * Why a row rather than a card
 *   Each setting used to be a card, laid out in a 2-column grid.  That moves the eye in a zigzag,
 *   and every card having a border makes **what belongs together** disappear.  Settings are a
 *   list, not a dashboard —— reading top to bottom, once, is right.
 *
 * The label and description sit together on the left, and the control is aligned to the right
 * edge.  With the values in one vertical line, "what is not at its default" reads.
 */
export function SettingRow({ label, htmlFor, desc, badges, note, children }: {
	label: string;
	/** Ties the label to the control.  Omitted when the control is not an input (a button, or text). */
	htmlFor?: string;
	desc?: ReactNode;
	/** A chip beside the label —— the source, or "needs a re-index" */
	badges?: ReactNode;
	/** A note under the description.  For what is **long and conditional**, like the reason it is locked. */
	note?: ReactNode;
	children?: ReactNode;
}) {
	return (
		<div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2.5 px-4 py-3.5">
			<div className="min-w-0 flex-1 basis-72">
				<div className="flex flex-wrap items-center gap-x-2 gap-y-1">
					{htmlFor
						? <label htmlFor={htmlFor} className="text-sm font-medium text-ink-100">{label}</label>
						: <span className="text-sm font-medium text-ink-100">{label}</span>}
					{badges}
				</div>
				{desc && <p className="mt-1 text-xs leading-relaxed text-ink-400">{desc}</p>}
				{note}
			</div>
			{children && <div className="flex shrink-0 flex-wrap items-center gap-2">{children}</div>}
		</div>
	);
}

/**
 * A group of rows.
 *
 * Concentric radii: the outer card is `rounded-card` (10px) and the input inside is `rounded-control` (6px).
 * The row itself sits flush against the card and takes no radius —— giving each row corners looks
 * like pills rolling around inside the card.
 */
export function SettingGroup({ title, desc, children, footer }: {
	title?: string;
	desc?: ReactNode;
	children: ReactNode;
	footer?: ReactNode;
}) {
	return (
		<section className="flex flex-col gap-2">
			{/*  Wrapping it in `title &&` made **a group given a description but no title lose the
			     description too.**  "Saved in ~/.kal/config.json" and "cannot be changed from the
			     screen in a container" had both vanished (checked on screen 2026-08-23).
			     The header is rendered if **either** is present. */}
			{(title || desc) && (
				<header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1">
					{title && <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-500">{title}</h2>}
					{desc && <p className="text-xs text-ink-500">{desc}</p>}
				</header>
			)}
			<div className="divide-y divide-ink-850 rounded-card bg-ink-900/60 shadow-card">
				{children}
			</div>
			{footer && <div className="px-1">{footer}</div>}
		</section>
	);
}
