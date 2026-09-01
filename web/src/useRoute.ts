import { useCallback, useEffect, useState } from 'react';

export type Route = 'galaxy' | 'settings';

/** Hash routing.
 *
 * With only two screens, bringing in a router is not worth it.  The hash alone gives reload,
 * bookmarking and the back button, and it needs no changes to nginx's try_files or vite's
 * historyApiFallback —— a hash never reaches the server.
 *
 * The default is the galaxy.  This app's main event is looking at the graph; running the pipeline is settings.
 */
export function useRoute(): [Route, (r: Route) => void] {
	const [route, setRoute] = useState<Route>(read);
	useEffect(() => {
		const on = () => setRoute(read());
		window.addEventListener('hashchange', on);
		return () => window.removeEventListener('hashchange', on);
	}, []);
	//  ⚠ It used to be a new function on every render.  Used as the effect dependency of the ⌘,
	//    shortcut, that removes and re-adds the keydown listener on every render —— on a screen
	//    where the galaxy runs at 60fps, that is 60 times a second.  It is made once.
	const go = useCallback((r: Route) => {
		window.location.hash = r === 'settings' ? '#/settings' : '#/';
	}, []);
	return [route, go];
}

function read(): Route {
	return window.location.hash.replace(/^#\/?/, '') === 'settings' ? 'settings' : 'galaxy';
}
