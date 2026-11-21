export const API_URL = (import.meta.env.VITE_API_URL || "http://localhost:8000").replace(/\/+$/, "");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Poll GET /health until it answers 200 (free hosts sleep). Calls onSlow() if it takes > 1.5 s. */
export async function waitForHealth(onSlow, attempts = 12) {
  const slowTimer = setTimeout(onSlow, 1500);
  try {
    for (let i = 0; i < attempts; i++) {
      try {
        const res = await fetch(`${API_URL}/health`, { signal: AbortSignal.timeout(20000) });
        if (res.ok) return true;
      } catch {
        // network error / timeout while the server boots: retry
      }
      await sleep(Math.min(2000 * (i + 1), 8000));
    }
    return false;
  } finally {
    clearTimeout(slowTimer);
  }
}

function describeError(status, data) {
  const detail = data?.detail;
  const text = Array.isArray(detail)
    ? detail.map((e) => `${e.loc?.at(-1) ?? "request"}: ${e.msg}`).join("; ")
    : detail || "unexpected error";
  return `Error ${status}: ${text}`;
}

/** POST /chat. Retries (2x, backoff) ONLY on network errors and 502/504; never on 422/503. */
export async function sendChat(body, simulateFailure) {
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure) headers["X-Simulate-Failure"] = simulateFailure;
  for (let attempt = 0; ; attempt++) {
    const canRetry = attempt < 2;
    let res;
    try {
      res = await fetch(`${API_URL}/chat`, { method: "POST", headers, body: JSON.stringify(body) });
    } catch {
      if (canRetry) {
        await sleep(1000 * 2 ** attempt);
        continue;
      }
      throw new Error("Network error: could not reach the server. Please check your connection and try again.");
    }
    if ((res.status === 502 || res.status === 504) && canRetry) {
      await sleep(1000 * 2 ** attempt);
      continue;
    }
    const data = await res.json().catch(() => null);
    if (res.ok && data) return data;
    throw new Error(describeError(res.status, data));
  }
}
