"""Microsoft Advertising (Bing Ads) integration — REST API v13.

AdLoop's fourth ad platform. Microsoft is retiring the SOAP API on
2027-01-31, so this package talks to the REST API directly over
``requests`` (no SDK): Campaign Management, Customer Management and
Reporting. It carries the same layers as the Reddit package: OAuth
(``auth``), the authenticated HTTP wrapper and report runner (``client``),
read tools (``read``) and draft/preflight/apply executors (``write``) that
plug into the shared ``confirm_and_apply`` gate.

Every call needs three things: an OAuth access token for a user with access
to the account (Microsoft identity platform, scope msads.manage), a
developer token, and the CustomerId / CustomerAccountId headers.
"""
