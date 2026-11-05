/**
 * The dashboard's optional second login factor (TOTP). Kept out of
 * `lib/api.ts` for the same reason `lib/admin-api.ts` is: a narrow,
 * security-sensitive surface with its own small contract, not another
 * half-dozen functions in the shared client.
 */

export interface TwoFactorStatus {
  enabled: boolean;
}

export interface TwoFactorEnrollment {
  secret: string;
  otp_uri: string;
  recovery_codes: string[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers as Record<string, string>),
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail: unknown = err.detail;
    const detailObject =
      detail && typeof detail === "object" ? (detail as Record<string, unknown>) : null;
    const message = detailObject ? detailObject.message : detail;
    throw new Error(
      (typeof message === "string" && message) || `Request failed: ${res.status}`,
    );
  }
  return res.json();
}

export function getTwoFactorStatus(): Promise<TwoFactorStatus> {
  return request("/api/v1/auth/2fa/status");
}

/**
 * Starts (or restarts) enrollment. Not yet active — see `confirmTwoFactor`.
 * `code` is required to re-enroll over an already-active factor — the
 * server proves possession of the current code first, the same way
 * `disableTwoFactor` does.
 */
export function enrollTwoFactor(code?: string): Promise<TwoFactorEnrollment> {
  return request("/api/v1/auth/2fa/enroll", {
    method: "POST",
    body: JSON.stringify({ code: code ?? null }),
  });
}

/** The first real code from the authenticator app turns 2FA on. */
export function confirmTwoFactor(code: string): Promise<TwoFactorStatus> {
  return request("/api/v1/auth/2fa/confirm", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

/** Requires a current code or an unused recovery code — proof of possession. */
export function disableTwoFactor(code: string): Promise<TwoFactorStatus> {
  return request("/api/v1/auth/2fa/disable", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}
