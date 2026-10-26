# Rem 5.6 - Rems Dl Release Notes

We are thrilled to present **Rem 5.6 (Rems Dl v5.6.0)**! This major release introduces the revolutionary **JPEG XL (JXL)** conversion pipeline and JXL Lab, enhanced headless/server and Docker operation, advanced rate-limiting safeguards, desktop integration automation on Linux, Windows installer enhancements, folder rename safety, and full cross-platform stability.

---

## 🚀 What's New in Rem 5.6

### 🖼️ Next-Gen Image Format: JPEG XL (JXL) Pipeline
- **Automatic JXL Conversion:** Optionally auto-convert downloaded images directly to modern JPEG XL format, drastically slashing file sizes while preserving visual quality.
- **JXL Lab:** Convert existing folders, galleries, or individual images to JXL with configurable compression effort and lossless/lossy parameters.
- **JXL Wallpaper & Preview Support:** Native JXL display and picker support in gallery and desktop wallpaper selection.

### 🌐 Hardened Headless, Server & Docker Operation
- **Persistent Headless Daemon:** In headless, server (`--headless`, `--server`), or Docker environments, closing browser tabs will **not** terminate the process.
- **Docker Image Updates:** Official container builds on GHCR with full dependency packaging (including `PySocks` and `pinterest-dl`).
- **Secure Thumbnail Proxy:** Thread-safe notification and thumbnail caching with strict SSRF filtering and allowed host verification.

### 🛡️ Scraper Resilience & Rate Limiting
- **Automated 429 Protection:** Rate limiters and backoff logic to prevent IP bans and throttling across Anime-DL, Zerochan, and boorus.
- **Tag Cache & Recategorization:** Gelbooru tag category caching and normalization for fast, accurate search and filtering.
- **Retired Inactive Scrapers:** Removed deprecated Waifu.im scraper.

### 💻 Platform & Packaging Improvements
- **Windows Installer & Portable:** Inno Setup installer automation in CI with bundled Visual C++ Redistributable (`vc_redist.x64.exe`) inside the Portable Zip.
- **Linux Desktop Integration:** Added `install-desktop.sh` to automatically install application launcher shortcuts and high-resolution icons to the desktop environment.
- **Robust Path & Clipboard Handling:** Enhanced PowerShell and macOS clipboard handling with literal path escaping, avoiding issues with special characters.
- **Safe Folder Rename Policy:** Two-step rename handling for case-only changes on case-insensitive filesystems (Windows/macOS) to prevent accidental data loss.

---

## 📦 Downloads & Packages

| File | Platform | Type | Description |
|------|----------|------|-------------|
| **`Rems-Dl-Windows-x64-Setup.exe`** | Windows (x64) | Installer | **Recommended for Windows.** Full setup with automatic Visual C++ runtime detection & installation. |
| **`Rems-Dl-Windows-x64-Portable.zip`** | Windows (x64) | Portable Archive | Standalone portable folder with bundled `vc_redist.x64.exe`. Extract and launch `Rems_Dl.exe`. |
| **`Rems-Dl-Linux-x86_64.tar.gz`** | Linux (x86_64) | Standalone Archive | Standalone Linux application with `install-desktop.sh` integration script. |
| **`ghcr.io/remlover-dev/rems-dl:latest`** | Linux (Docker) | Container Image | Official container image for zero-host-dependency headless server deployment. |
