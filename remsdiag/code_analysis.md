# Static code analysis (view render path)
Top candidate 1 — REPAINT DAMAGE TILES UNDER backdrop-filter
  .gallery-viewer CSS (web/style.css:1099-1315) carries backdrop-filter: blur(28px)
  saturate(160%); canvas repaints at 6099-6157 (script.js) go clearRect + one drawImage;
  the WebKitGTK 2.54 Skia web-process compositor stores canvas in rectangular tiles and
  propagates damage rectangles under the filter surface. Stage swaps
  (viewer open ~0.0s + display copy, stage-2 original ~0.5-4s, sample build later)
  create 2-3 windows in which old/new sharpness regions coexist inside tile rectangles.

Candidate 2 — createImageBitmap sample defects: buildViewerSample
  (script.js ~5055-5065) uses resizeQuality 'high' for 2840x4125 -> 947x1375 and
  473x687; upscaled every paint when fitScale<=1/3 or <=1/6 — anchoring seams to the
  lace area (high-frequency white feature is the maximal-amplifier).
Candidate 3 — stage-2 reveal: viewerResource.generation + 450 ms swap
  (script.js 5508-5620) that repaints the same fit box with sharper pixels — visible if
  the repaint region intersects a cached filter layer (mechanism 1 precondition).
Candidate 4 — PIL display copy: Rems_Dl.py 2156-2193 _make_display_copy uses
  Image.Resampling.BILINEAR, JPEG quality=88 4:2:0; not refuted at disk level (clean
  1:1 crops) but could still surface post-decode through the canvas re-scale.
Candidate 5 — imageSmoothingQuality 'high' handling on WebKitGTK for the minified
  original: moire rather than rectangle; zoomed-only.
Refuted — Math.floor source snapping and DPR rounding (script.js 6106-6140): the 8px
  margin / image-edge clamp keeps both boundaries outside the view, except at extreme
  viewport-edge pan (artifact follows viewport, NOT image content).
