"""ChatGPT Ads integration — the OpenAI Advertiser API.

ChatGPT Ads (OpenAI Ads Manager, ads.openai.com) is AdLoop's third ad
platform. It has its own REST API (api.ads.openai.com/v1) and no Python SDK,
so this package carries the stack the same way the Reddit package does:
the authenticated HTTP wrapper (``client``) and the read and diagnostic
tools (``read``).

Authentication is simpler than Reddit's: an Advertiser API key created in
Ads Manager → Settings → API keys, sent as a bearer token. Each key belongs
to exactly one ad account.
"""
