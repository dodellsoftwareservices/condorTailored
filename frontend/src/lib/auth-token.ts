// A browser session now rides on the HttpOnly `condor_session` cookie (set by
// `token-login`/`local-login`), which `fetch` attaches to same-origin requests
// on its own — there is no token for this module to read or inject. It stays
// as the shared low-level fetch anyway: FormData uploads (transcribe) and blob
// responses (authenticated images) go through `authFetch` for the same reason
// JSON calls go through `apiFetch` (lib/api.ts) — one place for the next auth
// transport change to land, instead of every call site.

export function authFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(path, init);
}
