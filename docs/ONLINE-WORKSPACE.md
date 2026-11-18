# Online desktop workspace — 0.2.1

30 October 2026. Desktop startup now selects Bitfinex and live account mode. It overrides an old saved fixture source without deleting paper records. The desktop hides fixture-source and paper-mode selection, starts without illustrative balances/signals, and does not fall back to synthetic charts if the engine disconnects.

Public Bitfinex candles and instrument discovery work without account credentials. A real network request returned ten BTC/USDT hourly candles during verification. Public charts do not establish account access. Balances, holdings and execution require credentials entered in Connections and successful account synchronization. No trade was submitted and no autonomous authorization was created.

Production engine startup uses --production. Fixture candles/catalog/execution and paper-mode switching are rejected in that mode; existing simulation records remain preserved. Developer/browser preview and regression fixtures remain available for testing.

Verification: 100 Python tests and 45 frontend checks pass; TypeScript/Vite passes. A production regression checks online defaults, no restored trading authority, fixture refusal, public-data adapter cleanup and preserved paper records. The 0.2.1 unsigned Windows installer built successfully (34.03 MiB). Two native open/close cycles passed with isolated data, authentication, shutdown cleanup and automatic-backup verification. Existing paper positions and order idempotency remained intact. The rebuilt desktop was then opened using the existing user profile, without activating trading. QA evidence: work/native-qa-11f954aecb544ea6930182b019b4c1f1.

All real-account acceptance and signing limits in RELIABILITY-RELEASE.md still apply. Online defaults are not an assertion that exchange execution has passed production acceptance.
