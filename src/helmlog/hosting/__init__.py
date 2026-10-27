"""Multi-boat hosted web server (docs/specs/websitehosting.md).

One review-only HelmLog web app per boat, each bound to that boat's own SQLite
file, behind a dispatcher that picks the boat from the request's Host header.
Nothing here touches hardware: the per-boat apps are the ordinary
``create_app()`` in hosted mode.
"""
