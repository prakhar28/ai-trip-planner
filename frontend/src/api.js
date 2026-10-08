const BASE = "/api";

export async function createRun(prompt, idempotencyKey) {
  const res = await fetch(`${BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = Array.isArray(body.detail) ? body.detail[0]?.msg : body.detail;
    throw new Error(detail || `Request failed with status ${res.status}`);
  }
  return res.json();
}

const EVENT_TYPES = ["status", "turn", "thought", "tool_call", "tool_result", "validation_error", "done", "error"];

// EventSource reconnects on its own and resends Last-Event-ID; the API resumes from there.
export function streamRun(runId, onEvent) {
  const source = new EventSource(`${BASE}/runs/${runId}/stream`);
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (e) => {
      onEvent(type, JSON.parse(e.data));
      if (type === "done" || type === "error") source.close();
    });
  }
  return () => source.close();
}
