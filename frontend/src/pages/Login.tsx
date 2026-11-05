import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "@/lib/auth";

export function Login() {
  const { isAuthenticated, loginWithToken, loginLocal } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [error, setError] = useState("");
  const [loggingIn, setLoggingIn] = useState(false);
  const attempted = useRef(false);
  // Set once a login attempt comes back asking for a second factor. The
  // one-time Telegram token has to be remembered to retry it with the code
  // attached; local mode has no such token to carry — it just resubmits.
  const [otpRequired, setOtpRequired] = useState(false);
  const [otp, setOtp] = useState("");
  const pendingLoginToken = useRef<string | null>(null);
  // How this install logs in — asked once, before anything is rendered, so the
  // "run /web in Telegram" card never flashes on a machine that has no Telegram.
  // `"unreachable"` is not a mode the server can report: it is what we know when
  // the probe never got an answer, and the only honest thing to render then.
  const [mode, setMode] = useState<string | null>(null);
  const probed = useRef(false);
  // Bumped by the Retry button on the unreachable card. The probe effect latches
  // on `probed`, so re-running it takes clearing that ref and a new dep value.
  const [probeAttempt, setProbeAttempt] = useState(0);

  // Where to land after login. Only allow internal paths to avoid open redirects.
  const rawRedirect = searchParams.get("redirect");
  const redirectTo =
    rawRedirect && rawRedirect.startsWith("/") && !rawRedirect.startsWith("//")
      ? rawRedirect
      : "/";

  useEffect(() => {
    if (isAuthenticated) {
      navigate(redirectTo, { replace: true });
    }
  }, [isAuthenticated, navigate, redirectTo]);

  // Auto-login when ?token= is present in URL
  useEffect(() => {
    const token = searchParams.get("token");
    if (!token || attempted.current) return;
    attempted.current = true;
    setLoggingIn(true);

    // Strip the one-time token from the URL so it does not linger in browser
    // history or get leaked via the Referer header. The token is consumed via
    // a POST below; the address bar should not keep it.
    window.history.replaceState(null, "", window.location.pathname);

    pendingLoginToken.current = token;
    loginWithToken(token)
      .then(() => navigate(redirectTo, { replace: true }))
      .catch((err: { code?: string; message?: string }) => {
        if (err?.code === "2fa_required" || err?.code === "2fa_invalid") {
          setOtpRequired(true);
          setError(err.code === "2fa_invalid" ? err.message ?? "" : "");
          setLoggingIn(false);
          return;
        }
        setError(err instanceof Error ? err.message : "Login failed");
        setLoggingIn(false);
      });
  }, [searchParams, loginWithToken, navigate, redirectTo]);

  // The one local-login attempt, shared by the probe below, the Retry button
  // and the 2FA form. Retrying calls this directly rather than re-firing the
  // effect: the `probed` ref has already latched, and the mode cannot have
  // changed without a server restart, so a second /auth/mode round trip
  // would buy nothing.
  const attemptLocalLogin = useCallback(
    (otpCode?: string) => {
      setError("");
      setLoggingIn(true);
      return loginLocal(otpCode)
        .then(() => navigate(redirectTo, { replace: true }))
        .catch((err: { code?: string; message?: string }) => {
          if (err?.code === "2fa_required" || err?.code === "2fa_invalid") {
            setOtpRequired(true);
            setError(err.code === "2fa_invalid" ? err.message ?? "" : "");
            setLoggingIn(false);
            return;
          }
          setError(err instanceof Error ? err.message : "Login failed");
          setLoggingIn(false);
        });
    },
    [loginLocal, navigate, redirectTo],
  );

  const submitOtp = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      const code = otp.trim();
      if (!code) return;
      if (pendingLoginToken.current) {
        setError("");
        setLoggingIn(true);
        loginWithToken(pendingLoginToken.current, code)
          .then(() => navigate(redirectTo, { replace: true }))
          .catch((err: { code?: string; message?: string }) => {
            setError(err instanceof Error ? err.message : "Invalid code");
            setLoggingIn(false);
            setOtp("");
          });
        return;
      }
      void attemptLocalLogin(code).then(() => setOtp(""));
    },
    [otp, loginWithToken, attemptLocalLogin, navigate, redirectTo],
  );

  // Local mode (FEAT-049): there is no login step. Ask the server how it
  // authenticates and, if the answer is "locally", claim the session and go.
  // Skipped entirely when a one-time ?token= is being redeemed above.
  useEffect(() => {
    if (searchParams.get("token") || probed.current) return;
    probed.current = true;

    let cancelled = false;
    fetch("/api/v1/auth/mode")
      .then((res) => (res.ok ? res.json() : { mode: "telegram" }))
      .then((data) => {
        if (cancelled) return;
        setMode(data?.mode ?? "telegram");
        if (data?.mode !== "local") return;
        return attemptLocalLogin();
      })
      .catch((err) => {
        // Nothing answered, so nothing here knows the mode — least of all this
        // catch. Guessing "telegram" printed "run /web in your bot" on machines
        // with no bot to run it in; say what actually happened instead.
        if (cancelled) return;
        setMode("unreachable");
        setError(err instanceof Error ? err.message : "Could not reach Griffin");
      });

    return () => {
      cancelled = true;
    };
  }, [searchParams, attemptLocalLogin, probeAttempt]);

  // Retry on the unreachable card re-asks /auth/mode. Unlike the local branch's
  // Retry, this one cannot go straight to a login: which login to attempt is
  // exactly the thing the failed probe was going to tell us.
  const retryProbe = useCallback(() => {
    setError("");
    setMode(null);
    probed.current = false;
    setProbeAttempt((n) => n + 1);
  }, []);

  return (
    <div className="flex h-[var(--app-viewport-height)] items-center justify-center">
      <div className="w-full max-w-sm rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-8 text-center">
        <div className="mx-auto mb-4 h-16 w-16">
          <img src="/griffin-logo.png" alt="Griffin" className="home-logo" />
        </div>
        <h1 className="mb-2 text-2xl font-bold">Griffin</h1>
        {otpRequired ? (
          <form onSubmit={submitOtp}>
            <p className="mb-4 text-sm text-[var(--color-text-muted)]">
              Enter the 6-digit code from your authenticator app, or one of
              your recovery codes.
            </p>
            <input
              type="text"
              inputMode="numeric"
              autoFocus
              autoComplete="one-time-code"
              aria-label="Two-factor authentication code"
              value={otp}
              onChange={(e) => setOtp(e.target.value)}
              placeholder="000000"
              disabled={loggingIn}
              className="mb-4 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-2 text-center text-lg tracking-widest"
            />
            <button
              type="submit"
              disabled={loggingIn || !otp.trim()}
              className="w-full rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {loggingIn ? "Verifying..." : "Verify"}
            </button>
          </form>
        ) : loggingIn || (mode === null && !searchParams.get("token")) ? (
          <p className="text-sm text-[var(--color-text-muted)]">
            Signing in...
          </p>
        ) : mode === "local" ? (
          // Local mode has no Telegram bot to run /web in, and the sign-in it
          // does have just failed. Two things can cause that — the server went
          // away between the probe and the POST, or the user it logs in as is
          // no longer approved — so name both rather than assert either; when
          // the server did answer, its own detail lands below in red.
          <>
            <p className="mb-4 text-sm text-[var(--color-text-muted)]">
              This install runs in local mode, so there is no login link — the dashboard signs you in by itself. That did not work this time.
            </p>
            <p className="mb-6 text-sm text-[var(--color-text-muted)]">
              Check that Griffin is still running, and that <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">ADMIN_USER_ID</code> in <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">.env</code> is an approved user in <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">config.yml</code> — or run <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">make setup</code> and choose Local mode.
            </p>
            <button
              type="button"
              onClick={() => void attemptLocalLogin()}
              className="rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
            >
              Retry
            </button>
          </>
        ) : mode === "unreachable" ? (
          // The probe got no answer at all, so neither card above applies: the
          // one thing known is that the server is not there. Name that, and keep
          // the advice true in both modes — the fetch's own detail lands below
          // in red.
          <>
            <p className="mb-4 text-sm text-[var(--color-text-muted)]">
              The dashboard could not reach Griffin, so it cannot tell how this install signs you in.
            </p>
            <p className="mb-6 text-sm text-[var(--color-text-muted)]">
              Check that Griffin is running — <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">make status</code> shows it, <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">make run</code> starts it — then retry.
            </p>
            <button
              type="button"
              onClick={retryProbe}
              className="rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90"
            >
              Retry
            </button>
          </>
        ) : (
          <>
            <p className="mb-6 text-sm text-[var(--color-text-muted)]">
              Run the <code className="rounded bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs">/web</code> command in your Telegram bot to get a login link.
            </p>
          </>
        )}
        {error && (
          <p className="mt-4 text-sm text-[var(--color-red)]">{error}</p>
        )}
      </div>
    </div>
  );
}
