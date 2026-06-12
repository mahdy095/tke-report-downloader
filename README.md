---
title: TKE Service Report Downloader
emoji: 📥
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# TKE Service Report Downloader

Drop a saved TKE customer-portal **Orders** page (HTML), and the app logs into
the portal headlessly, opens every order, downloads all service-report PDFs, and
returns them as a single ZIP.

Runs as a Docker Space so the headless Chromium browser has the system libraries
and process permissions it needs. Portal credentials are supplied via the Space's
**Settings → Variables and secrets** as `TKE_USERNAME` and `TKE_PASSWORD`.

See [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) for architecture and the full
debugging history.
