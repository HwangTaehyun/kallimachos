import { useEffect } from 'react';

/** While there is an unsaved edit, make **the browser ask again before discarding the page**.
 *
 * It cannot stop a screen change inside the app —— `beforeunload` does not fire on SPA routing.
 * That side was solved by **not unmounting the editor** in the first place:
 *   · tab switching   SettingsPage only hides it
 *   · routing         everOpened in App.tsx keeps it mounted
 * So what is left here is a reload, a tab close, or navigating away.
 *
 * The wording cannot be set —— the browser uses its own sentence.  The only signal is "they cancelled".
 * (The HTML standard: "the user agent is expected to ask the user to confirm" —— the wording is the
 *  user agent's business. https://html.spec.whatwg.org/multipage/browsing-the-web.html
 *
 * `returnValue = ''` is not used.  It used to be the only signal and is now deprecated
 * (tsc 6385), and `preventDefault()` alone works in every current browser.  This tool runs only in
 * a local Chrome, so there is no reason to keep the legacy path.
 */
export function useDirtyGuard(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return;
    const onLeave = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener('beforeunload', onLeave);
    return () => window.removeEventListener('beforeunload', onLeave);
  }, [dirty]);
}
