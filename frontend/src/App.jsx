import { useEffect, useRef, useState } from "react";
import { createRun, streamRun } from "./api.js";

const EXAMPLES = [
  "4 days in Lisbon in November, $1,200 budget, love food and old neighbourhoods",
  "Weekend in Montreal for two, $800 total, live music and bagels",
  "5 days in Tokyo on $2,000, first time, mix of temples and arcades",
];

const TOOL_LABELS = {
  get_weather: "Checking the weather",
  convert_currency: "Converting currency",
  calculate_budget: "Adding up the budget",
  submit_itinerary: "Submitting the plan",
};

function money(amount, currency) {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency, maximumFractionDigits: 0 }).format(amount);
  } catch {
    return `${Math.round(amount)} ${currency}`;
  }
}

function stepFromEvent(type, data) {
  switch (type) {
    case "status":
      return data.status === "queued" ? { kind: "info", text: "Waiting for a free worker" } : { kind: "info", text: "Planning started" };
    case "thought":
      return { kind: "thought", text: data.text };
    case "tool_call":
      return { kind: "tool", id: data.id, text: TOOL_LABELS[data.name] || data.name, detail: data.input ? JSON.stringify(data.input) : null, state: "running" };
    case "validation_error":
      return { kind: "warn", text: "Plan rejected by the checker, fixing it", detail: data.message };
    default:
      return null;
  }
}

export default function App() {
  const [prompt, setPrompt] = useState("");
  const [run, setRun] = useState(null); // { id, status }
  const [steps, setSteps] = useState([]);
  const [itinerary, setItinerary] = useState(null);
  const [usage, setUsage] = useState(null);
  const [error, setError] = useState(null);
  const stopStream = useRef(null);
  const pendingKey = useRef(null);

  useEffect(() => () => stopStream.current?.(), []);

  const busy = run && (run.status === "queued" || run.status === "running");

  async function handleSubmit(e) {
    e.preventDefault();
    if (busy) return;
    setError(null);
    setItinerary(null);
    setUsage(null);
    setSteps([]);
    // Same key for retries of this submission, so a double-click never creates two runs.
    pendingKey.current ??= crypto.randomUUID();
    try {
      const created = await createRun(prompt.trim(), pendingKey.current);
      pendingKey.current = null;
      setRun({ id: created.id, status: created.status });
      stopStream.current?.();
      stopStream.current = streamRun(created.id, handleEvent);
    } catch (err) {
      setError(err.message);
    }
  }

  function handleEvent(type, data) {
    if (type === "status") setRun((r) => ({ ...r, status: data.status }));
    if (type === "tool_result") {
      setSteps((prev) => prev.map((s) => (s.id === data.id ? { ...s, state: data.ok ? "ok" : "failed" } : s)));
      return;
    }
    if (type === "done") {
      setItinerary(data.itinerary);
      setUsage(data.usage ?? null);
      setRun((r) => ({ ...r, status: "completed" }));
      return;
    }
    if (type === "error") {
      setError(data.message);
      setUsage(data.usage ?? null);
      setRun((r) => ({ ...r, status: "failed" }));
      return;
    }
    const step = stepFromEvent(type, data);
    if (step) setSteps((prev) => [...prev, step]);
  }

  return (
    <div className="page">
      <header className="masthead">
        <h1>Trip planner</h1>
        <p>Describe the trip. An agent checks the weather, does the budget maths, and hands back a plan that fits.</p>
      </header>

      <main className="layout">
        <section className="panel" aria-label="Plan a trip">
          <form onSubmit={handleSubmit} className="ask">
            <label htmlFor="prompt">Where, how long, and how much?</label>
            <textarea
              id="prompt"
              rows={4}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder={EXAMPLES[0]}
              minLength={10}
              maxLength={2000}
              required
            />
            <div className="examples">
              {EXAMPLES.slice(1).map((ex) => (
                <button type="button" key={ex} className="example" onClick={() => setPrompt(ex)}>
                  {ex.split(",")[0]}
                </button>
              ))}
            </div>
            <button type="submit" className="go" disabled={busy || prompt.trim().length < 10}>
              {busy ? "Planning…" : "Plan my trip"}
            </button>
          </form>

          {(steps.length > 0 || busy) && (
            <div className="log" aria-live="polite">
              <h2>What the agent is doing</h2>
              <ol>
                {steps.map((s, i) => (
                  <li key={i} className={`step step-${s.kind} ${s.state ? `is-${s.state}` : ""}`}>
                    <span className="step-text">{s.text}</span>
                    {s.detail && <span className="step-detail">{s.detail}</span>}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {usage && (
            <p className="usage">
              {usage.turns} model calls, {(usage.input_tokens + usage.output_tokens).toLocaleString()} tokens
              {usage.cost_usd > 0 && <>, about ${usage.cost_usd.toFixed(3)}</>}
            </p>
          )}
        </section>

        <section className="result" aria-label="Itinerary">
          {error && (
            <div className="error" role="alert">
              <strong>The plan didn't finish.</strong> {error}
            </div>
          )}
          {!itinerary && !error && (
            <div className="empty">
              <p>{busy ? "Your route will appear here as soon as the plan passes its checks." : "Your route will appear here."}</p>
            </div>
          )}
          {itinerary && <Itinerary plan={itinerary} />}
        </section>
      </main>
    </div>
  );
}

function Itinerary({ plan }) {
  const cur = plan.currency;
  const used = Math.min(100, (plan.estimated_total / plan.total_budget) * 100);
  return (
    <article className="itinerary">
      <header className="trip-head">
        <h2>{plan.destination}</h2>
        <p className="weather">{plan.weather_note}</p>
        <div className="budget">
          <div className="budget-line">
            <span>{money(plan.estimated_total, cur)} planned</span>
            <span>{money(plan.total_budget - plan.estimated_total, cur)} to spare</span>
          </div>
          <div className="bar" role="img" aria-label={`${Math.round(used)}% of budget used`}>
            <div className="bar-fill" style={{ width: `${used}%` }} />
          </div>
          <dl className="breakdown">
            {Object.entries(plan.budget_breakdown).map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{money(v, cur)}</dd>
              </div>
            ))}
          </dl>
        </div>
      </header>

      <ol className="route">
        {plan.days.map((day) => (
          <li key={day.day} className="day">
            <h3>
              <span className="day-num">Day {day.day}</span> {day.title}
            </h3>
            <ol className="stops">
              {day.stops.map((stop, i) => (
                <li key={i} className="stop">
                  <div className="stop-head">
                    <span className="stop-name">{stop.name}</span>
                    <span className="stop-cost">{stop.estimated_cost > 0 ? money(stop.estimated_cost, cur) : "Free"}</span>
                  </div>
                  {stop.time && <span className="stop-time">{stop.time}</span>}
                  <p>{stop.description}</p>
                </li>
              ))}
            </ol>
          </li>
        ))}
      </ol>

      {plan.tips?.length > 0 && (
        <aside className="tips">
          <h3>Good to know</h3>
          <ul>
            {plan.tips.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </aside>
      )}
    </article>
  );
}
