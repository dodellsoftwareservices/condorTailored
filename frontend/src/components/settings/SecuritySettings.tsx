import { Check, Copy, KeyRound, Loader2, ShieldCheck, ShieldOff } from "lucide-react";
import { useState } from "react";

import {
  useConfirmTwoFactor,
  useDisableTwoFactor,
  useEnrollTwoFactor,
  useTwoFactorStatus,
} from "@/hooks/useTwoFactor";

/**
 * Two-factor login (TOTP). Gates the login endpoints only (`token-login`,
 * `local-login`) — once signed in, the session cookie carries the day, so
 * this is checked once per login, not on every request.
 *
 * No QR code here: rendering one needs either a server-side image (another
 * dependency on the Python side) or a client-side QR library (another
 * dependency here), and every authenticator app accepts the secret typed in
 * by hand just as well. The `otp_uri` is offered as a fallback for apps that
 * can import a link directly.
 */
export function SecuritySettings() {
  const { data, isLoading } = useTwoFactorStatus();
  const enroll = useEnrollTwoFactor();
  const confirm = useConfirmTwoFactor();
  const disable = useDisableTwoFactor();

  const [confirmCode, setConfirmCode] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [copied, setCopied] = useState<"secret" | "codes" | null>(null);
  const [copyFailed, setCopyFailed] = useState<"secret" | "codes" | null>(null);
  const [codesSaved, setCodesSaved] = useState(false);

  if (isLoading || !data) {
    return (
      <div className="flex items-center justify-center py-12 text-[var(--color-text-muted)]">
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    );
  }

  const copy = async (text: string, which: "secret" | "codes") => {
    setCopyFailed(null);
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // navigator.clipboard is undefined on plain HTTP served to anything
        // other than localhost — exactly the Tailscale/remote-VPS topology
        // this app explicitly supports. Without this fallback the button
        // silently did nothing, with no way to tell "copied" from "did not"
        // (system review 2026-10-31). execCommand is deprecated but still
        // works in this exact case every modern clipboard API refuses.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (!ok) throw new Error("execCommand copy was refused");
      }
      setCopied(which);
      setTimeout(() => setCopied(null), 1500);
    } catch {
      setCopyFailed(which);
      setTimeout(() => setCopyFailed(null), 4000);
    }
  };

  if (data.enabled) {
    return (
      <section className="space-y-4">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-[var(--color-text)]">
          <ShieldCheck className="h-4 w-4 text-[var(--color-primary)]" />
          Two-factor authentication is on
        </h3>
        <p className="text-xs text-[var(--color-text-muted)]">
          Logging in asks for a code from your authenticator app, once a day.
          Turning it off needs a current code or an unused recovery code.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            disable.mutate(disableCode, {
              onSuccess: () => {
                setDisableCode("");
                setCodesSaved(false);
                // Otherwise a later re-enroll in the same page session would
                // fall straight into the "confirm this code" step showing
                // the secret and recovery codes from the enrollment just
                // disabled, instead of the "Enable 2FA" starting point.
                enroll.reset();
              },
            });
          }}
          className="flex max-w-xs gap-2"
        >
          <input
            type="text"
            inputMode="numeric"
            value={disableCode}
            onChange={(e) => setDisableCode(e.target.value)}
            placeholder="Code or recovery code"
            className="flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-sm"
          />
          <button
            type="submit"
            disabled={disable.isPending || !disableCode.trim()}
            className="flex items-center gap-1.5 rounded-md border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-red)] transition-colors hover:bg-[var(--color-surface-hover)] disabled:opacity-50"
          >
            <ShieldOff className="h-3.5 w-3.5" />
            Turn off
          </button>
        </form>
        {disable.isError && (
          <p className="text-xs text-[var(--color-red)]">
            {(disable.error as Error).message}
          </p>
        )}
      </section>
    );
  }

  if (!enroll.data) {
    return (
      <section className="space-y-3">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold text-[var(--color-text)]">
          <KeyRound className="h-4 w-4" />
          Two-factor authentication is off
        </h3>
        <p className="text-xs text-[var(--color-text-muted)]">
          Add a code from an authenticator app (Google Authenticator, 1Password,
          Authy, …) as a second factor at login.
        </p>
        <button
          onClick={() => enroll.mutate(undefined)}
          disabled={enroll.isPending}
          className="rounded-lg bg-[var(--color-accent)] px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {enroll.isPending ? "Starting…" : "Enable 2FA"}
        </button>
        {enroll.isError && (
          <p className="text-xs text-[var(--color-red)]">
            {(enroll.error as Error).message}
          </p>
        )}
      </section>
    );
  }

  // Enrolled but not yet confirmed: show the secret and recovery codes
  // exactly once (the server will not hand them back again), then ask for
  // the first real code before turning anything on.
  const { secret, otp_uri, recovery_codes } = enroll.data;
  return (
    <section className="space-y-4">
      <h3 className="text-sm font-semibold text-[var(--color-text)]">
        Set up your authenticator app
      </h3>

      <div>
        <p className="mb-1 text-xs text-[var(--color-text-muted)]">
          Add this key manually (or open{" "}
          <code className="text-[11px]">{otp_uri}</code> on a device that has
          the app installed):
        </p>
        <div className="flex items-center gap-2">
          <code className="flex-1 overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-xs">
            {secret}
          </code>
          <button
            type="button"
            onClick={() => copy(secret, "secret")}
            className="rounded-md border border-[var(--color-border)] p-1.5 hover:bg-[var(--color-surface-hover)]"
            aria-label="Copy secret"
          >
            {copied === "secret" ? (
              <Check className="h-3.5 w-3.5 text-[var(--color-primary)]" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
        {copyFailed === "secret" && (
          <p className="mt-1 text-xs text-[var(--color-red)]">
            Couldn't copy automatically — select the key above and copy it
            manually.
          </p>
        )}
      </div>

      <div>
        <p className="mb-1 text-xs text-[var(--color-text-muted)]">
          Recovery codes — save these now. Each works once, if you ever lose
          the device.
        </p>
        <div className="flex items-start gap-2">
          <div className="grid flex-1 grid-cols-2 gap-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-3 font-mono text-xs">
            {recovery_codes.map((code) => (
              <span key={code}>{code}</span>
            ))}
          </div>
          <button
            type="button"
            onClick={() => copy(recovery_codes.join("\n"), "codes")}
            className="rounded-md border border-[var(--color-border)] p-1.5 hover:bg-[var(--color-surface-hover)]"
            aria-label="Copy recovery codes"
          >
            {copied === "codes" ? (
              <Check className="h-3.5 w-3.5 text-[var(--color-primary)]" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
        {copyFailed === "codes" && (
          <p className="mt-1 text-xs text-[var(--color-red)]">
            Couldn't copy automatically — select the codes above and copy
            them manually.
          </p>
        )}
      </div>

      <label className="flex items-start gap-2 text-xs text-[var(--color-text-muted)]">
        <input
          type="checkbox"
          checked={codesSaved}
          onChange={(e) => setCodesSaved(e.target.checked)}
          className="mt-0.5"
        />
        I've saved these recovery codes somewhere durable. If you're not an
        admin, an admin can reset your 2FA from the Admin panel if you lose
        both the device and these codes. Admin accounts have no such
        reset — if you're an admin, losing both means permanent lockout.
      </label>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          confirm.mutate(confirmCode);
        }}
        className="flex max-w-xs gap-2"
      >
        <input
          type="text"
          inputMode="numeric"
          autoFocus
          disabled={!codesSaved}
          value={confirmCode}
          onChange={(e) => setConfirmCode(e.target.value)}
          placeholder="000000"
          className="flex-1 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] px-3 py-1.5 text-center text-sm tracking-widest disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!codesSaved || confirm.isPending || !confirmCode.trim()}
          className="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {confirm.isPending ? "Checking…" : "Confirm"}
        </button>
      </form>
      {confirm.isError && (
        <p className="text-xs text-[var(--color-red)]">
          {(confirm.error as Error).message}
        </p>
      )}
    </section>
  );
}
