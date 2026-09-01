import { Component, type ErrorInfo, type ReactNode } from 'react';
import { tr } from '../i18n/lang';
import S from '../i18n/strings/errorBoundary';

/**
 * When a render throws, show **what broke instead of a blank page**.
 *
 * Why —— React unmounts the whole tree on an unhandled render exception.  With no boundary at all
 * the screen goes completely empty.  This is a local tool, so the user has neither a reason nor a
 * way to know to open the console.  It really happened: `status.py` returned the empty-DB state in
 * **a different shape**, `StatusPanel` threw reading `d.indexed`, and Python's traceback moved to
 * a blank page in the browser (2026-08-25 deep review, Round 6).
 *
 * That data problem itself is fixed.  This is for **the next one** —— a blank screen and a
 * displayed error take completely different amounts of time to fix.
 */
type Props = { children: ReactNode };
type State = { error: Error | null };

export default class ErrorBoundary extends Component<Props, State> {
	state: State = { error: null };

	static getDerivedStateFromError(error: Error): State {
		return { error };
	}

	componentDidCatch(error: Error, info: ErrorInfo) {
		//  Left in the console too —— the stack does not all fit on screen.
		console.error('[kal] exception during render', error, info.componentStack);
	}

	render() {
		const { error } = this.state;
		if (!error) return this.props.children;
		const t = tr(S);
		return (
			<div className="m-6 rounded-lg border border-red-400/40 bg-red-950/30 p-6 text-sm">
				<h2 className="mb-2 text-base font-semibold text-red-300">{t('heading')}</h2>
				<p className="mb-4 text-neutral-300">
					{t('bodyPre')}<code>just status</code>{t('bodyPost')}
				</p>
				<pre className="overflow-x-auto whitespace-pre-wrap rounded bg-black/40 p-3 text-xs text-red-200">
					{error.message}
					{error.stack ? `\n\n${error.stack}` : ''}
				</pre>
				<button
					type="button"
					className="mt-4 rounded bg-neutral-700 px-3 py-1.5 text-neutral-100 hover:bg-neutral-600"
					onClick={() => this.setState({ error: null })}
				>
					{t('retry')}
				</button>
			</div>
		);
	}
}
