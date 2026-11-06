# Two-strategy crypto paper-trading trial

Two independent $50 portfolios track forward paper trades and market observations. The runner uses public market-data endpoints and records all fills in a local simulation ledger.

## GitHub Actions

The [Crypto paper trial workflow](.github/workflows/paper-trial.yml) runs one cycle
for both strategies every 15 minutes on a standard GitHub-hosted runner in this
public repository. GitHub can delay scheduled runs. The workflow also runs when
its configuration changes on `main` and supports manual runs from the Actions tab.

The [paper-logs branch](https://github.com/OneTapTech/crypto-paper-trial/tree/paper-logs)
stores the SQLite ledger and latest standalone dashboard. Each run restores the
previous ledger before collecting data and pushes the updated ledger back to that
branch. Scanner errors are saved before the health check reports a failure.
The ledger includes all historical signals, simulated fills, equity snapshots and
scanner events. The dashboard shows recent history.

Open the latest run under [Actions](https://github.com/OneTapTech/crypto-paper-trial/actions/workflows/paper-trial.yml)
for balances, feed status and health checks. To view the dashboard, download
`dashboard.html` from `paper-logs` and open it in your browser. Disable the
workflow in Actions when the observation period ends.

### First upload to the empty repository

Extract the deployment ZIP and open a terminal in its top-level folder. From a
Git session signed in as an owner of the repository, run:

```bash
git init -b main
git add .
git commit -m "Set up crypto paper-trading experiment"
git remote add origin https://github.com/OneTapTech/crypto-paper-trial.git
git push -u origin main
```

The first push starts a run. In Actions, confirm that **Save ledger and
dashboard** and **Verify dashboard and report portfolio results** both pass.
Confirm that `paper-logs` contains `trial.sqlite3` and `dashboard.html`.

## Run locally

Requires Python 3.10+ and internet connectivity. Uses only Python standard library.

```bash
python trader.py --once              # verify data sources and populate trial.sqlite3
python dashboard.py                 # http://127.0.0.1:8080
python trader.py --interval 300     # continuous polling, separate terminal
```

The established strategy uses CoinGecko market data and falls back to Coinbase Exchange USD product statistics if CoinGecko is unavailable. The fallback estimates 24-hour USD volume as base volume multiplied by the latest price. The new-token strategy uses DexScreener profiles and pair data. Scanner events identify the source and record feed errors.

## Strategies

**Established:** BTC, ETH, SOL, LINK, AVAX. Buy on +1% to +8% reported 24-hour momentum, if reported 24-hour volume > $10M. Exit on 6% stop, 12% take-profit, 5% trailing stop or 24-hour timeout. The price change is a coarse signal, not proof of predictive power.

**New tokens:** discover recent token profiles, check pair age 1–48 hours, liquidity >= $100k and 24h volume >= $100k, with >8% 1h momentum. **Trades are deliberately disabled by a fail-closed safety gate** because DexScreener pair data does not verify holder concentration, contract permissions, honeypot/sellability, or transfer taxes. The scanner and rejection logs still run. To permit even simulated trades in this strategy, integrate independent contract-risk checks and a sellability simulation; do not simply bypass the gate. New-token results will therefore not be directly comparable until that integration is completed.

**Execution model:** simulated fills at market mark plus/minus 0.2% (established) or 1.5% (new) slippage; 0.1% per-side fee; up to 20% of available cash per position, capped at $10; maximum two open positions per portfolio. No margin or leverage. These are assumptions, not observed fill quality. Stop-losses are checked only when the poll runs, so gaps may exceed thresholds.

## Limitations and safeguards

- **This is not six independent AI models.** It is a transparent six-stage agent-like pipeline (scanner, rug filter, sizer, executor, logger, equity). It cannot claim learned AI intelligence.
- **No backtest or performance claim is included.** Only forward-observed simulated prices after starting the runner count toward the trial.
- **No hard portfolio-level circuit breaker is implemented yet.** Add max daily drawdown, stale-price protection, API-health gating, and emergency stop before any serious extended test. For now stop the process to halt it.
- **Equity uses last observed marks**, which can be stale. New tokens are particularly exposed to price manipulation, fake volume and unexecutable exits.
- **Reported realized P/L** is based on simulated fills and fee estimates. Real-world taxes, spreads, latency, failed orders and price impact may differ.
- Never expose this dashboard publicly without authentication. It binds to localhost by default.

## Review metrics

For each portfolio: initial $50, ending equity, realized/unrealized P/L, fees, number of entries/exits, win rate, maximum drawdown, rejected signals, feed outages, time online, and assumptions. 

## Ubuntu VM setup (paper-only)

Use a dedicated Ubuntu 24.04 VM with SSH access, 1 GB+ RAM, and a persistent disk. Keep TCP 8080 **closed** in the cloud firewall. SSH (22) should be restricted to your IP. The supplied installer creates a restricted `paperbot` user and two auto-restarting systemd services.

From your computer, after replacing `YOUR_SERVER_IP` and pointing to the folder containing the downloaded archive:

```bash
scp crypto_paper_trial_cloud.zip ubuntu@YOUR_SERVER_IP:/tmp/
ssh ubuntu@YOUR_SERVER_IP
sudo apt-get update && sudo apt-get install -y unzip
sudo mkdir -p /opt/crypto-paper
sudo unzip -o /tmp/crypto_paper_trial_cloud.zip -d /tmp/crypto-unpacked
sudo cp /tmp/crypto-unpacked/crypto_paper_trial/{trader.py,dashboard.py,healthcheck.py,deploy_ubuntu.sh} /opt/crypto-paper/
sudo bash /opt/crypto-paper/deploy_ubuntu.sh
sudo journalctl -u crypto-paper-trader -n 50 --no-pager
sudo -u paperbot python3 /opt/crypto-paper/healthcheck.py
curl -fsS http://127.0.0.1:8080/api
```

On your own computer, open a separate terminal and run `ssh -N -L 8080:127.0.0.1:8080 ubuntu@YOUR_SERVER_IP`, then visit `http://127.0.0.1:8080`. Do not publish the dashboard on a public IP. If you already have an older `trial.sqlite3`, don't copy it when starting a fresh trial. The first healthy workflow cycle sets the start of the GitHub Actions trial.

Check `systemctl is-active crypto-paper-trader crypto-paper-dashboard` and `journalctl -u crypto-paper-trader -f`. `healthcheck.py` verifies recent snapshots, not full market-feed correctness. The new-token strategy remains intentionally unable to place paper trades until independent token safety checks are implemented. If an API is unavailable, no results can be inferred from missing data.
