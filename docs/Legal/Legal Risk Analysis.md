---
tags:
  - type/legal
  - status/complete
---
# Legal Risk Analysis

OddsIQ scrapes Tipsport's public-facing REST API without authentication. This documents the legal context and risk assessment.

> **Disclaimer:** This is not legal advice. Consult a Czech lawyer if scaling beyond personal use or if Tipsport contacts you.

## What the System Does

- Sends HTTP requests to `https://www.tipsport.cz/rest/offer/v2/offer` -- the same endpoint the Tipsport website's JavaScript frontend calls to display odds to any visitor.
- No login, no session cookies with user credentials, no authenticated endpoints. The data accessed is publicly visible to anyone who opens tipsport.cz in a browser.
- Request frequency uses a single-user polling pattern: one request per market per poll cycle, session reuse, and no mass scraping.
- No data is republished, resold, or redistributed. It's consumed locally for personal analysis.

### Scope of this analysis

This document covers **Phase 1 operations only** -- scraping public odds data. It does NOT cover automated bet placement, which is a separate planned feature ([[P5 - Auto-Betting]]) with a different risk profile. The auto-betting section below summarizes the additional risk surface; a full legal reassessment should be done before P5 implementation.

**Single-book reality.** The pipeline now scrapes only Tipsport. The multi-book era (Betano, Allwyn, Fortuna) was archived on 2026-06-02 in the multi-sport / live-first refactor (recoverable from the pre-release history). The analysis below is written around Tipsport — the sole soft book — and the public-endpoint / factual-data reasoning is unchanged from the original single-book baseline.

## Czech Legal Context

### Copyright Act (zakon c. 121/2000 Sb.)

Protects original creative works. Odds data (numerical facts about betting lines) is not copyrightable -- it's factual data, not creative expression. Database rights (sui generis) under EU Directive 96/9/EC could theoretically protect a "substantial part" of a database, but querying a few dozen odds per session across 15 games is not a substantial extraction.

### Criminal Code (zakon c. 40/2009 Sb., SS 230)

Criminalizes unauthorized access to computer systems. Accessing a public API endpoint without authentication is not "unauthorized access" -- the endpoint is designed to serve data to any HTTP client. No login bypass, no credential stuffing, no exploitation of vulnerabilities.

### Tipsport Terms of Service

Tipsport's ToS likely prohibit automated access (most bookmaker ToS do). Violating ToS is a civil matter (breach of contract), not criminal. Enforcement typically means account restriction or IP blocking, not legal action -- especially against a single user running a personal tool.

### GDPR

Not applicable. OddsIQ collects odds data (prices), not personal data.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Tipsport blocks IP | Medium (if pattern detected) | Low -- VPN or wait | Single-user polling pattern, session reuse, smoke checks |
| Tipsport sends cease-and-desist | Very low | Low -- comply and stop | No commercial use, no redistribution |
| Criminal prosecution | Negligible | N/A | No unauthorized access, no law broken |
| Cloudflare escalates blocking | Medium | Medium -- pipeline stops | Playwright fallback, session refresh |

## Conclusion

The legal risk for Phase 1 operations (odds scraping) is low. The system accesses publicly available data at a polite rate for personal use. The primary risk is operational (IP blocking, Cloudflare escalation), not legal.

## Auto-Betting ([[P5 - Auto-Betting]]) Risk Addendum

Auto-placement changes the legal picture materially less than it changes the **operational** picture. Summary:

- **Legal status unchanged.** Automated bet placement is a violation of bookmaker ToS (civil contract breach), not unauthorized access or criminal activity. Czech criminal law does not criminalize automating actions on an account you legitimately own.
- **ToS violation is the core risk.** Tipsport (the current sole target book, and any book added in P5 planning) almost certainly prohibits automated placement. Enforcement is account-level, not legal -- expected response is account closure and void of unplayed winnings, not lawsuits. (The multi-book era — Fortuna, Betano — was archived 2026-06-02; P5 book targeting will be re-scoped at P5 commit time.)
- **Deposits should remain safe.** Czech gambling law (zakon c. 186/2016 Sb.) generally protects player deposits even in account disputes, but ToS fine print varies -- confirm per-book before automating.
- **GDPR unaffected.** Auto-placement reads and writes your own account data. No third-party personal data is collected or transmitted.
- **Implementation choice matters for detection risk, not legal risk.** The Chrome-extension-in-own-browser approach (vs headless automation or reverse-engineered API) does not change the legal analysis -- it's still contractual violation under ToS. It reduces the *detection* likelihood, which reduces the *account-loss* risk, which is the primary concern.

### Residual risk table (P5 addendum)

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Bookmaker detects automation, bans account | Medium over months | High -- account forfeiture + void unplayed winnings | Own-browser extension; pre-flight heartbeat; take-best rule (book scope to be re-decided at P5 commit) |
| Bookmaker withholds deposit | Low | High | Review each book's ToS before enabling auto-bet for that book |
| Czech Gambling Authority flags the account | Very low | Low -- automation of your own account isn't regulated | Account stays under personal limits / tax reporting |
| Legal action from bookmaker | Negligible | N/A | No precedent for civil suit against a single-account user for ToS violation |

**Recommendation for P5 readiness:** Before enabling auto-bet on any book, read that book's current ToS (it changes), confirm the deposit protection assumption, and accept that account closure is a realistic months-timeline outcome. Treat each book's account as a **consumable resource**, not a permanent fixture.

## Related

- [[TipsportScraper]] -- the scraper that accesses Tipsport's API
- [[CloudflareSession]] -- Cloudflare bypass mechanism
- [[Architecture Decisions]] -- design choices that minimize legal exposure
- [[P5 - Auto-Betting]] -- the planned auto-placement feature and its risk-accepted list
- Tipsport ban / operational fallback — open question, deferred to phase-commit time
