import { useEffect, useRef, useState } from "react";
import type { BreakerStatus } from "../api/types";
import { STATE_LABELS, controlNameFor } from "../utils/breaker";

export interface Transition {
  id: number;
  at: number;
  upstream: string;
  from: string;
  to: string;
  /** Set when an operator caused it from this page. */
  manual?: "trip" | "reset";
}

/**
 * Record every state change seen between polls. The gateway has no event
 * stream for this, but polling once a second catches each transition, since
 * the shortest a state lasts is a single trial request.
 */
export function useTransitions(breakers: BreakerStatus[]) {
  const previous = useRef<Record<string, string>>({});
  const pendingManual = useRef<Record<string, "trip" | "reset">>({});
  const nextId = useRef(1);
  const [transitions, setTransitions] = useState<Transition[]>([]);

  useEffect(() => {
    const seen: Transition[] = [];
    for (const breaker of breakers) {
      const before = previous.current[breaker.upstream];
      if (before !== undefined && before !== breaker.state) {
        seen.push({
          id: nextId.current++,
          at: Date.now(),
          upstream: breaker.upstream,
          from: before,
          to: breaker.state,
          manual: pendingManual.current[breaker.upstream],
        });
        delete pendingManual.current[breaker.upstream];
      }
      previous.current[breaker.upstream] = breaker.state;
    }
    if (seen.length > 0) setTransitions((current) => [...seen, ...current].slice(0, 50));
  }, [breakers]);

  const markManual = (upstream: string, action: "trip" | "reset") => {
    pendingManual.current[upstream] = action;
  };

  return { transitions, markManual };
}

interface Step {
  title: string;
  how: string;
  done: boolean;
}

function automaticSeen(transitions: Transition[], upstream: string, from: string, to: string) {
  return transitions.some(
    (transition) =>
      transition.upstream === upstream && transition.from === from && transition.to === to && !transition.manual,
  );
}

export function BreakerWalkthrough({
  breakers,
  transitions,
}: {
  breakers: BreakerStatus[];
  transitions: Transition[];
}) {
  // Walk through the first upstream; service-a backs /api/orders.
  const focus = breakers[0];
  if (!focus) return null;
  const name = controlNameFor(focus.upstream) ?? focus.upstream;

  const steps: Step[] = [
    {
      title: `Make ${name} fail and send traffic`,
      how: `Press "Make it fail" on the ${name} card, then start traffic to its route above. Each 503 from the upstream adds to the failure count.`,
      done:
        focus.failures > 0 ||
        focus.state !== "closed" ||
        transitions.some((transition) => transition.upstream === focus.upstream),
    },
    {
      title: "Watch it open",
      how: `After ${focus.failure_threshold} failures in a row it opens. The traffic strip switches to hollow refusals, answered by the gateway in a few milliseconds.`,
      done: automaticSeen(transitions, focus.upstream, "closed", "open"),
    },
    {
      title: "Heal it and wait for half open",
      how: `Press "Heal upstream". The breaker does not know yet; after ${focus.recovery_timeout_seconds}s it moves to half open and lets one trial request through.`,
      done: automaticSeen(transitions, focus.upstream, "open", "half_open"),
    },
    {
      title: "Two successful trials close it",
      how: "With traffic still running, the trials succeed and the breaker closes on its own. If a trial had failed, it would reopen and restart the timer.",
      done: automaticSeen(transitions, focus.upstream, "half_open", "closed"),
    },
  ];
  const current = steps.findIndex((step) => !step.done);

  return (
    <section className="card">
      <div className="card-header">
        <h2>Walkthrough</h2>
        <span>{current === -1 ? "complete, the breaker went all the way round" : `step ${current + 1} of ${steps.length}`}</span>
      </div>
      <ol className="walkthrough">
        {steps.map((step, index) => (
          <li key={step.title} className={step.done ? "done" : index === current ? "current" : ""}>
            <span className="walk-marker" aria-hidden="true">
              {step.done ? "✓" : index + 1}
            </span>
            <div>
              <strong>{step.title}</strong>
              {step.done && <span className="sr-only"> (done)</span>}
              <div className="hint">{step.how}</div>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function TransitionLog({ transitions }: { transitions: Transition[] }) {
  return (
    <section className="card">
      <div className="card-header">
        <h2>Transitions seen</h2>
        <span>this page, newest first</span>
      </div>
      {transitions.length === 0 ? (
        <div className="hint">No state changes yet. They appear here as the breakers move.</div>
      ) : (
        <ol className="transition-log">
          {transitions.map((transition) => (
            <li key={transition.id}>
              <span className="mono hint">{new Date(transition.at).toLocaleTimeString()}</span>
              <strong>{transition.upstream}</strong>
              <span>
                {STATE_LABELS[transition.from] ?? transition.from} to{" "}
                {STATE_LABELS[transition.to] ?? transition.to}
              </span>
              <span className="hint">
                {transition.manual ? `operator ${transition.manual}` : "automatic"}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
