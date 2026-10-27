# Browser verification

Verified on October 26, 2026 with system Chromium, Playwright, and the built Vite application served locally. This verifies the offline browser workflow, not Windows WebView2 or external provider accounts.

**29 browser checks passed. Zero browser runtime errors.**

Covered all ten navigation screens, global asset search, interval selection, SMA indicator, keyboard Escape, market filtering, paper buy/sell fills, overselling rejection, strategy threshold persistence, model registration/health/removal, honest HTTP model health in offline mode, deterministic backtest equity/trades, disabled browser credential entry, risk limit persistence, blocked real order confirmation in offline mode, proposal cancellation, light theme, paper engine start/pause, and page overflow at 1440×1000 and 390×844.

`tests/browser-check.mjs` runs against a started local server. Install the Playwright browser first with `npx playwright install chromium`, or set `CHROMIUM_PATH` to a compatible installed Chromium executable:

```sh
npm run build
npm run preview -- --port 1421
# In a second terminal:
AXIOM_URL=http://127.0.0.1:1421 node tests/browser-check.mjs
```

On Windows PowerShell, assign environment variables with `$env:AXIOM_URL='http://127.0.0.1:1421'` before running Node. Screenshots default to `work/browser-tests`; override with `QA_OUTPUT_DIR`.

Actual market-provider credentials, live broker order submission, model-server integration over a real network, Windows installation, and unattended process recovery were not exercised in these browser checks. Separate Python tests cover engine and API behavior with deterministic fixtures and mocked provider transport.

The final self-contained `Axiom-Preview.html` also passed all 29 workflow checks when served over local HTTP, including equity and drawdown rendering, with zero runtime errors. Direct `file://` launch was blocked by the managed browser administrator policy and was not verified here.
