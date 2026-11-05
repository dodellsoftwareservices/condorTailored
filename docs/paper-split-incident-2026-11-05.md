# Alpaca Paper ITA split incident — November 5, 2026

The large apparent single-position loss was caused by ITA's 3:1 split and an
unadjusted Alpaca Paper share count. The $2,500 per-order cap was respected.
KrakenSim remained at $50,000 with no spot fills, orders, fees or holdings.

## Order attribution

- Submitter: the Claude coding session `5a2c1c44-053a-493e-a2da-63fea2c369be`.
- User instruction at 16:59:54 UTC on November 4: add more ETFs to Alpaca.
- Claude created and ran its scratchpad `place_etfs.py`, using local login and
  `POST /api/v1/paper/equities/orders` to buy ITA, XLE, IBB, IHE, PEJ and VOO.
- ITA order: `d4f4b424-a789-4ec7-a385-9e938c1cf4fe`, submitted
  November 4 at **17:01:10 UTC / 12:01:10 p.m. EST**.
- Filled: 11.697501403 shares at $213.72, approximately $2,499.99.
- Alpaca returned 14 lifetime orders: all $2,500 buy orders on November 3–4.
  There were no new orders or sells on November 5 at investigation time.
- Local matching evidence: `data/paper_okr_journal.json`; Claude session log
  under `~/.claude/projects/-Users-devonodell-Desktop-internetmoney-hedge-fund-hedge/`,
  session above, tool invocation at line 1880.

## Why the displayed loss jumped

[iShares' October 5 filing](https://www.ishares.com/us/library/stream-document?documentId=1280410~1280117~926348~1255433~1192007~1864318&iframeUrlOverride=%2Fus%2Fliterature%2Fsai%2Fsai-ishares-trust-3-31.pdf&product=I-DJRBANK&shareClass=NA&stream=reg)
specifies a 3:1 ITA split, payable after November 4's close, with trading on the
new basis beginning November 5. Alpaca's announcement API independently returned
`old_rate=1`, `new_rate=3`, `ex_date=2026-11-05`.

Alpaca Paper showed the new market price, approximately $68, but retained
11.697501403 shares. The economic quantity after the split would be
35.092504209 shares. The raw position therefore appeared to lose about $1,706;
its split-adjusted estimated loss was about $113–$117 during the investigation.

[Alpaca staff describes the paper environment's corporate-action limitation](https://forum.alpaca.markets/t/ped-reverse-split-not-handled-in-paper-accounts/18569).
The application previously trusted raw paper equity without reconciling it.
The order preflight checked size and market hours, but did not check splits.

At one verified live reading, broker equity was $97,023.65 and estimated
split-adjusted equity was $98,614.74, a $1,591.09 difference. Combined with
KrakenSim's $50,000, the displayed estimate became $148,614.74. The remaining
approximately $1,385 loss was movement in the held basket; the correction does
not erase those losses.

## Remediation

- The shared Alpaca client checks recent and upcoming split announcements before
  paper buys; a split or unavailable feed blocks submission. Sells remain usable.
- Observed announcements are retained in `data/paper_corporate_actions.json`,
  so a rolling provider window cannot later remove an applied display correction.
- Valuation reconciles complete fills, announced split ratios and broker quantity.
  It adjusts only when the broker still matches the original quantity, and skips
  adjustment if the broker already matches the split quantity.
- Dashboard, Equities and the paper OKR routine use the same reconciliation.
  A suspicious move with missing or contradictory evidence is withheld for review.
- The UI labels corrected values as estimates. Broker cash, positions and buying
  power are not modified. Broker history after an unprocessed split is omitted
  from the displayed chart rather than drawing a false cliff.

Validation: 131 focused backend tests passed, including split reconciliation,
double-adjustment prevention, missing evidence and pre-submit blocking. The
frontend production build and the color guard passed. Live dashboard and
Equities API reads confirmed the correction after restart.
