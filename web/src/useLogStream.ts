import { useEffect, useRef, useState } from 'react';
import { streamLog } from './api';

/** Receives the run log over SSE and gives it back as an array of lines.
 *
 * It lived inside App.tsx, and RunLog importing it from there made a cycle.  It works today
 * (a function declaration is hoisted), but the moment it becomes `const useLogStream = () =>`
 * the TDZ makes it undefined at runtime.  Separated ahead of time.
 */
export function useLogStream(runId: string | null, onDone: () => void) {
  const [lines, setLines] = useState<string[]>([]);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    if (!runId) { setLines([]); return; }
    setLines([]);
    const stop = streamLog(
      runId,
      (l) => setLines((prev) => {
        // A run lasting hours produces tens of thousands of log lines.  Keeping them all in the DOM eats the browser.
        const next = prev.length > 4000 ? prev.slice(-3000) : prev.slice();
        next.push(l);
        return next;
      }),
      () => doneRef.current(),
    );
    return stop;
  }, [runId]);

  return lines;
}
