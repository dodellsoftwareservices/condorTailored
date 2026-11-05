# UI overhaul collaboration

Shared checkout, October 31, 2026. Preserve the existing trading and account
behavior, and keep OpenBB implementation separate from presentation changes.

## Owner's visual direction (November 4)

Use BMW factory light blue tones as the signature accent for Griffin. The
existing Zandvoort-inspired `--color-primary` is the starting point, with
graphite and white surfaces and enough contrast for small text. Never use
yellow, amber, or orange in the interface. Negative PnL, losses, errors, and
warnings use a readable soft red in every theme; positive trading values use
green. Categorical charts use blue, teal, violet, rose, or neutral tones. This is a design
preference, not a claim that the CSS value exactly matches a BMW paint code.

- Kimi's changes already present: `AppShell.tsx`, `CommandPalette.tsx`,
  `useShellLayout.ts`, `shell.css`, and its import in `main.tsx`.
- ChatGPT's complementary track: portfolio responsiveness; shared empty/loading
  states; native confirmation dialog; viewport-aware popovers; shared resize
  gestures and their consumers. Shared surface styles live in
  `components/ui/workspace-surfaces.css`.

Integration fixes: shell storage keys are declared in `lib/sessionState.ts`
(device preferences retained across login), and the route transition ends with
`transform: none` so fixed-position dialogs retain viewport positioning. The
native-dialog jsdom double lives in `src/test/native-dialog.ts`; it supplies
open/close state only. Verify actual focus containment in the browser.

No new UI dependency or replacement routing/data layer is needed. Preserve
query keys, permission checks, trading validation, URLs, and all existing actions.

Route code is loaded on demand in `App.tsx` with a local Suspense boundary per
page. The shared shell, server selection, and socket provider stay mounted.
Validate touch/pointer cancellation, keyboard operation, small viewports, and
reduced motion alongside a production frontend build.

## Verified handoff

- Production build passes. The main minified entry changed from 2,526 kB
  (718 kB gzip) to 917 kB (277 kB gzip). Page chunks load separately; the
  bootstrap also preloads the 115 kB API/shared chunk. These are bundle sizes,
  not a measured end-to-end startup benchmark. Vite still flags the main entry
  as larger than its configured 600 kB warning threshold.
- All 2,440 frontend tests pass across 236 files, including the existing draft
  guards, strategy deletion, workspace splits, and portfolio positions tests.
- Browser QA uses intercepted mock API responses and WebSockets: no real
  trading backend or provider is accessed. Checked 320, 390, 820, and 1440 px
  viewports; portfolio geometry; mobile CEX / DEX views; order draft retention;
  route loading without remounting the shell; light, dark, and colorblind
  themes; and reduced motion.
- Native confirmation QA checks `:modal`, keyboard focus isolation from the
  background page, Escape dismissal, and opener focus restoration. Browser
  chrome remains reachable by Tab, as native dialogs allow.
- Screenshots and mock-browser results for this run are under
  `/private/tmp/condor-ui-review`. This temporary directory is not shipped.

The backend, OpenBB integration, and the owner's `.env` were not changed by
this UI track. The existing API contracts, query keys, permission checks,
validation, and action handlers remain in use.
