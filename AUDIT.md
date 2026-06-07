KALSHIBOT COMPREHENSIVE AUDIT - ISSUES TO FIX
==============================================

CRITICAL ISSUES
===============

1. DEBUG_LOGGING enabled in production
   - File: app.py:39
   - Issue: DEBUG_LOGGING = True causes excessive console output
   - Impact: Slows down execution, pollutes logs
   - Fix: Set to False or make environment-based

2. Silent error handling throughout
   - Issue: Many try/except blocks catch all errors and do nothing
   - Impact: Hard to debug issues when they occur
   - Examples: app.py line 354, 402, 410, 595, 647, etc
   - Fix: Add proper error logging

3. No validation on API responses
   - Issue: Code assumes API always returns expected fields
   - Impact: Crashes on unexpected API responses
   - Example: kalshi_get calls don't validate response structure
   - Fix: Add schema validation for all API responses

4. Race conditions in position tracking
   - Issue: _recently_sold dict and tracked dict can be out of sync
   - Impact: Positions may show incorrectly or get stuck
   - Fix: Add locking mechanism

5. Monitor thread can get stuck
   - File: app.py _monitor() function
   - Issue: If Kalshi API hangs, monitor locks waiting
   - Impact: No auto-sells while hung
   - Fix: Add timeout to all Kalshi API calls in monitor


BACKEND (app.py) ISSUES
=======================

6. Rate limiter can starve scan loop
   - Issue: All API calls share one global _api_lock
   - Impact: Scan competes with portfolio enrichment for lock
   - Current: 0.5s base delay between calls
   - Fix: Consider separate rate limiters per endpoint type

7. Market cache has no invalidation strategy
   - Issue: 60s TTL might be stale during volatile markets
   - Impact: Buy/sell decisions based on old prices
   - Fix: Add price divergence check to invalidate early

8. Settings not validated before use
   - Issue: buy_settings, sell_strategy parsed but never validated
   - Impact: Bad settings silently used wrong way
   - Fix: Add schema validation on /api/strategy and /api/buy-settings

9. No check for negative quantities
   - Issue: Code doesn't prevent qty < 0 in some paths
   - Impact: Could create invalid orders
   - Fix: Add assertion checks

10. Sell strategy mode mismatch possible
    - Issue: Frontend sets mode but backend doesn't validate it's valid
    - Impact: Silent fallback to default behavior
    - Fix: Validate mode in /api/strategy endpoint

11. Portfolio enrichment times out silently
    - Issue: If /api/enrich-positions times out, data stays blank
    - Impact: User thinks position has no value
    - Fix: Cache last-known values as fallback


FRONTEND (index.html) ISSUES
=============================

12. Auto-mode timer can get out of sync
    - Issue: If page tab is hidden, timer still counts
    - Impact: Bot thinks time passed that didn't
    - Fix: Pause timer when page hidden

13. Settings can be partially saved
    - Issue: saveSettings() saves to localStorage but not always to backend
    - Impact: Settings lost on page reload if backend wasn't updated
    - Fix: Ensure backend always gets settings when tab switches

14. No confirmation before destructive actions
    - Issue: "Sell All" button has no confirmation
    - Impact: User could accidentally sell everything
    - Fix: Add confirmation modal

15. Timers not cleaned up on tab switch
    - Issue: Some intervals might persist when switching tabs
    - Impact: Memory leak over long sessions
    - Fix: Audit all setInterval calls in switchTab()

16. Position sorting can fail silently
    - Issue: If sort key not found, falls back without warning
    - Impact: User might think data is sorted when it's not
    - Fix: Add indication of actual sort status

17. Voice synthesis may not have voices loaded
    - Issue: speechSynthesis.getVoices() sometimes returns empty array
    - Impact: Voice announcements won't work on first call
    - Fix: Use onvoiceschanged event to load voices

18. Snooze button has no visual feedback of remaining time
    - Issue: Snooze state only updates on click
    - Impact: User doesn't know if voice is still muted
    - Fix: Add countdown display to snooze button


DATA/LOGIC ISSUES
=================

19. P&L calculation excludes unrealized losses
    - Issue: Only shows settled P&L, not current unrealized changes
    - Impact: User sees zero P&L while positions are down
    - Fix: Add unrealized P&L to calculations (optional, complex)

20. Position "Max Profit" calculation may be wrong
    - Issue: Assumes contracts resolve at $0.99 or $0.01
    - Impact: Shows wrong max profit for some positions
    - Fix: Use actual max/min from Kalshi API

21. Cost calculation doesn't account for fees
    - Issue: Shows cost without Kalshi fees
    - Impact: Actual profit is lower than shown
    - Fix: Deduct 1¢ per contract from revenue

22. Lotto history totals only show settled positions
    - Issue: "Total" row doesn't include open positions
    - Impact: User thinks profit is lower than actual potential
    - Fix: Add "current value" row for open positions


PERFORMANCE ISSUES
==================

23. Enrichment batches still too large
    - Issue: 100 tickers per batch can still timeout
    - Impact: Position values blank for users with 200+ positions
    - Fix: Reduce batch size to 50, consider streaming

24. Poll times tracking has memory leak
    - Issue: _pollTimes object keeps growing forever
    - Impact: Memory usage increases over long sessions
    - Fix: Trim old entries (>7 days)

25. Snapshots file can grow unbounded
    - Issue: Portfolio snapshots added every 5 minutes forever
    - Impact: File size grows, loading slow
    - Fix: Trim snapshots older than 30 days on startup

26. Recent buys list grows unbounded
    - Issue: _recentBuys array never cleared
    - Impact: Memory leak after bot runs for weeks
    - Fix: Cap at 100 most recent, or trim old

27. Coach API response could be huge
    - Issue: No limit on rows returned
    - Impact: Slow page load if user has 1000+ positions
    - Fix: Paginate or limit response


MISSING FEATURES / EDGE CASES
==============================

28. No way to view full order history
    - Issue: Only last 20 sales shown in ticker strip
    - Impact: User can't see older trades
    - Fix: Add "View All Sales" history page

29. Settings profiles not working
    - Issue: T1/T2 profile separation mentioned but incomplete
    - Impact: Settings might not separate properly
    - Fix: Verify profile isolation works correctly

30. No undo for manual sells
    - Issue: If user accidentally clicks sell, it's permanent
    - Impact: Loss of money
    - Fix: Add 5-second undo window with countdown

31. Notifications only visual/audio
    - Issue: No browser push notifications if tab is hidden
    - Impact: User might miss important alerts
    - Fix: Add Web Notifications API integration

32. No circuit breaker for repeated failures
    - Issue: If API is down, scan keeps trying forever
    - Impact: Wastes resources, fills logs
    - Fix: Add exponential backoff, then pause


STABILITY / RELIABILITY
=======================

33. Bot can crash if Kalshi API structure changes
    - Issue: Code assumes specific JSON structure
    - Impact: Bot stops working after Kalshi update
    - Fix: Add version check for API responses

34. No health check on startup
    - Issue: Bot starts even if API unreachable
    - Impact: User thinks bot is working when it's not
    - Fix: Health check on /api/portfolio before allowing start

35. Manager process can deadlock
    - Issue: If Flask hangs, manager keeps spawning processes
    - Impact: Resource exhaustion
    - Fix: Add process health monitor to manager

36. Settings can corrupt if saved during shutdown
    - Issue: No atomic file writes
    - Impact: Settings lost on crash
    - Fix: Use temp file + rename pattern

37. Position tracking can get out of sync with Kalshi
    - Issue: If settlement propagation delays, positions stuck "open"
    - Impact: Shows false open positions
    - Fix: Verify against Kalshi settlements API


UI/UX ISSUES
============

38. Position numbers change too frequently
    - Issue: Current price refreshes can cause visual flutter
    - Impact: Hard to read while watching
    - Fix: Batch updates or debounce renders

39. "Loading..." states confusing
    - Issue: Many endpoints show Loading but user doesn't know which
    - Impact: User doesn't know if waiting is normal
    - Fix: Add spinner + endpoint name

40. Sell button location inconsistent
    - Issue: Different widths on different tabs
    - Impact: Muscle memory fails, wrong clicks
    - Fix: Make consistent or sticky

41. No keyboard shortcuts
    - Issue: All interaction is mouse/touch only
    - Impact: Slow to use on keyboard
    - Fix: Add Ctrl+S to save, Ctrl+Enter to buy, etc

42. Settings not grouped logically
    - Issue: Many settings scattered on same form
    - Impact: User gets lost
    - Fix: Use collapsible sections or separate pages

43. Market titles sometimes show raw tickers
    - Issue: If prettification fails, shows "KXBTC-26JUN0717-T63249"
    - Impact: Confusing and ugly
    - Fix: Always have human-readable fallback

44. No dark mode toggle
    - Issue: App is always dark (intentional but inflexible)
    - Impact: User preference not respected
    - Fix: Add theme toggle if desired


TESTING / VALIDATION
====================

45. No unit tests
    - Issue: No automated test coverage
    - Impact: Regressions go undetected
    - Fix: Add pytest tests for critical functions

46. No integration tests
    - Issue: No end-to-end tests
    - Impact: Breaking changes in API integration go undetected
    - Fix: Mock Kalshi API and test full flows

47. No input validation tests
    - Issue: Never tested with malformed data
    - Impact: Crashes on unexpected input
    - Fix: Add fuzzing/property tests


DOCUMENTATION
==============

48. No API documentation
    - Issue: Endpoints not documented
    - Impact: Hard to extend/integrate
    - Fix: Add OpenAPI/Swagger docs

49. Monitor loop behavior undocumented
    - Issue: Monitor timing and conditions unclear
    - Impact: Hard to debug monitor issues
    - Fix: Document timing diagram

50. Rate limiter behavior undocumented
    - Issue: Not clear what the rate limits are
    - Impact: Hard to understand performance
    - Fix: Document rate limit strategy


SECURITY ISSUES
===============

51. API keys in logs
    - Issue: If DEBUG_LOGGING enabled, API key visible
    - Impact: Private key compromise risk
    - Fix: Redact keys in logs always

52. No HTTPS enforcement
    - Issue: Works over HTTP
    - Impact: Keys could be sniffed
    - Fix: Force HTTPS in production

53. No rate limiting on API endpoints
    - Issue: Anyone can hammer endpoints
    - Impact: DoS vulnerability
    - Fix: Add per-IP rate limits


MINOR / POLISH
==============

54. Typos in comments/strings
    - Fix: Audit and correct

55. Inconsistent spacing/formatting
    - Fix: Use auto-formatter

56. Some buttons don't have hover effects
    - Fix: Add consistent hover states

57. Mobile layout issues on some tabs
    - Fix: Test on actual phone sizes


