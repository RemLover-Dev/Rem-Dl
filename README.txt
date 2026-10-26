Rems-Dl fit-size rendering patch — area-fit-20261020-1

INSTALL
1. Close Rems-Dl completely.
2. Back up the current web/script.js outside the web directory.
3. Replace only web/script.js with the file in this package.
4. Start Rems-Dl normally and open 11421060.jpg at fit (0).
5. Wait for the original to load, then check the lace region. Also check
   zooming, panning, returning to fit, next/previous images, and reopening.

Your local agent only needs to install this file and restart the app. No
compositor scripts, GPU settings, Python edits, CSS edits, or cache deletion
are needed. To undo, restore the backed-up script.js and restart.

This replacement is based on appfiles_prepatch/script.js in remsdiag_full.zip.
The diagnostic-patched script in that ZIP differs only by diagnostic hooks
and test guards; this replacement omits those hooks. Existing diag_overlay.js
and its HTML include can stay in place. Do not use this full replacement if
you have made further application changes since that bundle; apply/review the
included diff against the current code instead.

WHAT CHANGED
At fit-size minification, the app now:
- Reads the active image's native pixels using an unscaled canvas draw.
- Averages pixel coverage in software, including premultiplied alpha, to the
  final physical-pixel dimensions. Filtering normally runs in a Web Worker.
- Caches one fitted bitmap and draws it at 1:1 with an integer pixel origin.
- Cancels superseded work when images or viewport dimensions change.
- Retains the existing renderer while a fitted bitmap is being prepared, and
  for zoomed views. A brief old-looking frame can occur during preparation.
- Prevents cached samples from an original from being selected while a
  different stage image is active.

This bypasses createImageBitmap resizing and canvas minification for the
settled fit view. Original files and server display-copy generation are not
modified. During image loading, the preview and original are filtered
separately as each becomes active.

WHY
Gwenview's RasterImageItem::paint scales the selected visible image with
QImage::scaled before it draws it. Qt's smooth scaler uses area sampling for
minification. The supplied app delegates its last resize to drawImage and
uses createImageBitmap for reduced samples; a browser quality hint does not
establish an equivalent filter. The new fit filter is independently written
from the area-coverage calculation; it does not copy KDE or Qt code.

Reference source reviewed:
https://github.com/KDE/gwenview/blob/master/lib/documentview/rasterimageitem.cpp
https://github.com/KDE/gwenview/blob/master/lib/documentview/rasterimageview.cpp
https://github.com/KDE/gwenview/blob/master/lib/documentview/abstractimageview.cpp
https://github.com/qt/qtbase/blob/dev/src/gui/painting/qimagescale.cpp

VERIFICATION
- node --check web/script.js
- node tests/verify-fit.cjs
- 1,936 fractional-dimension/alpha cases against an independent brute-force
  2-D area integrator, within one channel level.
- High-frequency checkerboard averages uniformly.
- Stage replacement, cache reuse, resize invalidation and cancellation checks.
- Fit blit has no scaling and integer physical coordinates at DPR 1 and 2.
- A stale original sample cannot replace a different active stage image.
- Source image processed at 2840x4125 -> 690x1002 in about 168 ms in a local
  Node run in its native realm. This is not a WebKitGTK timing measurement.

LIMITATION
The exact GPU/engine cause of the rectangle is not proven. This patch removes
browser scaling from the affected settled fit path. WebKitGTK and your desktop
are unavailable here, so visual confirmation in your app is still required.
If the rectangle remains, return a full-resolution screenshot after the
original settles and say whether the appearance changed. Do not ask the local
agent to invent another fix.

CONTENTS
web/script.js             replacement application file
fit-scaling.patch         diff against the bundled prepatch script.js
fit-vs-diagnostic.patch   diff against the bundled diagnostic script.js
tests/verify-fit.cjs      focused algorithm/lifecycle/render-call checks
README.txt               installation and validation notes
