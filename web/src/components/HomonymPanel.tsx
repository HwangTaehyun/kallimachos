import { useCallback, useEffect, useState } from 'react';
import { Loading } from './Loading';
import { useForm } from 'react-hook-form';
import { api } from '../api';
import { useDirtyGuard } from '../useDirtyGuard';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/homonymPanel';

type FormValues = { content: string };

/** The homonym rule editor —— the opposite direction to `AliasPanel`.
 *
 *   aliases.yml    several names → one node    (joins)
 *   homonyms.yml   one name → several nodes    (splits)
 *
 * Why the candidates arrive as **a block of YAML** rather than JSON: an alias candidate is a
 * yes/no on "are these two the same" and makes a list, while a homonym needs a person to fill in
 * the `cue` (a word appearing only in that sense).  Structuring it only ends up in an editor
 * anyway, so it is given in a form that can be pasted.
 */
export function HomonymPanel({ onApply, busy, otherDirty, onDirty }: {
  onApply: () => void; busy: boolean;
  /** Does another editor hold an unsaved change (the parent merges and passes it down) */
  otherDirty: boolean;
  /** Raises my dirty state to the parent */
  onDirty: (d: boolean) => void;
}) {
  const t = useT(S);
  const [sug, setSug] = useState<string>('');
  const [weak, setWeak] = useState(false);
  const [loadingSug, setLoadingSug] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const {
    register, handleSubmit, reset, getValues, setValue,
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
  //    (`yaml.safe_load("") or {}` passes validation).  The homonym file disappears wholesale.
  //    The cause was the screen not distinguishing "the file is empty" from "it could not be loaded".
  //    (r4-ux, 2026-08-21)
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(() => {
    setErr(null);
    api.homonyms()
      .then((r) => { reset({ content: r.content }); setLoaded(true); })
      .catch((e) => { setLoaded(false); setErr(String(e)); });
  }, [reset]);
  useEffect(() => { load(); }, [load]);

  const loadSug = useCallback((w: boolean) => {
    setLoadingSug(true);
    api.homonymSuggestions(w)
      .then((r) => setSug(r.yaml))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoadingSug(false));
  }, []);

  const onSubmit = handleSubmit(async ({ content }) => {
    setErr(null); setSaved(null);
    try {
      const r = await api.saveHomonyms(content);
      setSaved(r.note || t('savedFallback'));
      reset({ content });
    } catch (e) {
      setErr(String(e));
    }
  });

  return (
    <section className="rounded-card border border-ink-800 bg-ink-900/50" aria-labelledby="hom-h">
      <h2 id="hom-h" className="border-b border-ink-800 px-4 py-3 text-sm font-semibold">
        homonyms.yml
        <span className="ml-2 font-normal text-ink-400">
          {t('headerSubtitle')}
        </span>
      </h2>

      <div className="space-y-3 p-4">
        <p className="text-xs leading-relaxed text-ink-400">
          {t('introPre')}<code className="text-ink-200">vault</code>{t('introMid1')}
          <strong>{t('introBold1')}</strong>
          {t('introMid2')}<strong>{t('introBold2')}</strong>{t('introMid3')}
          <code className="text-ink-200">{t('rebuildGraph')}</code>{t('introPost')}
        </p>

        <form onSubmit={onSubmit} className="space-y-2">
          {!loaded && (

            <div role="alert" className="rounded-control border border-red-800/60 bg-red-950/30 p-3 text-sm text-crit">

              {t('errNoLoad')}

              <strong className="ml-1">{t('errNoLoadStrong')}</strong>{t('errNoLoadEnd')}

              <button type="button" onClick={load}

                className="ml-2 underline hover:text-crit">{t('retry')}</button>

            </div>

          )}
          <label htmlFor="hom-content" className="sr-only">{t('contentLabel')}</label>
          <textarea
            id="hom-content"
            disabled={!loaded}
            {...register('content')}
            spellCheck={false}
            rows={14}
            className="w-full rounded-card border border-ink-800 bg-ink-950 p-3 font-mono
                       text-xs leading-relaxed text-ink-100 focus:border-ink-600
                       focus:outline-none"
          />
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={!loaded || !isDirty || isSubmitting}
              className="rounded-card border border-ink-700 px-3 py-1.5 text-xs
                         hover:bg-ink-800 disabled:opacity-40"
            >
              {isSubmitting ? t('saving') : t('save')}
            </button>
            {/* Not pressable before saving.  This button reads **the saved file** and rebuilds ——
                pressed after editing without saving, it runs for an hour and produces a graph
                missing the rule just written.  (The note above says "press after fixing", and saving belongs in between.) */}
            <button
              type="button"
              onClick={onApply}
              disabled={busy || isDirty || otherDirty}
              title={(isDirty || otherDirty) ? t('unsavedTitle') : undefined}
              className="rounded-card border border-ink-700 px-3 py-1.5 text-xs
                         hover:bg-ink-800 disabled:opacity-40"
            >
              {t('rebuildGraph')}
            </button>
            {isDirty && <span className="text-xs text-warn">{t('unsavedChanges')}</span>}
            {saved && <span className="text-xs text-ok">{saved}</span>}
            {err && <span className="text-xs text-rose-400">{err}</span>}
          </div>
        </form>

        <div className="border-t border-ink-800 pt-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => loadSug(weak)}
              disabled={loadingSug}
              className="rounded-card border border-ink-700 px-3 py-1.5 text-xs
                         hover:bg-ink-800 disabled:opacity-40"
            >
              {loadingSug ? <Loading label={t('searching')} size="sm" /> : t('findCandidates')}
            </button>
            <label className="flex items-center gap-1.5 text-xs text-ink-400">
              <input
                type="checkbox"
                checked={weak}
                onChange={(e) => { setWeak(e.target.checked); }}
                className="accent-ink-500"
              />
              {t('weakSignals')}
            </label>
          </div>

          {sug && (
            <>
              <p className="mt-2 text-xs text-ink-400">
                {t('sugPre')}<code className="text-ink-200">cue</code>{t('sugMid')}
                <strong>{t('sugBold')}</strong>{t('sugPost')}
              </p>
              <pre className="mt-2 max-h-72 overflow-auto rounded-card border border-ink-800
                              bg-ink-950 p-3 font-mono text-2xs leading-relaxed
                              text-ink-300">
                {sug}
              </pre>
              <button
                type="button"
                onClick={() => {
                  const cur = getValues('content').replace(/\s*$/, '');
                  // It has to be `setValue(…, { shouldDirty: true })` (the same as AliasPanel).
                  // It used to be `reset(…, { keepDirty: true })`, and keepDirty only preserves an
                  // **existing** dirty state rather than setting one —— appending 5KB to a clean
                  // form left it clean and the save button disabled.  Pasting candidates is this
                  // panel's whole job, and there was no way to save it.
                  setValue('content', `${cur}\n\n${sug}\n`, { shouldDirty: true });
                }}
                className="mt-2 rounded-card border border-ink-700 px-3 py-1.5 text-xs
                           hover:bg-ink-800"
              >
                {t('appendToEditor')}
              </button>
            </>
          )}
        </div>
      </div>
    </section>
  );
}
