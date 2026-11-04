/**
 * Settings → Security: the TOTP enroll/confirm/disable flow.
 *
 * Three things worth pinning:
 *
 * * **Enrollment is two steps.** Calling the enroll endpoint does not turn
 *   2FA on by itself — the secret and recovery codes only become live once
 *   `confirmTwoFactor` sees a real code, and the UI must not claim "on" in
 *   between.
 * * **Disabling resets the enrollment view.** A later re-enroll in the same
 *   page session must start from the secret just generated, not the one a
 *   prior (now-disabled) enrollment left sitting in mutation state.
 * * **A wrong disable code changes nothing.** The status stays "on" and the
 *   error surfaces, rather than the form silently clearing.
 *
 * @vitest-environment jsdom
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getTwoFactorStatus = vi.fn();
const enrollTwoFactor = vi.fn();
const confirmTwoFactor = vi.fn();
const disableTwoFactor = vi.fn();

vi.mock("@/lib/twoFactorApi", () => ({
  getTwoFactorStatus: () => getTwoFactorStatus(),
  enrollTwoFactor: () => enrollTwoFactor(),
  confirmTwoFactor: (code: string) => confirmTwoFactor(code),
  disableTwoFactor: (code: string) => disableTwoFactor(code),
}));

const { SecuritySettings } = await import("./SecuritySettings");

declare global {
  var IS_REACT_ACT_ENVIRONMENT: boolean;
}

const ENROLLMENT = {
  secret: "ABCD1234EFGH5678",
  otp_uri: "otpauth://totp/Condor:root?secret=ABCD1234EFGH5678&issuer=Condor",
  recovery_codes: ["aaaa1111", "bbbb2222", "cccc3333"],
};

let container: HTMLDivElement;
let root: Root;

async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <SecuritySettings />
      </QueryClientProvider>,
    );
  });
  for (let i = 0; i < 20; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
  }
}

async function click(el: Element) {
  await act(async () => {
    (el as HTMLElement).click();
  });
  for (let i = 0; i < 10; i++) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5));
    });
  }
}

function buttonSaying(text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((b) =>
    b.textContent?.includes(text),
  );
  if (!button) throw new Error(`no button saying ${text}`);
  return button as HTMLButtonElement;
}

async function typeInto(el: Element, value: string) {
  const input = el as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )!.set!;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

function numericInput(): HTMLInputElement {
  const el = container.querySelector('input[inputmode="numeric"]');
  if (!el) throw new Error("no numeric input");
  return el as HTMLInputElement;
}

/** The Confirm form stays disabled until this is checked (finding #17). */
async function checkCodesSaved() {
  const el = container.querySelector('input[type="checkbox"]');
  if (!el) throw new Error("no codes-saved checkbox");
  await click(el);
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  getTwoFactorStatus.mockReset().mockResolvedValue({ enabled: false });
  enrollTwoFactor.mockReset().mockResolvedValue(ENROLLMENT);
  confirmTwoFactor.mockReset().mockResolvedValue({ enabled: true });
  disableTwoFactor.mockReset().mockResolvedValue({ enabled: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
});

describe("SecuritySettings", () => {
  it("offers to enable 2FA when it is off", async () => {
    await mount();
    expect(container.textContent).toContain("off");
    expect(() => buttonSaying("Enable 2FA")).not.toThrow();
  });

  it("does not claim 2FA is on until the first code is confirmed", async () => {
    await mount();
    await click(buttonSaying("Enable 2FA"));

    expect(enrollTwoFactor).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain(ENROLLMENT.secret);
    expect(container.textContent).toContain(ENROLLMENT.recovery_codes[0]);
    // Still off — enrolling alone must not flip the status.
    expect(container.textContent).not.toContain("is on");
  });

  it("turns 2FA on once a real code is confirmed", async () => {
    await mount();
    await click(buttonSaying("Enable 2FA"));
    await checkCodesSaved();
    await typeInto(numericInput(), "123456");
    await click(buttonSaying("Confirm"));

    expect(confirmTwoFactor).toHaveBeenCalledWith("123456");
    expect(container.textContent).toContain("is on");
  });

  it("keeps 2FA on and shows the error when the disable code is wrong", async () => {
    getTwoFactorStatus.mockResolvedValue({ enabled: true });
    disableTwoFactor.mockRejectedValueOnce(new Error("Invalid code"));
    await mount();

    const input = container.querySelector("input") as HTMLInputElement;
    await typeInto(input, "000000");
    await click(buttonSaying("Turn off"));

    expect(disableTwoFactor).toHaveBeenCalledWith("000000");
    expect(container.textContent).toContain("Invalid code");
    expect(container.textContent).toContain("is on");
  });

  it("falls back to execCommand when navigator.clipboard is unavailable", async () => {
    // navigator.clipboard is undefined on plain HTTP served to anything but
    // localhost — the Tailscale/remote-VPS topology this app explicitly
    // supports. Before the fallback, the copy button silently did nothing.
    const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    const execCommand = vi.fn().mockReturnValue(true);
    document.execCommand = execCommand;

    try {
      await mount();
      await click(buttonSaying("Enable 2FA"));
      await click(container.querySelector('[aria-label="Copy secret"]')!);

      expect(execCommand).toHaveBeenCalledWith("copy");
      expect(container.textContent).not.toContain("Couldn't copy");
    } finally {
      if (original) Object.defineProperty(navigator, "clipboard", original);
    }
  });

  it("tells the user to copy manually when every copy path fails", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
    document.execCommand = vi.fn().mockReturnValue(false);

    await mount();
    await click(buttonSaying("Enable 2FA"));
    await click(container.querySelector('[aria-label="Copy recovery codes"]')!);

    expect(container.textContent).toContain("copy them manually");
  });

  it("disabling resets the enrollment view for a later re-enroll", async () => {
    await mount();
    // Enroll and confirm once.
    await click(buttonSaying("Enable 2FA"));
    await checkCodesSaved();
    await typeInto(numericInput(), "123456");
    await click(buttonSaying("Confirm"));
    expect(container.textContent).toContain("is on");

    // Disable it.
    const disableInput = container.querySelector("input") as HTMLInputElement;
    await typeInto(disableInput, "654321");
    await click(buttonSaying("Turn off"));
    expect(container.textContent).toContain("off");

    // A fresh enroll must show a NEW enrollment call, not the old one's data
    // left over from before — a second, different secret proves it.
    enrollTwoFactor.mockResolvedValueOnce({
      ...ENROLLMENT,
      secret: "NEWSECRET9999",
      otp_uri: "otpauth://totp/Condor:root?secret=NEWSECRET9999&issuer=Condor",
    });
    await click(buttonSaying("Enable 2FA"));
    expect(container.textContent).toContain("NEWSECRET9999");
    expect(container.textContent).not.toContain(ENROLLMENT.secret);
  });
});
