# Rem 5.3 - Rems Dl Release Notes

We are thrilled to present **Rem 5.3 (Rems Dl v5.3.0)**! This release resolves the critical PyInstaller runtime metadata issue on Windows portable and installer builds, introduces interactive gallery multi-selection with bulk operations, cross-platform native clipboard file/image copying, desktop folder picker integration, deduplication synchronization, and comprehensive layout refinements.

---

## 🚀 What's New in Rem 5.3

### 🛠️ PyInstaller Metadata Fix (`rule34Py` & Frozen Bundles)
- **Resolved Startup Crash:** Fixed `importlib.metadata.PackageNotFoundError: No package metadata was found for rule34Py` which previously prevented frozen executable builds (installer and portable) from launching.
- **Automated Metadata Bundling:** Configured `copy_metadata` in `Rems_Dl.spec` to automatically package required package distribution metadata into standalone releases.
- **Custom Hooks & Runtime Safeguards:** Added dedicated hook `hooks/hook-rule34Py.py` and runtime hook `hooks/rthook-metadata.py` with multi-layered runtime fallbacks in Python code to guarantee reliable startup across all environments.

### 🎨 Gallery Multi-Select & Bulk Actions
- **Interactive Multi-Select Mode:** Easily select images across the current page using click, drag-paint, or `Ctrl+A`.
- **Bulk Clipboard Copy:** Copy multiple selected image files or single full-resolution images directly to the system clipboard.
- **Bulk Disk Deletion:** Delete selected gallery items from disk with immediate deduplication store synchronization.

### 📋 Cross-Platform Native Clipboard Integration
- System-level clipboard support for image bitmaps and file paths across Windows, macOS, and Linux without external CLI dependencies.

### 📁 Desktop Folder Picker
- Integrated native directory selection dialog via `/api/folder` and quick-access button to open download directories directly in the OS file explorer.

### ⚡ Deduplication & Startup Performance
- Dedup database cleanup is automatically triggered when images are removed from the gallery, preventing orphaned hash entries.
- Streamlined startup rescan routines for faster boot times.

### 🖼️ Gallery & Layout Polish
- Refined multi-column card spacing and reduced bottom gap between gallery grid and pagination controls.
- Decoded entity-encoded tags for booru sites (Gelbooru & Safebooru).
- Visual enhancements for toasts, tag scrollbars, and media icons.

---

## 📦 Downloads & Packages

| File | Platform | Type | Description |
|------|----------|------|-------------|
| **`Rems-Dl-Windows-x64-Setup.exe`** | Windows (x64) | Installer | **Recommended for Windows.** Full setup with automatic Visual C++ runtime detection & installation. |
| **`Rems-Dl-Windows-x64-Portable.zip`** | Windows (x64) | Portable Archive | Standalone portable folder. Extract and double-click `Rems_Dl.exe`. |
| **`Rems-Dl-Linux-x86_64.tar.gz`** | Linux (x86_64) | Standalone Archive | Standalone Linux application with desktop integration files. |
