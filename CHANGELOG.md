# Changelog

## 1.0.2

- Photo page: Share/Save works in the Home Assistant app again (download fallback when
  the app's web view has no Web Share API; 1.0.1 only showed "Sharing is not available").
- Photo page: no extra gap above the title in the Companion app.

## 1.0.1

- Ship the Trailwatch icon with the integration (`brand/`), shown by Home Assistant
  2026.9+ on the integration and device pages.

## 1.0.0 (first public release)

- Archive, classification (local SpeciesNet server and/or AI Task, stronger AI second
  opinion), phone alerts with quiet labels, per-camera rules and cooldown.
- Requested videos: download, pairing with their photo by clip number,
  classification (server frame sampling, or one ffmpeg frame for AI-only setups).
- Automatic video requests for chosen animals, queued one at a time.
- Trailwatch Review card and the photo page opened by phone alerts
  (Share/Save, Request video, Close).
- Usage sensors: local vs cloud decisions, AI calls, estimated savings and cost.
