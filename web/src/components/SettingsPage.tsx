import { LanguagePage } from './LanguagePage';
import { localizeCatalog } from '../i18n/catalog';
import { getLang, useLang, useT } from '../i18n/lang';
import S from '../i18n/strings/settingsPage';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, type Estimate, type Group, type LlmStatus, type Run, type Status, type Step } from '../api';
import { heavy, sentence } from '../estimate';
import { StatusPanel } from './StatusPanel';
import { StepList } from './StepList';
import { StalePanel } from './StalePanel';
import { RunLog } from './RunLog';
import { AliasPanel } from './AliasPanel';
import { HomonymPanel } from './HomonymPanel';
import { useConfirm } from './Confirm';
import { ErrorBanner } from './ErrorBanner';
import { ConfigPanel } from './ConfigPanel';
import { LoadingBlock } from './Loading';
import { SettingsShell } from './SettingsShell';
import { DEFAULT_ITEM } from '../settingsNav';

/** The settings screen —— everything that decides what the galaxy view draws.
 *
 * The galaxy is the main event and this is settings (opened with ⌘,).  It is not even mounted
 * until first opened (everOpened in App.tsx).  Once opened it **stays mounted even when hidden**
 * —— because the YAML buffer being edited disappeared along with the unmount.
 *
 * Instead it stops polling while hidden (`hidden`).  status hashes the whole vault, so calling it
 * continuously while only the graph is being viewed is wasted work.
 */
export function SettingsPage({ hidden = false, onClose = () => {} }:
  { hidden?: boolean; onClose?: () => void }) {
  const t = useT(S);
  //  Another editor holding an unsaved change locks this one too.  Both panels' "rebuild" is
  //  **the same action** (`refresh_kg --force`), and that action reads the saved file.  Were the
  //  lock per panel, editing homonyms while pressing the alias side's button would run a rebuild
  //  **without that edit**, for 35 minutes —— exactly the case the lock was there to prevent.
  //  Did a run finish while hidden —— a forced refresh happens once on becoming visible again
  const pendingForce = useRef(false);
  const [aliasDirty, setAliasDirty] = useState(false);
  const [homDirty, setHomDirty] = useState(false);

  const [page, setPage] = useState(DEFAULT_ITEM);
  const [ask, confirmUi] = useConfirm();
  const [status, setStatus] = useState<Status | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  //  The live estimate.  On failure it is left an empty object and the screen falls back to the
  //  fixed values —— raising an error here would make the pipeline unusable, and the estimate is supplementary.
  const [est, setEst] = useState<Record<string, Estimate>>({});
  const [groups, setGroups] = useState<Group[]>([]);
  //  The catalogue's source text (the API).  A language change re-localises from here —— it is not re-requested.
  const catalog = useRef<{ steps: Step[]; groups: Group[] } | null>(null);
  const lang = useLang();
  useEffect(() => {
    if (!catalog.current) return;
    const l = localizeCatalog(catalog.current, lang);
    setSteps(l.steps); setGroups(l.groups);
  }, [lang]);
  const [llm, setLlm] = useState<LlmStatus | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  //  An operation (stop, run) failure.  Its lifetime differs from `err` (a fetch failure) —— refresh does not clear it.
  const [opErr, setOpErr] = useState<string | null>(null);
  //  Did the user **pick** a run themselves.  Polling must not override that choice.
  const pinned = useRef(false);
  const [loading, setLoading] = useState(true);
  //  "Reload" has to read **something different on each page**.  It used to always re-read status,
  //  so in settings pressing it **did nothing at all** —— that page looks at /api/config.
  const [configReload, setConfigReload] = useState(0);

  //  A screen holding an edit buffer **stays mounted once opened** (see the render below).
  //  They are not all mounted from the start because /api/config starts Python + LanceDB and takes
  //  1.5 seconds —— not a price to charge someone who came only to look at the pipeline.
  const [everCfg, setEverCfg] = useState(false);
  const [everAlias, setEverAlias] = useState(false);
  const isCfg = page === 'tuning' || page === 'paths';
  const isAlias = page === 'aliases' || page === 'homonyms';
  if (isCfg && !everCfg) setEverCfg(true);
  if (isAlias && !everAlias) setEverAlias(true);

  const refresh = useCallback(async (fresh = false) => {
    try {
      const [st, rs] = await Promise.all([api.status(fresh), api.runs()]);
      setStatus(st);
      setRuns(rs);
      // It attaches to a running run —— so one started from another screen or the CLI is picked up too.
      // If the user is looking at a past run they chose, it is left alone.
      //
      // ⚠ The code was **the opposite of the comment**: `curIsRunning ? cur : running.id` meant a
      //   chosen run that had finished went straight back to the running one.  The only case it
      //   preserved was "what was chosen is already the running one" —— that is, it preserved nothing.
      //   While something ran, the screen bounced every 5 seconds and `useLogStream` cleared the
      //   log with `setLines([])`.  Reading a past log was impossible.  (reproduced by r4-ux)
      //
      //   "Did the user **choose** it" is an event, not a state.  A ref remembers it.
      const running = rs.find((r) => r.status === 'running');
      setActive((cur) => {
        if (!running || pinned.current) return cur;
        return cur === running.id ? cur : running.id;
      });
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    api.steps()
      .then((r) => { catalog.current = r; const l = localizeCatalog(r, getLang()); setSteps(l.steps); setGroups(l.groups); })
      // It must not be swallowed —— an empty pipeline is indistinguishable from "no steps configured",
      // and worse, `start()` never receives the step information and cannot make the confirmation decision.
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
    // LLM availability —— a container may have no claude authentication.  The check starts the CLI
    // once and is therefore slow.  It runs once at boot and the server caches the result.
    api.llm().then(setLlm).catch(() => setLlm(null));
    api.estimate().then(setEst).catch(() => setEst({}));
    void refresh();
  }, [refresh]);

  // It refreshes often only while something is running.  Calling status while idle repeats hashing
  // the whole vault for nothing.
  //
  // While hidden it is not called at all.  This screen is not unmounted when moving to the graph
  // (everOpened in App.tsx —— to protect the YAML being edited).  So without stopping polling here,
  // the vault would keep being hashed in the background the whole time the galaxy is on screen.
  const running = runs.some((r) => r.status === 'running');
  useEffect(() => {
    if (hidden) return;
    //  It catches up **immediately** the moment it becomes visible again.  Without that it shows a
    //  stale screen for up to 60 seconds —— unchanged even when a run finished while hidden.
    //  If a run finished while hidden, the forced refresh (a full vault hash) missed at the time
    //  happens here.  Doing it while hidden would run an expensive job for a screen nobody is
    //  looking at, and not doing it at all leaves the vault comparison stale.  (r2-verify's point, 2026-08-21)
    const force = pendingForce.current;
    pendingForce.current = false;
    void refresh(force);
    const ms = running ? 5000 : 60000;
    const t = setInterval(() => void refresh(), ms);
    return () => clearInterval(t);
  }, [hidden, running, refresh]);

  //  An estimate goes stale the moment a run finishes —— that work was just done, so less is left.
  //  It is called only when `running` falls true→false (not a 3–4 second call on every poll).
  useEffect(() => {
    if (running || hidden) return;
    api.estimate().then(setEst).catch(() => {});
  }, [running, hidden]);

  const run = async (id: string, args: string[] = []) => {
    try {
      const r = await api.start(id, args);
      pinned.current = false;   // a newly started run is followed
      setActive(r.id);
      setOpErr(null);
      void refresh();
    } catch (e) {
      //  An operation error goes to `opErr`.  The previous commit moved stop and **missed this one** ——
      //  a run failure (412 "run it on the host with `just <id>`", 409 "already … running")
      //  landing in `err` is cleared by the next successful poll's `setErr(null)`.
      //  It disappears mid-read.  That is the defect this round set out to remove.  (r4-ux round 5)
      setOpErr(e instanceof Error ? e.message : String(e));
    }
  };

  /** The pre-run confirmation.  **Every caller passes through here** —— StepList's run button and
   *  the alias and homonym panels' "rebuild the graph" alike.  Attaching a confirmation per caller
   *  eventually misses one, and that one is the most expensive step there is.
   *
   *  It does not ask about everything —— asked every time, a person learns to press without reading.
   *  It asks only about what changes the DB or runs for over 5 minutes. */
  const start = (id: string, args: string[] = []) => {
    const s = steps.find((x) => x.id === id);
    // **It asks when it does not know the step.**  It used to read `!s` as "nothing to ask about"
    // and just run, which is backwards:
    //   · `steps` arrives asynchronously.  Pressed before it does, s is absent.
    //   · The alias and homonym panels never look at `steps` at all —— their button is always live.
    //   · If `/api/steps` fails, `steps` is **permanently** an empty array.
    // The last case is particularly bad —— every confirmation is off, silently and permanently.
    // In that state, "rebuild the graph" is a 35-minute DB overwrite.
    if (s && !heavy(s, est[id])) return void run(id, args);
    ask({
      title: t('runTitle')(s ? s.title : id),
      body: s
        ? `${s.desc}\n\n${sentence(s, est[id])}` +
          (s.writes_db ? t('dbWarning') : '.')
        : t('unknownStepBody'),
      cta: t('runCta'),
      onOk: () => void run(id, args),
    });
  };

  //  "Reload" —— what it reads differs per page.  Aliases and homonyms hold an edit buffer, so they
  //  are locked: reloading would throw away hand-written YAML.
  const reload = () => (isCfg ? setConfigReload((n) => n + 1) : void refresh(true));

  return (
    <SettingsShell
      active={page}
      onNav={setPage}
      onClose={onClose}
      hidden={hidden}
      actions={
        <button
          type="button"
          onClick={reload}
          disabled={isAlias}
          title={isAlias ? t('lockedReloadTitle') : undefined}
          className="shrink-0 rounded-control px-3 py-1.5 text-xs text-ink-400 ring-1 ring-ink-800
                     transition-[color,background-color,box-shadow,scale] duration-150
                     enabled:hover:text-ink-100 enabled:hover:ring-ink-700 enabled:active:scale-[0.96]
                     disabled:cursor-not-allowed disabled:opacity-35"
        >
          {t('reload')}
        </button>
      }
    >
      {/*  The two banners have different lifetimes —— `opErr` (an operation failure) has to be closed
           by a person, while `err` (a fetch failure) disappears on its own once a poll succeeds.  So
           the close button is on `opErr` alone. */}
      {opErr && <ErrorBanner text={opErr} onClose={() => setOpErr(null)} />}
      {err && <ErrorBanner text={err} />}

      {loading && !status && (page === 'pipeline' || page === 'status') ? (
        <LoadingBlock label={t('loadingStatus')} />
      ) : page === 'pipeline' ? (
        <div className="flex flex-col gap-6">
          {/* The star of this screen.  It uses the full width —— five nodes have to sit side by side */}
          <StepList
            steps={steps}
            groups={groups}
            est={est}
            status={status}
            runs={runs}
            busy={running}
            llm={llm}
            onStart={start}
            //  What was chosen directly cannot be overridden by polling (see the pinned comment above)
            onSelect={(id: string) => { pinned.current = true; setActive(id); }}
          />
          <RunLog
            runId={active}
            runs={runs}
            onCancel={async (id) => {
              //  An operation error is held **separately**.  Put in `err`, the very next line's
              //  `refresh()` succeeds and clears it with `setErr(null)` —— the banner disappears
              //  within a second, giving the same result as the `.catch(() => {})` that was removed.
              //  Pressing stop on a finished run (the button is still visible within the 5-second
              //  poll window) makes the server return a 404 "not running".  (r4-ux)
              try { await api.cancel(id); setOpErr(null); }
              catch (e) { setOpErr(e instanceof Error ? e.message : String(e)); }
              void refresh();
            }}
            //  Deferred while hidden —— RunLog keeps the stream alive on the graph screen too, and its
            //  completion event calls a full vault hash.  That would run for a screen nobody is watching.
            onFinished={() => { if (hidden) pendingForce.current = true; else void refresh(true); }}
          />
        </div>
      ) : page === 'language' ? (
        <LanguagePage />
      ) : page === 'status' ? (
        <div className="flex flex-col gap-8">
          {status && <StatusPanel status={status} />}
          {status && <StalePanel status={status} />}
        </div>
      ) : null}

      {/* A screen holding an edit buffer is **not unmounted** when the page changes —— it is only
          hidden.  The buffer lives in react-hook-form and draft state, so unmounting loses the
          hand-written YAML and the number just typed with no warning (measured: 551 chars →
          edited to 582 → another screen → back to 551).  The same idiom as GalaxyView.

          The pipeline side is the opposite and is unmounted —— RunLog streams the run log, so
          keeping it alive while hidden leaves a connection open for a screen nobody is watching. */}
      {everCfg && (
        <div className={isCfg ? '' : 'hidden'}>
          <ConfigPanel
            busy={running}
            reloadKey={configReload}
            onReindex={() => start('index')}
            show={page === 'paths' ? 'paths' : 'tuning'}
            status={status}
          />
        </div>
      )}

      {everAlias && (
        <>
          <div className={page === 'aliases' ? '' : 'hidden'}>
            <AliasPanel
              onApply={() => start('refresh_kg', ['--force'])}
              onApplyFast={() => start('apply_aliases')}
              busy={running}
              otherDirty={homDirty}
              onDirty={setAliasDirty}
            />
          </div>
          {/* The opposite direction —— splitting beside joining.  A person confirms both. */}
          <div className={page === 'homonyms' ? '' : 'hidden'}>
            <HomonymPanel
              onApply={() => start('refresh_kg', ['--force'])}
              busy={running}
              otherDirty={aliasDirty}
              onDirty={setHomDirty}
            />
          </div>
        </>
      )}

      {confirmUi}
    </SettingsShell>
  );
}
