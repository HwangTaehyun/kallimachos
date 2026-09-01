import { useCallback, useEffect, useRef, useState } from 'react';
import { Loading } from './Loading';
import { useForm } from 'react-hook-form';
import { api, type Suggestion } from '../api';
import { useDirtyGuard } from '../useDirtyGuard';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/aliasPanel';

type FormValues = { content: string };

export function AliasPanel({ onApply, onApplyFast, busy, otherDirty, onDirty }: {
  onApply: () => void; onApplyFast: () => void; busy: boolean;
  /** Does another editor hold an unsaved change (the parent merges and passes it down) */
  otherDirty: boolean;
  /** Raises my dirty state to the parent */
  onDirty: (d: boolean) => void;
}) {
  const t = useT(S);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [weak, setWeak] = useState(false);
  const [loadingSug, setLoadingSug] = useState(true);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const {
    register, handleSubmit, reset, setValue, getValues,
    formState: { isDirty, isSubmitting },
  } = useForm<FormValues>({ defaultValues: { content: '' } });

  // It only stops a reload or tab close throwing away an edit.  A screen change inside the app was
  // already solved by not unmounting the editor (see useDirtyGuard's comment).
  useDirtyGuard(isDirty);
  //  The parent merges the two editors' dirty states and locks them against each other (see the comment below)
  useEffect(() => { onDirty(isDirty); }, [isDirty, onDirty]);

  // Starting to edit again takes "saved" down.  Left up, it holds an unsaved change while the
  // screen says it is saved —— a flat contradiction with the "unsaved changes" right beside it.
  useEffect(() => { if (isDirty) setSaved(null); }, [isDirty]);

  //  ⚠ The editor opens **only when loading succeeded.**
  //    A failure used to show a red line at the bottom while the textarea stayed the empty string
  //    from `defaultValues`.  One character makes `isDirty` true and unlocks save ——
  //    pressing it sends that one character to the server, which **overwrites the real file**
  //    (`yaml.safe_load("") or {}` passes validation).  The alias file disappears wholesale.
  //    The cause was the screen not distinguishing "the file is empty" from "it could not be loaded".
  //    (r4-ux, 2026-08-21)
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(() => {
    setErr(null);
    api.aliases()
      .then((r) => { reset({ content: r.content }); setLoaded(true); })
      .catch((e) => { setLoaded(false); setErr(String(e)); });
  }, [reset]);
  useEffect(() => { load(); }, [load]);

  //  ⚠ Two things lived here.
  //  ① A race —— turning "including inclusion rules" on and off fires two requests together.  The
  //     `weak=1` one calls Python, so it is slower and **arrives later**, leaving the checkbox off
  //     while the result is weak, **permanently**.  The first `.finally` has already cleared
  //     "searching…", so there is no signal a second one is on its way.
  //  ② One `err` field was shared by the file load, the candidate fetch and the save.  A failed
  //     candidate fetch put a red line **directly under the save button**, reading as a save failure.
  //     And it was not cleared when the next fetch succeeded.  (r4-ux)
  const sugGen = useRef(0);
  const [sugErr, setSugErr] = useState<string | null>(null);
  const loadSuggestions = useCallback((w: boolean) => {
    const my = ++sugGen.current;
    setLoadingSug(true);
    api.suggestions(w)
      .then((r) => { if (my === sugGen.current) { setSuggestions(r); setSugErr(null); } })
      .catch((e) => { if (my === sugGen.current) setSugErr(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (my === sugGen.current) setLoadingSug(false); });
  }, []);

  useEffect(() => { loadSuggestions(weak); }, [weak, loadSuggestions]);

  const onSubmit = handleSubmit(async (v) => {
    setErr(null); setSaved(null);
    try {
      const r = await api.saveAliases(v.content);
      reset({ content: v.content });   // the saved value becomes the new baseline (clearing isDirty)
      setSaved(r.note);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  });

  /** Puts a candidate into the YAML.  The one with more documents becomes the representative ——
   * it is already the central spelling in the graph.
   *
   * ⚠ Pressing the same representative twice must not simply append a block.  A duplicated YAML
   *   top-level key makes `safe_load` keep **only the last**, so the variant added earlier quietly
   *   disappears —— and the save succeeds with a 200.  An existing key gets it slotted underneath.
   *   (2026-08-18 adversarial review)
   */
  const accept = (s: Suggestion) => {
    const [canon, variant] = s.a_docs >= s.b_docs ? [s.a, s.b] : [s.b, s.a];
    const cur = getValues('content');
    const lines = cur.split('\n');
    const keyAt = lines.findIndex((l) => l.trimEnd() === `${canon}:`);
    if (keyAt < 0) {
      const block = `${canon}:\n  - ${variant}\n`;
      setValue('content', cur.trimEnd() + '\n\n' + block, { shouldDirty: true });
      return;
    }
    // Inserted after this key's last entry
    let end = keyAt + 1;
    while (end < lines.length && /^\s+-\s/.test(lines[end] ?? '')) end++;
    if (lines.slice(keyAt + 1, end).some((l) => l.replace(/^\s*-\s*/, '').trim() === variant)) {
      return;                       // already there
    }
    lines.splice(end, 0, `  - ${variant}`);
    setValue('content', lines.join('\n'), { shouldDirty: true });
  };

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <section className="rounded-card border border-ink-800 bg-ink-900/50" aria-labelledby="alias-h">
        <h2 id="alias-h" className="border-b border-ink-800 px-4 py-3 text-sm font-semibold">
          aliases.yml
          <span className="ml-2 font-normal text-ink-400">{t('humanConfirmedOnly')}</span>
        </h2>
        <form onSubmit={onSubmit} className="flex flex-col gap-3 p-4">
          {!loaded && (

            <div role="alert" className="rounded-control border border-red-800/60 bg-red-950/30 p-3 text-sm text-crit">

              {t('loadFailedPrefix')}

              <strong className="ml-1">{t('loadFailedStrong')}</strong>{t('loadFailedSuffix')}

              <button type="button" onClick={load}

                className="ml-2 underline hover:text-crit">{t('retry')}</button>

            </div>

          )}
          <label htmlFor="alias-content" className="sr-only">{t('aliasContentLabel')}</label>
          <textarea
            id="alias-content"
            disabled={!loaded}
            spellCheck={false}
            rows={22}
            {...register('content')}
            className="w-full resize-y rounded-card border border-ink-700 bg-ink-950 p-3
                       font-mono text-xs leading-relaxed text-ink-200
                       focus:ring-accent focus:outline-none"
          />
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="submit"
              disabled={!loaded || !isDirty || isSubmitting}
              className="rounded-control border border-ink-700 px-3 py-1.5 text-sm transition-[color,background-color,box-shadow]
                         enabled:hover:ring-accent enabled:hover:text-accent
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {isSubmitting ? t('saving') : t('save')}
            </button>
            {/* Both apply buttons read **the saved file** —— pressed after editing without saving,
                a long rebuild runs without the alias just written. */}
            <button
              type="button"
              disabled={busy || isDirty || otherDirty}
              title={(isDirty || otherDirty) ? t('unsavedApplyHint') : undefined}
              onClick={onApply}
              className="rounded-control border border-ink-700 px-3 py-1.5 text-sm transition-[color,background-color,box-shadow]
                         enabled:hover:border-cyan-glow enabled:hover:text-cyan-glow
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t('applyToGraph')}
            </button>
            <button
              type="button"
              /* ⚠ Only here is `otherDirty` not consulted.  `apply_aliases` is
                 `refresh_kg --aliases-only` and **skips extraction** —— `split_sense`, which splits
                 homonyms, is called only in the extraction step (lr_extract), so this path never
                 reads homonyms.yml at all.  Locking this too would be over-locking ——
                 blocking what can be done is the same kind of bug.

                 ⚠ `title` has to use **the same condition**.  This round put `otherDirty` on the
                   title alone, so with homonyms dirty it said "save first" while the button stayed
                   live —— a contradiction.  (r4-ux round 5) */
              disabled={busy || isDirty}
              title={isDirty ? t('unsavedApplyHint')
                             : t('applyFastHint')}
              onClick={onApplyFast}
              className="rounded-control border border-ink-700 px-3 py-1.5 text-sm transition-[color,background-color,box-shadow]
                         enabled:hover:ring-accent enabled:hover:text-accent
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {t('applyAliasesOnly')}
            </button>
            {isDirty && <span className="text-xs text-warn">{t('unsavedChanges')}</span>}
          </div>
          <p className="text-xs text-ink-400">
            <span className="text-ink-200">{t('applyAliasesOnlyLabel')}</span>{t('applyAliasesOnlyDesc')}
          </p>
          {saved && <p className="text-xs text-ok">{t('savedNotePrefix')}{saved}</p>}
          {err && <p role="alert" className="text-xs text-crit">{err}</p>}
          <p className="text-xs text-ink-400">
            {t('aliasHintPrefix')}
            <strong className="font-medium text-ink-200">{t('aliasHintStrong')}</strong>{t('aliasHintSuffix')}
          </p>
        </form>
      </section>

      <section className="rounded-card border border-ink-800 bg-ink-900/50" aria-labelledby="sug-h">
        <div className="flex flex-wrap items-center gap-2 border-b border-ink-800 px-4 py-3">
          <h2 id="sug-h" className="text-sm font-semibold">{t('aliasCandidates')}</h2>
          <span className="text-xs text-ink-400">
            {loadingSug ? <Loading label={t('findingCandidates')} size="sm" /> : t('pairsCount')(suggestions.length)}
          </span>
          <label className="ml-auto flex items-center gap-1.5 text-xs text-ink-400">
            <input type="checkbox" checked={weak} onChange={(e) => setWeak(e.target.checked)}
              className="accent-amber-glow" />
            {t('includeSubstringRules')}
          </label>
        </div>
        {/* A failed candidate fetch appears **here** —— not under the save button. */}
        {sugErr && (
          <div role="alert" className="mx-4 mb-2 rounded-control border border-red-800/60 bg-red-950/30 p-2 text-xs text-crit">
            {t('failedLoadCandidates')}{sugErr}
          </div>
        )}
        <ul className="max-h-[34rem] divide-y divide-ink-800 overflow-y-auto">
          {suggestions.map((s, i) => (
            <li key={`${s.a}|${s.b}|${i}`} className="flex items-center gap-3 px-4 py-2.5 text-sm">
              <span className="w-16 shrink-0 rounded bg-ink-800 px-1.5 py-0.5 text-center text-2xs text-ink-400">
                {s.reason}
              </span>
              <span className="min-w-0 flex-1 truncate">
                {s.a} <span className="text-ink-400 tabular-nums">({s.a_docs})</span>
                <span className="mx-1.5 text-ink-400">↔</span>
                {s.b} <span className="text-ink-400 tabular-nums">({s.b_docs})</span>
              </span>
              <span className="hidden shrink-0 text-2xs text-ink-400 sm:inline">{s.type}</span>
              {/* With the editor locked there is nowhere to put it —— [retry] discards it with a reset */}
              <button
                type="button"
                disabled={!loaded}
                onClick={() => accept(s)}
                className="shrink-0 rounded-control border border-ink-700 px-2 py-1 text-xs
                           hover:ring-accent hover:text-accent transition-[color,background-color,box-shadow]"
              >
                {t('add')}
              </button>
            </li>
          ))}
          {!loadingSug && suggestions.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-ink-400">{t('noCandidates')}</li>
          )}
        </ul>
      </section>
    </div>
  );
}
