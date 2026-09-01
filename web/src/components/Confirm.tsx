import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '../i18n/lang';
import S from '../i18n/strings/confirm';

type Req = {
  title: string;
  body: string;
  cta: string;
  onOk: () => void;
};

/** The confirmation put in front of an action that cannot be undone.
 *
 * It uses `<dialog>` + `showModal()`.  The focus trap, Esc to close and the inert background come
 * from the browser —— built as a div overlay all three have to be hand-written, and usually one is
 * missing.  The plugin's modal moved to `<dialog>` for the same reason.
 *
 * Focus goes to **cancel**, not to confirm.  Focused on the destructive side, one Enter just runs
 * it —— which defeats the point of putting up a confirmation.
 */
function ConfirmDialog({ req, onClose }: { req: Req; onClose: () => void }) {
  const t = useT(S);
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    ref.current?.showModal();
    cancelRef.current?.focus();
  }, []);

  return (
    <dialog
      ref={ref}
      aria-labelledby="confirm-title"
      onClose={onClose}
      onCancel={onClose}                 // Esc
      className="m-auto max-w-lg rounded-card border border-ink-700 bg-ink-900 p-0
                 text-ink-100 backdrop:bg-black/60 backdrop:backdrop-blur-sm"
    >
      <div className="space-y-3 p-5">
        <h2 id="confirm-title" className="text-sm font-semibold">{req.title}</h2>
        <p className="text-xs leading-relaxed text-ink-300">{req.body}</p>
        <div className="flex justify-end gap-2 pt-1">
          <button
            ref={cancelRef}
            type="button"
            onClick={() => ref.current?.close()}
            className="rounded-card border border-ink-700 px-3 py-1.5 text-xs hover:bg-ink-800"
          >
            {t('cancel')}
          </button>
          <button
            type="button"
            onClick={() => { req.onOk(); ref.current?.close(); }}
            className="rounded-card border border-red-500/50 bg-red-950/50 px-3 py-1.5
                       text-xs text-red-100 hover:bg-red-900/60"
          >
            {req.cta}
          </button>
        </div>
      </div>
    </dialog>
  );
}

/** Gives both the function that opens a confirmation and the node to plug into the screen.
 *
 *     confirm({ title: '…', body: '…', cta: 'Run', onOk: () => start(id) });
 *     return <>{confirmUi}…</>;
 */
export function useConfirm() {
  const [req, setReq] = useState<Req | null>(null);
  const ask = useCallback((r: Req) => setReq(r), []);
  const ui = req ? <ConfirmDialog req={req} onClose={() => setReq(null)} /> : null;
  return [ask, ui] as const;
}
