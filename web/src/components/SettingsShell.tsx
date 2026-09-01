import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { NAV, getNav, filterNav, findItem, type NavItem } from '../settingsNav';
import { useLang, useT } from '../i18n/lang';
import S from '../i18n/strings/settingsShell';

/**
 * The settings screen's shell —— the contents list on the left, the content on the right.  (Orca's way)
 *
 * Why a sidebar rather than tabs
 *   Horizontal tabs work to three or four.  Six laid out in one row makes **the classification
 *   disappear** —— "Pipeline" and "Paths" sit side by side and read as equal weight.  A vertical
 *   contents list gets the group titles for free: what is operations and what is index settings
 *   becomes visible.  It does not fight for space either —— more items just extend downwards.
 *
 * The keyboard
 *   ⌘,  open (App's job —— it has to work while viewing the galaxy)
 *   ⌘F  jump to the contents search
 *   Esc close and return to the app
 *
 * ⌘F intercepts the browser's "find in page".  The reason it does anyway: this screen is mostly
 * collapsed (the other pages are not even rendered), so the browser's find **cannot find what is
 * not visible** —— the contents search really does find more.  It does not intercept inside an input.
 */
export function SettingsShell({ active, onNav, onClose, hidden = false, children, actions }: {
	active: string;
	onNav: (id: string) => void;
	onClose: () => void;
	/** No shortcuts are registered while hidden —— Esc must not act on the galaxy screen */
	hidden?: boolean;
	children: ReactNode;
	/** To the right of the page title —— things like "Reload" */
	actions?: ReactNode;
}) {
	const [q, setQ] = useState('');
	const search = useRef<HTMLInputElement>(null);
	const lang = useLang();
	const t = useT(S);
	const nav = useMemo(() => getNav(lang), [lang]);
	const groups = useMemo(() => filterNav(q, nav), [q, nav]);
	const item: NavItem | undefined = findItem(active, nav);

	useEffect(() => {
		if (hidden) return;
		const on = (e: KeyboardEvent) => {
			const el = e.target as HTMLElement | null;
			const typing = !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);

			if (e.key === 'Escape') {
				//  Inside an input it leaves the field first.  Closing the window in one go turns
				//  "I meant to cancel the value" into "I closed the screen".
				if (typing) { el!.blur(); return; }
				e.preventDefault();
				onClose();
				return;
			}
			if (e.key === 'f' && (e.metaKey || e.ctrlKey) && !typing) {
				e.preventDefault();
				search.current?.focus();
				search.current?.select();
			}
		};
		window.addEventListener('keydown', on);
		return () => window.removeEventListener('keydown', on);
	}, [hidden, onClose]);

	return (
		<div className="flex h-full min-h-0">
			{/* ── the contents list ── */}
			<nav
				aria-label={t('navLabel')}
				className="flex w-52 shrink-0 flex-col gap-4 overflow-y-auto border-r border-ink-850
				           bg-ink-950 px-3 py-4 md:w-64 md:px-4"
			>
				<button
					type="button"
					onClick={onClose}
					className="flex items-center gap-1.5 self-start rounded-control px-1 py-1 text-xs text-ink-400
					           transition-[color,background-color,box-shadow,scale] duration-150
					           hover:text-ink-100 active:scale-[0.96]"
				>
					<svg viewBox="0 0 24 24" fill="none" className="h-3.5 w-3.5" aria-hidden>
						<path d="M15 5l-7 7 7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
					</svg>
					{t('backToApp')}
				</button>

				<div className="relative">
					<svg viewBox="0 0 24 24" fill="none" aria-hidden
						className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-500">
						<circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="2" />
						<path d="M16 16l4 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
					</svg>
					<input
						ref={search}
						type="search"
						value={q}
						onChange={(e) => setQ(e.target.value)}
						placeholder={t('searchPlaceholder')}
						aria-label={t('searchAriaLabel')}
						className="w-full rounded-control bg-ink-900 py-1.5 pl-8 pr-9 text-xs text-ink-100
						           shadow-card outline-none placeholder:text-ink-500
						           transition-[color,background-color,box-shadow] duration-150
						           focus:ring-1 focus:ring-accent/60"
					/>
					<kbd aria-hidden className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2
					                            text-2xs tabular-nums text-ink-700">⌘F</kbd>
				</div>

				{groups.length === 0 ? (
					<p className="px-1 text-xs leading-relaxed text-ink-500">
						{t('noMatches')(q)}
					</p>
				) : groups.map((g) => (
					<div key={g.title} className="flex flex-col gap-0.5">
						<h2 className="px-2 pb-1 text-2xs font-semibold uppercase tracking-wide text-ink-600">
							{g.title}
						</h2>
						{g.items.map((it) => (
							<button
								key={it.id}
								type="button"
								onClick={() => onNav(it.id)}
								aria-current={active === it.id ? 'page' : undefined}
								className={`rounded-control px-2 py-1.5 text-left text-sm
								            transition-[color,background-color,box-shadow] duration-150 ${
									active === it.id
										? 'bg-ink-800 font-medium text-ink-100'
										: 'text-ink-400 hover:bg-ink-900 hover:text-ink-200'
								}`}
							>
								{it.label}
							</button>
						))}
					</div>
				))}
			</nav>

			{/* ── the content ── */}
			<div className="min-w-0 flex-1 overflow-y-auto">
				<div className={`mx-auto px-5 py-6 md:px-8 md:py-8 ${item?.wide ? '' : 'max-w-3xl'}`}>
					<header className="mb-6 flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
						<div className="min-w-0">
							<h1 className="text-xl font-semibold tracking-tight text-ink-100">
								{item?.label ?? t('settingsFallback')}
							</h1>
							{item?.desc && (
								<p className="mt-1 text-sm leading-relaxed text-ink-400">{item.desc}</p>
							)}
						</div>
						{actions}
					</header>
					{children}
				</div>
			</div>
		</div>
	);
}

export { NAV };
