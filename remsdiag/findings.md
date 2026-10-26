# 11421060 viewer render artifact — findings (session 2026-10-20, marker REMSDIAG-20261020-a1)
Probe: view of `Danbooru/Maimaiuxu/Sensitive/Images/11421060(.jpg -> .jxl)` at viewer open; canvas is
a Gwenview-style multiresolution renderer (web/script.js) inside `.gallery-viewer` with
`backdrop-filter: blur(28px) saturate(160%)` (web/style.css:1103-1104). User-visible symptom:
a RECTANGULAR sharpness/texture artifact around the white lace armour, at fit and zoomed.

## Byte & endpoint evidence
- Pre-conversion source (.jpg): 2840x4125 JPEG, 12,441,944 B, sha256 7795eeaa0c942b84…1ba81.
  1:1 lace-region crop (1150-1850 x 1850-2650) clean; no rectangle.
- /api/gallery/file/...jpg (200 image/jpeg): byte-identical to disk.
- /api/gallery/display/...jpg (200 image/jpeg, 1763x2560, 742763 B, sha 7c2b5222…a34b2a):
  display-cache dc6e45ea1106065e.jpg pre-existed; moved aside -> regenerated copy bit-identical
  (pixel-diff bbox None) -> entry restored byte-exact.
- POST-session the file was converted to JPEG XL by Rems' own JPEG XL Lab
  (`11421060.jxl`, 9.8 MB, sha b42a9c77…9299b): sidecar record shows
  `encoding:"jpeg"`, `original_name:"11421060.jpg"` -> a lossless JPEG transcode
  (`core/jxl_worker.py:34-53`); original bitstream recoverable via the app's restore path.
- Re-test of current state (headless Flask, no GUI):
  /api/gallery/file/...11421060.jxl -> 200 image/png 16,191,765 B (sha 9052edde…924f) —
  server-side `browser_png` decode for WebKitGTK (Rems_Dl.py `if full.lower().endswith('.jxl')`
  branch, core/jxl_routes.browser_png, cache under database/jxl_display_cache);
  `/display/.jxl` -> 742,763 B, sha 7c2b5222…a34b2a — **bit-identical to the pre-conversion
  display copy**, i.e. pixel data unchanged (BILINEAR long-edge-2560 JPEG reproduction is
  deterministic on identical pixels). Pillow-decode(jxl) vs browser_png bytes: lace-crop
  pixel-hash equal (8486e9a9…ecabf), confirming the jxl holds the same pixels.

## Renderers at play
Stage 1: /display/ fit copy; Stage 2: original bytes after 450 ms + decode, then async
1/3 and 1/6 `createImageBitmap({resizeQuality:'high'})` samples
(script.js 5030-5100). `paintViewer` picks rep by `L.zoom<=1/6` / `<=1/3`, else stage image
(6099-6157); source snap `Math.floor(ix*repScale)`, margin 8/L.zoom;
`imageSmoothingEnabled = L.zoom < 4`, quality 'high'.

## Ranked candidate mechanisms (static code analysis)
1. Rep/swap repaint damage rectangles: canvas repaints (clearRect + big drawImage) live
   UNDER `backdrop-filter: blur(28px)`; WebKitGTK 2.54 runs a new Skia web-process
   compositor with 512x512 tile backing stores — timely repaints mixing old/new sharpness
   read as transient/static rectangles around high-frequency content.
2. `createImageBitmap(resizeQuality:'high')` sample defects at 2840x4125 -> 947x1375 /
   473x687; content-anchored sharpness rectangles when the sample is upscaled (fit +
   the 1/3 zoom band).
3. Stage-2 swap seam: the 450 ms timer + big decode reveal replaces a soft fit view with
   sharper pixels — the STAGE 1/2 boundary can present as a rectangle if a repaint region
   is cached as a filter layer.
4. PIL display-copy BILINEAR + 4:2:0 (quality 88): re-emergence of JPEG block edges after
   WebKit decode + canvas rescale (1:1 crop clean at disk level; cannot fully refute
   without the GUI A/B shots).
5. `imageSmoothingQuality 'high'` behavior on WebKitGTK for the minified original
   (moire-not-rectangle, low confidence).
6. Refuted by math: source-snap floor boundaries and DPR rounding (8px margin /
   image-edge clamp keep them outside viewport).

External: WebKitGTK 2.54.1 already carries the 2.54.0 tile-snap/message-level fixes;
`backdrop-filter` pixelated/shimmer bug (bugs.webkit.org #269953) still open for 2.54.x,
matching mechanism 1/weblay 5; canvas updates are structurally rectangular
(WebKitGTK graphics docs).

## Visual tests A-D and Gwenview — NOT RUN
User stopped on-screen work after the KWin compositor crash (below). No screenshots exist.
Hardware next time (zero compositor scripting): B = disable only .gallery-viewer
backdrop filters; C = `REMSDIAG_BYPASS_DISPLAY` + `REMSDIAG_NO_SAMPLES` (diag hooks in
script.js guarded returns); D = both. Overlay marker REMSDIAG-20261020-a1 shows
stage/URL/decoded dims/rep/DPR.

## Incident for the record
- My KWin scripting window-position probe `c.frameGeometry = Qt.rect(60,60,1400,900)`
  segfaulted kwin_wayland (PID 2342, SIGSEGV in QV4 `StoreProperty`, KWin 6.8.0 /
  Qt 6.12.0; coredumpctl 2026-10-20 20:28:51). Compositor crash closed all clients on the
  user's screen. Script withdrawn; not re-run.
- /tmp/opencode was subsequently wiped; originals of runner.py / kwin_rems.js /
  endpoint_response_*.jpg / REDACTED diagnostics were lost. Do not re-run harness scripts
  in this repo — they aren't here; treat them as LOST (not present).

## Files in this folder
- findings.md (this), env_info.md, code_analysis.md, docs_scan.md
- forensics/: source grid + lace crops from the CURRENT jxl path
- session-stats.txt: hash/sha test history numbers
