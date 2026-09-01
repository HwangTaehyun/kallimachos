import { useEffect, useState } from 'react';
import { GalaxyView } from './components/GalaxyView';
import { SettingsPage } from './components/SettingsPage';
import { useRoute, type Route } from './useRoute';
import { useT } from './i18n/lang';
import S from './i18n/strings/app';

/** Is it ⌘ or Ctrl.  Printing a key that does not exist is not guidance but a lie. */
export const MOD = typeof navigator !== 'undefined'
	&& /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
	? '⌘' : 'Ctrl+';

/** The main event is the galaxy view (the 3D knowledge graph).  Operating the pipeline has receded into settings ——
 *  most reasons for opening this tool are "to see what is in there", not to run something.
 */
export default function App() {
	const [route, go] = useRoute();

	// Once settings has been **opened even once, it stays mounted** from then on.
	//
	// Why: a route change used to unmount SettingsPage wholesale, and that took the react-hook-form
	// buffers of the aliases.yml / homonyms.yml editors with it.
	// Pressing the graph tab with "unsaved changes" on screen threw away hand-written YAML with no
	// warning (measured: 551 chars → edited to 582 → graph → back to 551).
	//
	// Why not mount it from the start: at boot, `api.llm()` starts the claude CLI once.  There is no
	// reason to charge that to someone who opened it only to look at the galaxy.
	//
	// While hidden, SettingsPage stops its own polling (the `hidden` prop).
	const [everOpened, setEverOpened] = useState(route === 'settings');
	if (route === 'settings' && !everOpened) setEverOpened(true);

	//  ⌘, —— open and close settings.  It has to be **here** so it works while viewing the galaxy
	//  (SettingsPage is not even mounted until it is first opened, so it cannot live there).
	useEffect(() => {
		const on = (e: KeyboardEvent) => {
			if (e.key !== ',' || !(e.metaKey || e.ctrlKey)) return;
			e.preventDefault();
			go(route === 'settings' ? 'galaxy' : 'settings');
		};
		window.addEventListener('keydown', on);
		return () => window.removeEventListener('keydown', on);
	}, [route, go]);

	return (
		//  Settings sits **below** the header.  There are two reasons it does not cover it:
		//    ① there must not be only one way out.  The wordmark is an always-visible way home.
		//    ② covering it means fighting over z-index —— the header is a flex item at `z-20`, and
		//       z-index applies to a flex item even with position static
		//       (CSS Flexbox §5.4).  The header really did draw on top, hiding the sidebar's
		//       "back to the app" entirely and cutting the page title in half
		//       (checked on screen 2026-08-23).  Put in a sibling container, there is no such fight.
		<div className="flex h-full flex-col">
			<Header route={route} onGo={go} />

			<div className="relative min-h-0 flex-1">
				<GalaxyView hidden={route !== 'galaxy'} />

				{everOpened && (
					<div
						className={`absolute inset-0 bg-ink-950 ${route === 'settings' ? '' : 'invisible'}`}
						aria-hidden={route !== 'settings'}
					>
						{/* The scrolling is owned separately by SettingsShell in its two columns —— the contents
						    list scrolls as a contents list and the content as content.  Wrapping again here gives two layers of scroll. */}
						<SettingsPage hidden={route !== 'settings'} onClose={() => go('galaxy')} />
					</div>
				)}
			</div>
		</div>
	);
}

function Header({ route, onGo }: { route: Route; onGo: (r: Route) => void }) {
	const inSettings = route === 'settings';
	const t = useT(S);
	return (
		<header className="z-20 flex shrink-0 flex-wrap items-center gap-3 border-b border-ink-850
		                   bg-ink-950/85 px-4 py-2.5 backdrop-blur md:px-6">
			{/*  The wordmark is the way home —— pressing a logo on the web to go home is an idiom that
			     needs no explanation.  The words after it say **where you are**, so it doubles as a
			     breadcrumb. */}
			<h1 className="text-base font-semibold tracking-tight">
				<button
					type="button"
					onClick={() => onGo('galaxy')}
					title={t('toGraphTitle')}
					className="rounded-control px-1 py-0.5 -mx-1
					           transition-[color,background-color,box-shadow,scale] duration-150
					           hover:bg-ink-900 active:scale-[0.96]"
				>
					Kallimachos{' '}
					<span className="font-normal text-ink-400">· {inSettings ? t('settings') : t('graphBreadcrumb')}</span>
				</button>
			</h1>

			{/*  There is only one place to go, so it is a button rather than a tab.  And it names
			     **where it goes, not where you are** —— a "Settings" button visible while in settings
			     gives no idea what it does.  The shortcut is printed with it: whoever knows it will
			     not press, and whoever does not learns it here. */}
			<button
				type="button"
				onClick={() => onGo(inSettings ? 'galaxy' : 'settings')}
				className="ml-auto flex items-center gap-2 rounded-control px-3 py-1.5 text-sm text-ink-400
				           transition-[color,background-color,box-shadow,scale] duration-150
				           hover:bg-ink-900 hover:text-ink-200 active:scale-[0.96]"
			>
				{inSettings ? <GraphIcon /> : <GearIcon />}
				{inSettings ? t('graph') : t('settings')}
				<kbd className="rounded bg-ink-900 px-1.5 py-0.5 text-2xs text-ink-500">
					{inSettings ? 'Esc' : `${MOD},`}
				</kbd>
			</button>
		</header>
	);
}

/** A set of nodes and the lines joining them —— what this app shows, in itself. */
function GraphIcon() {
	return (
		<svg viewBox="0 0 24 24" fill="none" className="h-4 w-4" aria-hidden>
			<path d="M8 16.4l3-7.2M13.6 7.9l2.9 6M9.4 18.6l6.1-1.4"
				stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
			<circle cx="6.5" cy="18.5" r="2.2" stroke="currentColor" strokeWidth="2" />
			<circle cx="18" cy="16.5" r="2.2" stroke="currentColor" strokeWidth="2" />
			<circle cx="12" cy="6.5" r="2.2" stroke="currentColor" strokeWidth="2" />
		</svg>
	);
}

function GearIcon() {
	return (
		<svg viewBox="0 0 24 24" fill="none" className="h-4 w-4" aria-hidden>
			<circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" />
			<path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 110-4h.09A1.65 1.65 0 004.6 8a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 3.6 1.65 1.65 0 0010 2.09V2a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 8v0a1.65 1.65 0 001.51 1H21a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"
				stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
		</svg>
	);
}

export type { Route };
