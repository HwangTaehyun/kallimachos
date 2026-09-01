/**
 * Shows an error **so it can be read**.
 *
 * It used to dump the server's string as one block.  This is what that actually looked like:
 *
 *   Could not read the status: thread 'lancedb-tokio-worker' (19522) panicked at
 *
 * Twelve lines of a Rust panic + a Python traceback covered the top of the screen, while
 * **what to do about it** (the disk is full) was buried somewhere inside as a single word.
 *
 * So it is split into three layers:
 *   ① What happened —— one sentence.
 *   ② What to do about it —— only when known, down to the command.
 *   ③ The source text —— folded away.  Only whoever needs it unfolds it.
 *
 * The source text is **not discarded.**  When the diagnosis is wrong it is the only clue.
 */
import { useT } from '../i18n/lang';
import S from '../i18n/strings/errorBanner';

type Dx = { what: string; fix?: React.ReactNode };

/** Turns a `fix` dictionary value (plain text with backtick-wrapped commands) into JSX wrapped in <Cmd>. */
function renderFix(s: string): React.ReactNode {
  return s.split('`').map((part, i) => (i % 2 ? <Cmd key={i}>{part}</Cmd> : part));
}

/** Only known failures are turned into human words.  Unknown ones quietly lead with the source text ——
 *  a wrong diagnosis is worse than none. */
function diagnose(text: string, t: (k: keyof typeof S.en) => string): Dx | null {
  const lower = text.toLowerCase();

  if (lower.includes('no space left') || lower.includes('storagefull')) {
    return { what: t('diskFullWhat'), fix: renderFix(t('diskFullFix')) };
  }
  if (lower.includes('not logged in') || lower.includes('credit balance')) {
    return { what: t('claudeUnavailableWhat'), fix: renderFix(t('claudeUnavailableFix')) };
  }
  if (lower.includes('failed to fetch') || lower.includes('networkerror') || lower.includes('load failed')) {
    return { what: t('apiUnreachableWhat'), fix: renderFix(t('apiUnreachableFix')) };
  }
  return null;
}

export function ErrorBanner({ text, onClose }: { text: string; onClose?: () => void }) {
  const t = useT(S);
  const dx = diagnose(text, t);

  return (
    <div
      role="alert"
      className="mb-4 rounded-card border border-red-500/40 bg-red-950/40 px-4 py-3 text-sm text-red-100"
    >
      <div className="flex items-start gap-3">
        <span aria-hidden className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-crit" />
        <div className="min-w-0 flex-1">
          {/* With a diagnosis it is the title; without one, the source text's first line is */}
          <p className="font-medium leading-relaxed">
            {dx ? dx.what : firstLine(text)}
          </p>
          {dx?.fix && <p className="mt-1.5 text-sm leading-relaxed text-crit/90">{dx.fix}</p>}

          {/* The source text.  With a diagnosis, all of it; without one, the rest after the first line, folded. */}
          {(dx || rest(text)) && (
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-crit/80 hover:text-crit">
                {t('viewRawOutput')}
              </summary>
              <pre className="mt-1.5 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded
                              border border-red-500/20 bg-black/30 p-2 font-mono text-2xs
                              leading-relaxed text-crit/80">
                {text}
              </pre>
            </details>
          )}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="shrink-0 rounded px-2 py-0.5 text-xs text-crit underline-offset-2
                       hover:text-red-100 hover:underline"
          >
            {t('dismiss')}
          </button>
        )}
      </div>
    </div>
  );
}

/** The first sentence.  A traceback usually says what happened on its first line.  Too long and it is cut. */
function firstLine(s: string): string {
  const l = s.split('\n')[0]!.trim();
  return l.length > 200 ? `${l.slice(0, 200)}…` : l;
}

function rest(s: string): boolean {
  return s.includes('\n') || s.length > 200;
}

function Cmd({ children }: { children: React.ReactNode }) {
  return <code className="rounded bg-black/40 px-1 py-0.5 font-mono text-xs">{children}</code>;
}
