# Get started

Griffin's agents watch the market and place trades on your exchange account for you. This guide takes you from a fresh install to your first agent running. Plan on about twenty minutes.

You need two things: an account on a crypto exchange, and an API key from that account. An API key is a pair of long codes that lets Griffin trade for you without ever knowing your password.

> A note on practice accounts. Most exchanges offer a practice version (often called testnet) where the money isn't real. The agents that come with Griffin are set up for the real Binance and Bitget futures markets, not the practice ones. To use a practice account you have to copy an agent and change which exchange it uses, and that means editing its instructions. If that's more than you want to take on, start on the real exchange with a very small amount and the limits in [Step 4](#step-4-try-a-dry-run).

## Step 1. Say hello to Griffin

Click **Agents** at the top, then **Griffin** in the list on the left — that's the general assistant, not one of the trading specialists below. Type "hi" in the box at the bottom and send it.

If Griffin answers, the AI behind your agents is working. If nothing comes back, see "If something goes wrong" at the bottom of this guide.

## Step 2. Connect your exchange

1. Click **Settings** at the top, then **Keys and Wallets** on the left.
2. Click **Add Credential**.
3. Choose the type. **Spot** is plain buying and selling. **Perpetual/futures** is trading contracts, and it's what most of the built-in agents use.
4. Pick your exchange, paste in your API key and secret, and save.

When you create the key on the exchange's website, switch on trading and leave withdrawals off. That way the key can't move money out of your account, even if someone else gets hold of it.

To check it worked, open **Portfolio**. Your balance should be there.

Hyperliquid does not serve this region and has been removed. Solana uses a wallet instead of a key. Solana wallets go under **Gateway** in Settings.

If you change a key later, restart anything that was already running. It keeps using the old key until you do.

## Step 3. Choose an agent

Each agent has one job, and each trades on particular exchanges. Pick one that matches the exchange you just connected.

- **Adaptive Grid Trader** places a ladder of buy and sell orders on one coin and adjusts it once an hour. It has two ready-made setups: BTC on Bitget and SOL on Binance.
- **Directional Trader** follows price trends, trading in the direction a coin is moving. It has one setup, on Binance futures.
- **Market Making Expert** places buy and sell orders on both sides of the price and earns the small gap between them. One setup uses the exchange you name when you start it.
- **Solana DEX LP Expert** puts money into liquidity pools on Solana. It needs a Solana wallet connected under Gateway.
- **Fundamental Analyst** researches a company or crypto project and tells you whether it looks cheap or expensive. It doesn't trade.
- **Griffin** is the general assistant from Step 1. Ask it about your portfolio or your bots.

For example, with a Binance futures key connected, the SOL grid setup and the Directional Trader's setup have an account to trade on. The BTC grid setup needs Bitget.

Click the agent's name in the list on the left. At the top of its page is a **Loop** menu. Each choice there is one ready-made setup, called a loop. Pick one.

## Step 4. Try a Dry Run

Click **Start**. A window called **Start New Session** opens. At the top are three choices:

- **Dry Run**: the agent looks at the market and tells you what it would do. It places no trades. Always begin here.
- **Run Once**: the agent makes one round of decisions and can place real trades.
- **Loop**: the agent repeats on a timer until you stop it.

Choose **Dry Run**, then fill in the rest:

- **Trading Context**: say what you want in your own words. For example, "Trade BTC only and keep it cautious."
- **Total Amount Quote**: the most money the agent can use in this session, in the exchange's main currency (usually USDT). Start with a small number.
- **Risk Limits**: three hard stops the agent can't go past, even if it wants to.
  - **Max Position ($)**: the most it can have in open trades.
  - **Max Executors**: how many trades it can have open at once.
  - **Max Drawdown %**: how much it can lose before it's blocked from trading.

Leave the other boxes as they are, then press **Run Dry Test**.

When it finishes, its notes appear on the agent's page. Read what it decided and why. If it doesn't make sense, change the Trading Context and try again.

## Step 5. Let it trade

When a dry run looks sensible:

1. Click **Start** again, choose **Run Once**, and press **Execute Once**. The agent makes one round of decisions and may place real orders. Afterwards, check **Portfolio** and your exchange to confirm it did what you expected.
2. When that goes well, click **Start** once more and choose **Loop**. Set **Frequency (sec)**, which is how often the agent checks the market. 3600 means once an hour, which is what the grid setups are built for. Checking more often uses more of your AI allowance. Then press **Start Session**.
3. Tick **Resume after Griffin restarts** if you want the agent to carry on by itself after a restart.

Keep the amount small for the first few days.

## Step 6. Watch it, and stop it

The agent's page shows each round: what it saw, what it decided, and what it did. **Portfolio** and **Bots** show what's actually open on the exchange.

**Pause** holds a session and **Resume** carries on where it left off. **Stop** ends it, after you confirm.

Trades that are already open can stay open on the exchange after an agent stops or Griffin is turned off. Check **Portfolio** and **Bots**, and close anything you don't want to keep.

## Good habits

- Keep the amount and the limits small until you trust an agent.
- By default, the portal only opens on the computer Griffin runs on, with no login required on that computer — so leave it that way unless you know you need remote access. If you do expose it beyond that computer (for example over Tailscale), turn on two-factor login first — see [Securing your login](#securing-your-login) below.
- The grid agents expect to be the only thing trading that coin on that account. Don't trade the same coin by hand while one is running.
- Read what the agent wrote about each decision. If something surprises you, stop it.

## Securing your login

If more than one person can reach the portal, or you access it from outside your own network, turn on two-factor login: **Settings → Security → Enable 2FA**. It asks for a 6-digit code from an authenticator app (like Google Authenticator or Authy) on top of the normal login, and gives you a set of one-time recovery codes.

**Save those recovery codes before you confirm.** Each one works exactly once, and the page will not let you continue until you tick the box confirming you've saved them. If you're an approved user (not an admin) and you lose both your authenticator device and these codes, an admin can reset your 2FA from **Settings → Admin**, your name in the list, **Reset 2FA**. Admin accounts have no such reset today — if you're an admin, losing both locks you out permanently, with no in-app recovery. Save the codes somewhere durable (a password manager, not a note on the same computer) before you turn this on, and double-check that step if you're the admin.

## If something goes wrong

**A tab says "Connect an Exchange or Wallet".** No exchange is connected yet. Do [Step 2](#step-2-connect-your-exchange).

**The agent never answers, or stops partway through the day.** Free AI models have a daily limit, around 1000 requests. Wait until tomorrow, or switch to a paid model. The last section of this guide shows how.

**The Start window shows the server as offline.** Griffin runs two separate things: the dashboard you're looking at, and a Docker-based trading engine (`hummingbot-api`) alongside it that actually talks to exchanges. This message means the second one isn't reachable. Open Docker Desktop and check that it's running, then from the `condor` folder run `make doctor` — it specifically checks whether the trading engine is up and authenticating, and says what's wrong if it isn't.

**Start is refused.** The agent's setup check failed, for example because the account doesn't hold enough of the right coin. The message on screen says what's missing.

**The agent starts but places no trades.** It may be set up for an exchange you haven't connected ([Step 3](#step-3-choose-an-agent)), or it may have decided there was nothing worth doing. Its notes on each round say which.

**I changed my key but the agent still uses the old one.** Stop it and start it again.

## For terminal users

Everything above is done in the portal. These commands are for the computer Griffin runs on — the codebase and its `make` commands are still named `condor`. Run them from the `condor` folder.

```
make run          # start Griffin
make stop         # stop Griffin
make restart      # restart Griffin — does NOT touch the separate Docker trading engine (see below)
make doctor       # check the setup, including the Docker trading engine
make pick-model   # choose the AI
```

`make run`/`stop`/`restart` only manage Griffin's own process. The Docker-based trading engine (`hummingbot-api`) is a second, separate thing started the first time by `make setup` — if trades stop working but Griffin itself is responsive, check that stack (`docker ps`, or just `make doctor`) before assuming Griffin itself is broken.

After `make run`, open http://localhost:8088 in your browser.

To check that an agent can really trade before you start it:

```
make doctor AGENT=adaptive_grid_trader
```

It checks that the account holds the right coin in the right amount, that the account's mode and leverage fit, that your order sizes meet the exchange's minimums, and that the computer's clock is right. A ✗ means the agent won't start. A ? means it couldn't be checked, and that is not a pass.

To add Binance or Bybit practice keys from your `.env` file, first set `BINANCE_EXCHANGE_API` and `BINANCE_SECRET_KEY` (or `BYBIT_TESTNET_API_KEY` and `BYBIT_TESTNET_API_SECRET`). Then run:

```
uv run python scripts/register_testnet_credentials.py --check
uv run python scripts/register_testnet_credentials.py
```

The first line only checks. The second saves the keys and reads your balance back.
