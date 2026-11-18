# 0.2.0 verification update

0.2.2 build checkpoint (31 October 2026): the current source was packaged successfully as `src-tauri/target/release/bundle/nsis/Axiom Workstation_0.2.2_x64-setup.exe` (Windows x64, unsigned). The build passed 99 Python tests, 47 frontend checks, TypeScript/Vite compilation, Python engine bundling and native NSIS packaging. An isolated run of the bundled engine verified version 0.2.2, authenticated Live and Paper snapshots, unauthorized-request rejection, the built-in deterministic model, DuckDB history availability and paused startup in both modes. Temporary engine processes were stopped after verification. This checkpoint did not install or launch the new desktop executable, access a real Bitfinex account or submit trades. Earlier installed-app checks below apply to their stated versions.

29 October 2026: new unsigned x64 installer built successfully at `src-tauri/target/release/bundle/nsis/Axiom Workstation_0.2.0_x64-setup.exe`. The rebuilt desktop passed two isolated native startup/close/reopen cycles, including stored positions, orders/idempotency, settings, models and source history; automatic backup hashes, version 0.2.0, loopback binding, unauthorized HTTP rejection and sidecar cleanup passed. Disposable OS-vault save/read/delete passed. The actual user profile was not reset, installed over or used for these tests. See [current release evidence](RELIABILITY-RELEASE.md). Prior installed/uninstalled 0.1.0 verification is retained below.

# Windows verification - 28-29 October 2026

Verified locally in `D:\crypt` on Windows 11 with Node 24.21.0 and Python 3.13.6.

| Check | Result |
| --- | --- |
| Locked Node dependency installation (`npm ci`) | Passed; audit reported zero vulnerabilities |
| Python dependency installation | Passed using public PyPI after the initial resolution failed |
| TypeScript and Vite production build (`npm run build`) | Passed; Vite reports a JavaScript chunk above 500 kB |
| Frontend logic (`npm run test:frontend`) | 45 passed |
| Backend (`.venv/Scripts/python.exe -m pytest tests -q`) | 60 passed; final packaging run took 5.50 seconds, with one non-fatal pytest cache permission warning |
| Browser workflows (`npm run test:browser`) | 29 passed in system Edge against the Vite development server; zero runtime errors |
| Offline preview generation | `dist-offline/Axiom-Preview.html` generated successfully |
| PyInstaller engine bundle | `work/engine-bundle/axiom-engine.exe` built successfully |
| Bundled engine smoke check | Startup, authenticated snapshot and unauthorized rejection passed. Test cleanup was blocked by the sandbox; the test process tree was subsequently terminated with approved elevated access. |
| Rust installation | Rust/Cargo 1.99.1 installed successfully |
| Visual Studio 2022 C++ Build Tools installation | Passed: MSVC 14.44.35207 on `D:\BuildTools2022`, Windows SDK 10.0.26100.0 |
| Native desktop / NSIS installer | Passed: optimized x64 executable and unsigned NSIS installer built with `./scripts/build-windows.ps1 -SkipSetup` |
| Installed desktop smoke check | Passed: silent installation, desktop launch, bundled engine health, loopback binding and unauthorized snapshot rejection |
| Installed shutdown and uninstall | Passed: normal window close terminates both PyInstaller processes; silent uninstall removes the desktop executable |

Installer: `src-tauri/target/release/bundle/nsis/Axiom Workstation_0.1.0_x64-setup.exe` (58.23 MiB).

SHA-256: `8906EEC8387171300870A9868F8685992E6BECD86014347D53BC53B8F99D245E`.

The temporary installed copy was removed after the smoke test. The installer and compiled executable remain in the build output. Build outputs, generated Tauri schemas/permissions and dependencies are ignored by Git; `src-tauri/Cargo.lock` records the resolved Rust dependency versions.

The initial broad C++ workload installation failed with insufficient space on C: (0x80070070, 6.71 GB required). Installing only the compiler and SDK with the compiler destination on D: succeeded. The smoke script was fixed for PowerShell 5.1 and now identifies the engine through the desktop process tree, supporting Windows application-cache path redirection.

To rebuild, open a fresh PowerShell session and run `./scripts/build-windows.ps1 -SkipSetup`; dependencies are installed in `.venv` and `node_modules`. To repeat installed lifecycle verification, run `./scripts/smoke-installed-windows.ps1`. The smoke test temporarily installs and then uninstalls its own copy.

These checks used offline fixtures and mocked provider transport. Desktop visual workflows, full frontend IPC behavior, WebView2 provisioning on a machine without the runtime, Windows credential storage, external model endpoints and account integrations remain unverified. No real order was submitted.

## Desktop production hardening checkpoint - 29 October 2026

Release direction: Windows desktop, both Alpaca and Kraken, built-in deterministic model initially. Preserve the independent HTTP-model registry for a future replacement model.

The latest installer includes a disconnected-engine screen and reconnection action. Desktop workspace commands reject disconnected execution rather than using local demo mutations. Snapshot transport failure returns the desktop to the connection screen. A regression verifies blocked commands leave the workspace unchanged.

Alpaca equity scheduling and manual order confirmation now require an explicitly open provider market clock. Closed, missing and invalid session states block submission before claiming the proposal. Crypto pairs retain 24/7 behavior. Regression verifies the proposal can still be confirmed once the market opens.

The updated package passed silent installation, desktop launch, loopback engine health/authentication, normal shutdown of both bundled-engine processes, and silent uninstall again.

This is still a development checkpoint. The desktop automation tools failed to initialize with `failed to write kernel assets: The system cannot find the path specified`, preventing visual desktop workflow verification. Provider account access and Windows credential-vault operations have not been verified with user accounts. Kraken live execution remains blocked by incomplete reconciliation. Other functional gaps remain in `COMPLETENESS-AUDIT.md`; these fixes do not establish production readiness.
