import { useEffect, useRef, useState } from 'react';
import { fetchGraph } from '../api';
import { tr, useT } from '../i18n/lang';
import S from '../i18n/strings/galaxyView';

declare global {
  interface Window {
    __KAL_GRAPH__?: unknown;
    __KAL_VAULT__?: string;
    /** The bundle leaves a handle here once it has booted —— used as the ready signal */
    galaxy?: { controller: { resize(): void } };
  }
}

/** The 3D knowledge graph.  This app's main screen.
 *
 * Why it is not rewritten in React
 *   The renderer, layout worker and control panel are an existing 820KB bundle, sharing **the same
 *   source** as the Obsidian plugin.  Porting it means maintaining the same thing twice.  Here the
 *   bundle is loaded as-is and only the data is fetched from /api/graph and put in.
 *
 * Why it is only hidden, never unmounted
 *   The bundle is an IIFE: it boots once the moment it loads and cannot be called again.  Unmount
 *   and come back and the screen is blank.  On top of that the layout computation is heavy, so
 *   redoing it every time makes a tab switch take seconds —— hidden, even the camera position survives.
 */
export function GalaxyView({ hidden }: { hidden: boolean }) {
  const [phase, setPhase] = useState<'graph' | 'boot' | 'ready'>('graph');
  const [err, setErr] = useState<string | null>(null);
  const started = useRef(false);
  const t = useT(S);

  useEffect(() => {
    if (started.current) return;   // guards against StrictMode's double run —— the bundle must not load twice
    started.current = true;
    void (async () => {
      try {
        const { graph, vaultName } = await fetchGraph();
        // The bundle reads these two the moment it loads.  They must be set before the script tag.
        window.__KAL_GRAPH__ = graph;
        window.__KAL_VAULT__ = vaultName;
        setPhase('boot');
        // The filename comes from the manifest —— the bundle's name carries a content hash, so a
        // rebuild changes the URL.  With a fixed name the browser kept running the old bundle, and
        // there was no way to notice from the screen alone (2026-08-19).
        const m = await fetch('/galaxy/manifest.json', { cache: 'no-store' });
        if (!m.ok) throw new Error(tr(S)('bundleNotFound'));
        const { js, css } = (await m.json()) as { js: string; css: string };
        await inject('link', { rel: 'stylesheet', href: `/galaxy/${css}` });
        await inject('script', { src: `/galaxy/${js}` });
        await waitForBoot();
        setPhase('ready');
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  // The canvas size is 0 while hidden.  Without re-measuring on becoming visible, the picture is
  // stretched from a single pixel.
  useEffect(() => {
    //  controller is optional too —— there really is a moment in the bundle's boot race where only
    //  the galaxy object is in place (measured 2026-09-01: TypeError reading 'resize' — the first entry right after the manifest was restored).
    if (!hidden && phase === 'ready') window.galaxy?.controller?.resize?.();
  }, [hidden, phase]);

  return (
    <div className={`absolute inset-0 ${hidden ? 'invisible' : ''}`} aria-hidden={hidden}>
      {/* The bundle draws here.  The id is the name the bundle looks for and must not change.
          The size is given as h-full/w-full rather than inset because the bundle attaches
          .galaxy-view-content while booting and that rule is position: relative.  The moment
          absolute becomes relative, inset-0 cannot make a size and the height goes to 0
          (measured 1440x0, canvas 2x2).  In the self-contained HTML #galaxy is position: fixed, so
          the ID selector wins and the problem never showed. */}
      <div id="galaxy" className="h-full w-full" />

      {phase !== 'ready' && !err && (
        <div className="absolute inset-0 grid place-items-center bg-ink-950">
          <p className="text-sm text-ink-400">
            {phase === 'graph' ? t('fetchingGraph') : t('computingLayout')}
          </p>
        </div>
      )}

      {err && (
        <div className="absolute inset-0 grid place-items-center bg-ink-950 p-6">
          <div role="alert" className="max-w-md rounded-card border border-red-500/40 bg-red-950/30 p-5 text-sm">
            <h2 className="font-semibold text-crit">{t('couldntDraw')}</h2>
            <p className="mt-2 text-ink-400">{err}</p>
            <a href="#/settings"
               className="mt-3 inline-block rounded-control border border-ink-700 px-3 py-1.5 text-ink-200 hover:ring-accent hover:text-accent">
              {t('runExport')}
            </a>
          </div>
        </div>
      )}
    </div>
  );
}

/** Loads the css and js once.  If already present it is left alone. */
function inject(tag: 'link' | 'script', attrs: Record<string, string>): Promise<void> {
  const url = attrs.href ?? attrs.src;
  if (document.querySelector(`${tag}[${attrs.href ? 'href' : 'src'}="${url}"]`)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const el = document.createElement(tag);
    Object.assign(el, attrs);
    el.onload = () => resolve();
    el.onerror = () => reject(new Error(tr(S)('couldntLoad')(String(url))));
    document.head.appendChild(el);
  });
}

/** The bundle's boot is asynchronous.  window.galaxy appearing means it is done.
 *
 * On timeout it **gives up quietly** —— on failure the bundle writes its own error message inside
 * #galaxy, so removing the overlay that was covering it gives the user more information.
 */
function waitForBoot(timeoutMs = 120_000): Promise<void> {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const tick = () => {
      if (window.galaxy || Date.now() - t0 > timeoutMs) resolve();
      else setTimeout(tick, 120);
    };
    tick();
  });
}
