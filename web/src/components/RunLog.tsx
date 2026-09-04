import { useEffect, useRef, useState } from 'react';
import type { Run } from '../api';
import { useLogStream } from '../useLogStream';
import { ago } from './StatusPanel';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/runLog';

export function RunLog({ runId, runs, onCancel, onFinished }: {
  runId: string | null;
  runs: Run[];
  onCancel: (id: string) => void;
  onFinished: () => void;
}) {
  const t = useT(S);
  const lines = useLogStream(runId, onFinished);
  const [stick, setStick] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);
  const run = runs.find((r) => r.id === runId);

  // A new line sticks to the bottom and follows.  Unless the user has scrolled up to read ——
  // nothing is more annoying than being dragged down while looking back through a log.
  useEffect(() => {
    if (!stick || !boxRef.current) return;
    boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines, stick]);

  return (
    //  ⚠ `min-h-[24rem]` applied **even when empty**.  With nothing running, an empty box took half
    //    the screen and the pipeline below it needed scrolling to reach.  The reason for opening
    //    this screen is the pipeline, not the log.
    //    It grows only when there is content.
    <section className={`flex flex-col rounded-card bg-ink-900/50 ring-1 ring-ink-850
                         lg:sticky lg:top-20 lg:max-h-[calc(100vh-7rem)] ${
      lines.length > 0 || runId ? 'min-h-[24rem]' : ''}`}
      aria-labelledby="log-h">
      <div className="flex flex-wrap items-center gap-2 border-b border-ink-800 px-4 py-3">
        <h2 id="log-h" className="text-sm font-semibold">{t('title')}</h2>
        {run ? (
          <span className="text-xs text-ink-400">
            {run.step} · {run.id} · {t('startedAt')(ago(run.started_at))}
            {run.status !== 'running' && ` · ${run.status}`}
            {run.exit_code != null && run.exit_code !== 0 && ` (exit ${run.exit_code})`}
          </span>
        ) : (
          <span className="text-xs text-ink-400">{t('idle')}</span>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-ink-400">
            <input
              type="checkbox"
              checked={stick}
              onChange={(e) => setStick(e.target.checked)}
              className="accent-amber-glow"
            />
            {t('stick')}
          </label>
          {run?.status === 'running' && (
            <button
              type="button"
              onClick={() => onCancel(run.id)}
              className="rounded-control border border-red-500/40 px-2.5 py-1 text-xs text-crit hover:bg-red-950/40"
            >
              {t('stop')}
            </button>
          )}
        </div>
      </div>

      <div
        ref={boxRef}
        // Dropping away from the bottom turns auto-follow off (touching the bottom again turns it back on).
        onScroll={(e) => {
          const el = e.currentTarget;
          setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 24);
        }}
        className="flex-1 overflow-auto px-4 py-3 font-mono text-xs leading-relaxed"
        role="log"
        aria-live="polite"
        aria-atomic="false"
      >
        {lines.length === 0 ? (
          <p className="text-ink-400 font-sans">
            {runId ? t('waiting') : t('empty')}
          </p>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`whitespace-pre-wrap break-words ${lineClass(l)}`}>{l}</div>
          ))
        )}
      </div>
    </section>
  );
}

/** Colours only the lines that should catch the eye in a log.  Colour everything and nothing shows. */
function lineClass(l: string): string {
  //  ⚠ These match **the pipeline's own output**, which went English —— the Korean words
  //     ('실패' · '경고' · '완료' · '통과') had stopped matching anything.  The symbols carried it, so
  //     nothing looked broken; a failure line simply lost its colour when the word was all it had.
  if (/(❌|failed|Error|Traceback|error:)/i.test(l)) return 'text-crit';
  if (/(⚠|warn)/i.test(l)) return 'text-warn';
  if (/(✅|done|passed|complete)/i.test(l)) return 'text-ok';
  if (/^\s*[│├└─]/.test(l) || /^══/.test(l)) return 'text-live';
  return 'text-ink-400';
}
