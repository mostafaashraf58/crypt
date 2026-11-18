# Use the local price forecasting model

This setup downloads Amazon's pretrained **Chronos-Bolt Small** checkpoint and runs it on your PC's CPU. Axiom 0.2.3 can connect with one button and display the estimated next candle close, its model range, the upward score, and the target time. You can change the trading pair and candle interval in the app.

The forecast uses up to 200 completed closing prices. The price estimate is the model's median forecast. The range runs from its 10th to 90th percentile. The upward score is approximated by interpolating its quantiles and is limited to 10–90%. Its probability and range coverage have not been calibrated on your market. Predictive accuracy and trading profitability have not been established.

## First use on this PC

The model has already been downloaded and tested in `D:\crypt`. Install the updated **Axiom Workstation 0.2.3** installer from `src-tauri\target\release\bundle\nsis`, then open PowerShell and run

```powershell
cd D:\crypt
./scripts/start-model.ps1
```

Keep that window open. Wait until it says **Application startup complete**.

1. Open the updated Axiom desktop app and select **Paper**.
2. Select **Bitfinex market data** at the bottom of the app, then open **AI Models** and click **Connect local model**.
3. Under **Forecast market**, choose **BTC/USDT**. Under **Forecast interval**, choose **1h**.
4. Confirm **Chronos-Bolt Small (local)** is selected under **Prediction model**.
5. Click **Get next interval forecast**.

The result shows the predicted next close in USDT, the 80% model range, the last completed close, the input cutoff, and the target close time in Cairo time. A 1h forecast predicts the close of the hour immediately following the latest completed hourly candle. The current partial candle is excluded.

Forecast checks work with public Bitfinex market data. You can use these steps before connecting an exchange account. Model connection pauses the selected mode. Selecting a model for a forecast does not assign it to a trading strategy.

## Download and set up on another PC

Keep the complete Axiom folder, including `model_service`, `scripts`, and `requirements-model.txt`. Downloading a GitHub ZIP works, or clone the repository when these changes are available there. Install **Python 3.13 or 3.12, 64-bit**, including the Python launcher, from [python.org](https://www.python.org/downloads/windows/). Allow approximately 3 GB of free disk space for setup. Setup requires internet access to PyTorch, PyPI, Hugging Face, and their download servers.

Open PowerShell in the Axiom folder and run

```powershell
./scripts/setup-model.ps1
```

The script installs the CPU runtime, downloads the pinned pretrained model, verifies that it loads, and runs a sample forecast. Wait for **Setup complete**. If the download is interrupted, rerun the same command to resume. Then run

```powershell
./scripts/start-model.ps1
```

Follow steps 1–5 above. Setup selects and installs everything required. Editing source files, model settings, URLs, and model weights is unnecessary.

If PowerShell blocks these local scripts, run each command through PowerShell with a policy override for that invocation

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-model.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-model.ps1
```

The model service listens at `127.0.0.1:9001` and loads the downloaded checkpoint from `work\local-model`. Inference runs locally after setup. Bitfinex price updates still need internet access.

## Each later session

Start `./scripts/start-model.ps1`, keep its PowerShell window open, and open Axiom. The model registration persists. Click **Health check** on its card, then request a forecast. Use **Connect local model** again if the registration is missing. Stop the service with **Ctrl+C** in its PowerShell window.

Paper and Live keep separate model registries. Connect the model in each mode where you want to use it. The downloaded weights and local service are shared.

To use it for paper strategy decisions, open **Strategies**, edit or create a **BTC/USDT** strategy, set **Prediction interval** to **1h**, select **Chronos-Bolt Small (local)**, and save. Then use **Trading** to select and start that paper strategy. Strategies use the upward score to evaluate their entry and exit thresholds. The numeric price forecast is displayed for inspection.

## If something needs attention

| Message or symptom | Action |
| --- | --- |
| Local model unavailable | Run `start-model.ps1`, wait for startup, then click Connect local model again. |
| Model files missing or invalid | Rerun `setup-model.ps1`. |
| Address already in use / port 9001 | An existing model window may already be running. Use its service or stop it with Ctrl+C before starting another. |
| Model is busy | Wait for the current forecast to finish, then request again. |
| Bitfinex data unavailable or stale | Check internet access and retry after market data refreshes. |
| Uncalibrated quantile estimate | This describes how the direction score is computed. It is expected for this pretrained model. |
| No Connect local model button | Install the new 0.2.3 app. An older installed executable will continue showing its older interface. |

You can recheck the downloaded model independently

```powershell
./.venv-model/Scripts/python.exe -m model_service.download --verify
```

## Model details

The setup pins `amazon/chronos-bolt-small` to revision `772f3d25d38aec6d914c8949dab4462e2d46f5d8`. It stores a manifest with local file hashes and checks them before serving. The service version includes the checkpoint revision and adapter version, and **Connect local model** pins that version in Axiom.

This model has 48 million parameters and supports pretrained time series forecasting. I selected it for a downloadable CPU setup that needs no training. Its performance on cryptocurrency prices needs separate evaluation. Model and runtime dependencies are isolated in `.venv-model`.

The checkpoint and Chronos software use the Apache 2.0 license. Keep the downloaded model card with the weights. See the [official model card](https://huggingface.co/amazon/chronos-bolt-small), [Chronos source and license](https://github.com/amazon-science/chronos-forecasting), and [Axiom prediction API](AI-API.md).
