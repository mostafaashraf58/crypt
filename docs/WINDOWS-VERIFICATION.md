# Windows verification - 28-29 October 2026

Verified locally in `D:\crypt` on Windows 11 with Node 24.21.0 and Python 3.13.6.

| Check | Result |
| --- | --- |
| Locked Node dependency installation (`npm ci`) | Passed; audit reported zero vulnerabilities |
| Python dependency installation | Passed using public PyPI after the initial resolution failed |
| TypeScript and Vite production build (`npm run build`) | Passed; Vite reports a JavaScript chunk above 500 kB |
| Frontend logic (`npm run test:frontend`) | 44 passed |
| Backend (`.venv/Scripts/python.exe -m pytest tests -q`) | 59 passed; final packaging run took 6.30 seconds, with one non-fatal pytest cache permission warning |
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

SHA-256: `8A45E51E1167A09CBDC8E09121E5AEAC8223DD56E6504DA0AA93625B228007A3`.

The temporary installed copy was removed after the smoke test. The installer and compiled executable remain in the build output. Build outputs, generated Tauri schemas/permissions and dependencies are ignored by Git; `src-tauri/Cargo.lock` records the resolved Rust dependency versions.

The initial broad C++ workload installation failed with insufficient space on C: (0x80070070, 6.71 GB required). Installing only the compiler and SDK with the compiler destination on D: succeeded. The smoke script was fixed for PowerShell 5.1 and now identifies the engine through the desktop process tree, supporting Windows application-cache path redirection.

To rebuild, open a fresh PowerShell session and run `./scripts/build-windows.ps1 -SkipSetup`; dependencies are installed in `.venv` and `node_modules`. To repeat installed lifecycle verification, run `./scripts/smoke-installed-windows.ps1`. The smoke test temporarily installs and then uninstalls its own copy.

These checks used offline fixtures and mocked provider transport. Desktop visual workflows, full frontend IPC behavior, WebView2 provisioning on a machine without the runtime, Windows credential storage, external model endpoints and account integrations remain unverified. No real order was submitted.
