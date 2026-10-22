# External + repo documentation scan
Repo learnings (style.css comments document WebKitGTK raster-layer decisions):
- style.css:753/1168/1250/1339/1364/3+  note WebKitGTK blink/rasterize diseases of
  backdrop-filter and opacity/transform layers — this codebase already knows
  backdrop-filter surfaces re-rasterize per frame and shift layers, and uses
  will-change/backface-visibility pitfalls accordingly.
External (Web sources):
- WebKitGTK 2.54 release notes 2026-09-16: new Skia web-process compositor replaces
  TextureMapper; escape hatch WEBKIT_USE_SKIA_FOR_COMPOSITION=0 still available in 2.54.
- Igalia Periodical #76 (2026): "visible pixel snapping ... nearest-neighbour sampling
  for backing-store tiles when a layer's transform doesn't map 1:1" fixed pre-2.54.0.
- WebKitGTK 2.54.1 (2026-10-02): "Do not rasterize the part of a tile that the clip
  drops", "Skip non-composited frames when damage is empty", "Align the filter surface
  with the device pixel grid", "Fix SkiaCompositingLayer filter rendering under
  transforms" — all present in this machine's 2.54.1.
- bugs.webkit.org #269953: "[GTK] backdrop-filter blur produces shaking, pixelated
  artifacts worse for larger blurs" — still NEW after 2.54.x: strongest match for
  mechanism 1/5 (rectangular tile artifacts under the viewer canvas).
- docs.webkit.org "Ports/WebKitGTK ... Graphics": canvas backing-store 512x512 tiles,
  rectangular dirty-region updates make canvas rect-seams structural.
- bugs.kde.org #521521: Plasma 6.6.5 NVIDIA ghosting — not matching AMD/amdgpu here;
  secondary only.
Verification sequence if resumed: B (backdrop off) -> C (samples off) -> if both
needed, pin WEBKIT_USE_SKIA_FOR_COMPOSITION=0 run; each uses the same window size.
