"""Reddit Ads integration — OAuth, REST client, reads, and safety-gated writes.

Reddit is AdLoop's second ad platform. Unlike the Google services it has
its own OAuth provider (reddit.com), its own REST API (ads-api.reddit.com)
and no vendor SDK, so this package carries the whole stack: token refresh
(``auth``), the authenticated HTTP wrapper (``client``), read tools
(``read``) and the draft/preflight/apply executors (``write``) that plug
into the shared ``confirm_and_apply`` gate.
"""
