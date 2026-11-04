---
tags:
  - type/legal
  - status/complete
---
# Legal Risk Analysis

OddsIQ reads odds from Tipsport's public offer API. When the optional betting engine is enabled, it also places bets on the operator's own Tipsport account through a browser extension. This page records the legal context and the risks as I understand them.

> **Disclaimer:** This is not legal advice. Consult a Czech lawyer before running this beyond personal use, or if Tipsport makes contact.

## What the System Does

### Odds collection

- Sends requests to `https://www.tipsport.cz/rest/offer/v2/offer`, the endpoint the Tipsport website's own frontend calls to show odds to any visitor.
- Does not log in. The scraper uses an anonymous browser session, the same kind any visitor gets, and never calls account endpoints.
- Gets past Cloudflare's bot check. A headless Chromium solves the JavaScript challenge, and later requests present a Chrome-like TLS fingerprint through curl_cffi. See [[CloudflareSession]].
- Polls at a single-user rate: one moneyline request per sport per poll cycle, with session reuse.
- Does not republish, resell or redistribute anything. The data is used locally for personal analysis.

### Bet placement (optional, off by default)

- The betting engine shipped on 2026-08-09. It does nothing unless `BETTING_ENABLED` is set, the engine is armed, and the separate confirm switch is on.
- It drives a real Chrome for Testing window, running under Xvfb in a container, that is logged into the operator's own account. The operator logs in by hand over a tunnelled VNC session. The system never stores or types the account password; the session lives in the browser profile and is renewed by Tipsport's own remembered-session login.
- The extension clicks and types on the Tipsport page through the Chrome DevTools protocol, so the page receives input events like a person's.
- Ceilings set in the environment cap the stake, bets per day, total open stakes and daily loss. See [[P5 - Auto-Betting]].

## Czech Legal Context

### Copyright Act (zakon c. 121/2000 Sb.)

Protects original creative works. Odds data (numerical facts about betting lines) is not copyrightable: it is factual data, not creative expression. Database rights (sui generis) under EU Directive 96/9/EC could in theory protect a "substantial part" of a database, but querying a few dozen odds per poll across a day's games is not a substantial extraction.

### Criminal Code (zakon c. 40/2009 Sb., § 230)

Criminalizes unauthorized access to computer systems. Reading a public API endpoint that serves data to any visitor is not obviously "unauthorized access", and the system involves no credential stuffing and no exploitation of vulnerabilities.

**Open question:** the scraper deliberately gets past Cloudflare's bot check. Whether that counts as overcoming a security measure under § 230 is not something this page can settle. It is the first question to put to a lawyer.

### Tipsport Terms of Service

I have not reviewed Tipsport's current terms. They very likely prohibit both automated access and automated bet placement, as most bookmakers' terms do. Breaching terms of service is a civil, contractual matter. The expected response is account restriction or closure, possibly with unsettled bets voided, or IP blocking, rather than legal action against a single user of a personal tool.

### Gambling Act (zakon c. 186/2016 Sb.)

The working assumption is that player deposits stay protected even in an account dispute. This is unverified, and the terms' fine print decides it in practice. Check it before enabling the betting engine.

### GDPR

Not applicable to odds data, which contains no personal data. The betting engine reads and writes only the operator's own account; no third-party personal data is collected or transmitted.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Tipsport blocks the IP or tightens its bot protection | Medium | Medium: collection stops | Single-user polling, session reuse, the `tipsport-smoke` pre-flight check |
| Tipsport detects automated betting and closes the account | Medium over months | High: account closed, unsettled bets possibly voided | Betting is off by default; small flat stakes; hard caps; treat the account as consumable |
| Deposit withheld in a dispute | Low | High | Read the current terms before enabling betting |
| Cease-and-desist letter | Very low | Low: comply and stop | No commercial use, no redistribution |
| Criminal exposure | Believed low, unverified (see the Cloudflare question) | High | Legal advice before any wider use |
| Gambling regulator interest | Very low | Low | Personal account within personal limits and tax reporting |

## Conclusion

The main risks are contractual and operational: losing the Tipsport account, or being blocked. The open legal question is the Cloudflare handling. Before enabling the betting engine, or using the tool beyond personal research, read Tipsport's current terms and get advice on that question.

## Related

- [[TipsportScraper]]: the scraper that reads Tipsport's offer API
- [[CloudflareSession]]: how the Cloudflare challenge is handled
- [[P5 - Auto-Betting]]: the auto-betting roadmap and its accepted risks
- [[Architecture Decisions]]: design decisions and their reasons
