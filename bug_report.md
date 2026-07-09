# Bug Report - CoWork Multi-Tenant Booking API

This report documents the confirmed bugs fixed against the preliminary-round PDF contract. It separates bugs present in the original codebase from regressions introduced by an earlier AI-assisted fix pass.

## Verification

- Existing smoke flow: `tests/test_smoke.py`
- Focused regression suite: `tests/test_contract_regressions.py`
- Key scenarios covered: room response contract, report cache invalidation, start-time filtering, refresh-token concurrency, cross-org export, invalid datetime handling, double-booking concurrency, reference uniqueness, and cancel idempotency.

## Original Seeded Bugs

### 1. Access token lifetime was too long

**Files:** `app/auth.py`

**Bug:** `create_access_token()` multiplied `ACCESS_TOKEN_EXPIRE_MINUTES` by 60 before passing it to `timedelta(minutes=...)`, creating access tokens valid for 54,000 seconds instead of 900 seconds.

**Rule violated:** Auth rule: access tokens expire in exactly 900 seconds.

**Fix:** Use `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.

**Verification:** JWT `exp - iat` is exactly 900.

### 2. Logout did not revoke the presented access token

**Files:** `app/auth.py`

**Bug:** Logout stored the token `jti`, but request auth checked whether `sub` was revoked. Logged-out access tokens still worked.

**Rule violated:** Logout immediately invalidates the presented access token.

**Fix:** Check revoked access tokens by `jti`.

**Verification:** A request using the same access token after `/auth/logout` returns 401.

### 3. Refresh tokens were reusable and racy

**Files:** `app/auth.py`, `app/routers/auth.py`

**Bug:** Refresh tokens were not invalidated after use. A first attempted fix added separate check and revoke calls, but concurrent refresh requests could still pass the check before any request revoked the token.

**Rule violated:** Refresh tokens are single-use; reuse returns 401. This must hold under concurrent requests.

**Fix:** Added `consume_token_jti()` to atomically check and revoke a refresh token `jti` under one lock. `/auth/refresh` now uses that helper.

**Verification:** Concurrent refresh attempts for the same token produce exactly one 200 response; the rest return 401.

### 4. Duplicate usernames returned success

**Files:** `app/routers/auth.py`

**Bug:** Registering an existing username in the same org returned the existing user instead of a conflict.

**Rule violated:** Duplicate username within an org returns `409 USERNAME_TAKEN`.

**Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)`. Also catch user insert `IntegrityError` so registration races return the contract error instead of a 500.

**Verification:** Duplicate registration in the same org returns 409 with `USERNAME_TAKEN`.

### 5. Offset datetimes were stripped instead of converted to UTC

**Files:** `app/timeutils.py`

**Bug:** Aware datetimes used `replace(tzinfo=None)`, which dropped the offset without converting the instant to UTC.

**Rule violated:** Input datetimes with UTC offsets must be converted to UTC before storage or comparison.

**Fix:** Use `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

**Verification:** Booking with `+05:00` input is stored and returned at the corresponding UTC time.

### 6. Invalid booking datetimes returned 500

**Files:** `app/timeutils.py`, `app/routers/bookings.py`

**Bug:** `datetime.fromisoformat()` errors escaped from booking creation and produced an internal server error.

**Rule violated:** Invalid booking windows must return `400 INVALID_BOOKING_WINDOW`; service liveness must not break on bad input.

**Fix:** `parse_input_datetime()` catches parse failures and raises `AppError(400, "INVALID_BOOKING_WINDOW", ...)`.

**Verification:** `POST /bookings` with invalid datetime strings returns 400 with `INVALID_BOOKING_WINDOW`.

### 7. Booking window validation allowed invalid starts and durations

**Files:** `app/routers/bookings.py`

**Bug:** Start times up to five minutes in the past were allowed. `end_time <= start_time` and durations shorter than one hour were not fully rejected.

**Rule violated:** Start time must be strictly in the future; end must be after start; duration must be whole hours from 1 to 8.

**Fix:** Reject `start <= now`, `end <= start`, non-whole-hour durations, and durations outside `[1, 8]`.

**Verification:** Past starts, zero/negative windows, fractional durations, and out-of-range durations return `400 INVALID_BOOKING_WINDOW`.

### 8. Back-to-back bookings were rejected

**Files:** `app/routers/bookings.py`

**Bug:** The conflict check used non-strict boundaries, so a booking ending at time T conflicted with one starting at time T.

**Rule violated:** Overlap is `existing.start < new.end AND new.start < existing.end`; back-to-back bookings are allowed.

**Fix:** Query conflicts with strict overlap predicates.

**Verification:** Back-to-back booking succeeds; true overlap returns `409 ROOM_CONFLICT`.

### 9. Booking create/cancel races could violate rules

**Files:** `app/routers/bookings.py`

**Bug:** Conflict, quota, and cancel checks happened before writes without a critical section. Concurrent requests could double-book, exceed quota, or create multiple refund logs.

**Rules violated:** No double-booking, booking quota, refund idempotency, and liveness/concurrency rules.

**Fix:** Added a process-level booking lock around create and cancel check-then-write sections.

**Verification:** Concurrent identical booking requests produce exactly one success; concurrent cancel requests produce one success, the rest `409 ALREADY_CANCELLED`, and exactly one refund log.

### 10. Rate limiting was not thread-safe

**Files:** `app/services/ratelimit.py`

**Bug:** Rolling-window buckets were updated without a lock around the read-modify-write sequence.

**Rule violated:** `POST /bookings` is limited to 20 requests per rolling 60 seconds per user, including concurrent requests.

**Fix:** Guard bucket cleanup, append, and count check with a lock.

**Verification:** The 21st booking attempt in a rolling window returns `429 RATE_LIMITED`; concurrent updates do not under-count.

### 11. Reference codes duplicated under concurrency

**Files:** `app/models.py`, `app/services/reference.py`, `app/routers/bookings.py`

**Bug:** The in-memory reference counter was updated without a lock, allowing duplicate codes in concurrent booking creation.

**Rule violated:** Every booking reference code is unique, including under concurrent creation.

**Fix:** Lock reference-code generation and add a database uniqueness constraint on `Booking.reference_code`.

**Verification:** Concurrent successful bookings receive unique reference codes.

### 12. Booking pagination skipped/repeated items

**Files:** `app/routers/bookings.py`

**Bug:** Bookings were sorted descending, page 1 used `offset(page * limit)`, and the query hardcoded limit 10.

**Rule violated:** `GET /bookings` must sort by ascending `start_time`, tie by ascending `id`, use `offset((page - 1) * limit)`, respect `limit`, and never skip/repeat sequential pages.

**Fix:** Sort ascending by `start_time` and `id`, use the correct offset, and pass through the requested limit.

**Verification:** Pages with limit 2 return item ids `[1,2]`, `[3,4]`, `[5]` for five ordered bookings.

### 13. Booking detail leaked other members' bookings and returned wrong start time

**Files:** `app/routers/bookings.py`

**Bug:** Member visibility was incomplete, and detail responses overwrote `start_time` with `created_at`.

**Rules violated:** Members may read only their own bookings; booking responses must contain the real `start_time`.

**Fix:** Non-admin, non-owner access returns `404 BOOKING_NOT_FOUND`. Removed the `start_time` overwrite.

**Verification:** Members cannot read another user's booking; detail response preserves the real booking start.

### 14. Refund calculation and refund logging were inconsistent

**Files:** `app/routers/bookings.py`, `app/services/refunds.py`

**Bug:** Refund tier boundaries were wrong, `<24h` notice returned 50%, and refund amounts could truncate differently from the response.

**Rule violated:** Refund tiers are `>=48h` 100%, `>=24h` 50%, `<24h` 0%; half-cents round up; response amount equals stored refund log amount.

**Fix:** Compare notice with `timedelta` thresholds, return 0% below 24 hours, use shared half-up rounding, flush the refund log in the same transaction, and return the stored amount.

**Verification:** 50% of 1001 cents returns/stores 501 cents; concurrent cancel creates exactly one refund log.

### 15. Availability/report caches became stale after booking changes

**Files:** `app/routers/bookings.py`

**Bug:** Booking creation did not invalidate usage reports, and cancellation did not invalidate availability.

**Rules violated:** Usage reports and availability must reflect current state immediately.

**Fix:** Invalidate usage-report cache and room-date availability cache after create/cancel.

**Verification:** Reports and availability reflect bookings immediately after create and cancel.

### 16. Room stats drifted under concurrency

**Files:** `app/routers/rooms.py`, `app/services/stats.py`

**Bug:** In-memory stats were updated incrementally and could drift under concurrent create/cancel bursts.

**Rule violated:** Room stats must always match confirmed bookings.

**Fix:** `GET /rooms/{id}/stats` now derives count and revenue directly from confirmed bookings in the database.

**Verification:** Stats match database-derived confirmed booking count and revenue after concurrent activity.

### 17. Notification locks could deadlock

**Files:** `app/services/notifications.py`

**Bug:** Create and cancel notifications acquired locks in opposite orders.

**Rule violated:** No combination of concurrent valid requests may hang the service.

**Fix:** Both notification paths acquire email lock then audit lock.

**Verification:** Concurrent create/cancel activity completes without deadlock.

### 18. Admin export leaked or hid bookings

**Files:** `app/services/export.py`, `app/routers/admin.py`

**Bug:** Original export could fetch bookings for a room without org scoping, and `include_all=false` hid org bookings by filtering to the admin's own user id.

**Rule violated:** Admin export is org-scoped; cross-org resource ids behave as nonexistent.

**Fix:** Export always queries bookings joined through rooms in the admin's org. `room_id` is validated in the route and cross-org ids return `404 ROOM_NOT_FOUND`. The `include_all` flag no longer narrows admin export to the admin user's own bookings.

**Verification:** Admin export includes member bookings in the org; cross-org `room_id` returns 404.

## Regressions From The Earlier AI Fix Pass

### 19. `GET /rooms` response shape was changed

**Files:** `app/routers/rooms.py`

**Bug:** A previous fix changed `GET /rooms` from a plain list to a paginated object `{items, page, limit, total}`.

**Rule violated:** The PDF contract lists room responses as a room object/list contract, with no pagination wrapper for `GET /rooms`.

**Fix:** Restored `GET /rooms` to return a plain list of rooms in the caller's org.

**Verification:** `GET /rooms` returns a JSON array.

### 20. Undocumented `ROOM_NAME_TAKEN` error was added

**Files:** `app/models.py`, `app/routers/rooms.py`

**Bug:** A prior fix added a unique `(org_id, name)` room constraint and returned `409 ROOM_NAME_TAKEN`.

**Rule violated:** The API contract lists exact error codes; `ROOM_NAME_TAKEN` is not part of the contract.

**Fix:** Removed the room-name uniqueness constraint and the `ROOM_NAME_TAKEN` error path.

**Verification:** Creating two same-name rooms in one org succeeds and `GET /rooms` returns both.

### 21. Availability was changed from start-date filtering to overlap filtering

**Files:** `app/routers/rooms.py`

**Bug:** Availability began returning bookings that overlapped the date even if they started on a previous UTC date.

**Rule violated:** Availability returns confirmed bookings starting on that UTC date.

**Fix:** Filter by `Booking.start_time >= day_start` and `< day_end`.

**Verification:** A booking from 23:00 to 01:00 is not returned for the next day's availability.

### 22. Usage report was changed from start-range filtering to overlap filtering

**Files:** `app/routers/admin.py`

**Bug:** Usage report began counting bookings that overlapped the range even if they started before it.

**Rule violated:** Usage report counts confirmed bookings starting in `[from, to]`.

**Fix:** Date-only report ranges use start time in whole inclusive days. ISO datetime report ranges are also accepted and use inclusive exact end time.

**Verification:** A booking from 23:00 to 01:00 is not counted in the next day's date-only report; ISO datetime bounds around a start time count the booking.

### 23. Room creation did not invalidate cached reports

**Files:** `app/routers/rooms.py`

**Bug:** Reports include rooms with zero bookings, but creating a room did not invalidate cached reports.

**Rule violated:** Usage reports must reflect the current state immediately.

**Fix:** Invalidate the org report cache after successful room creation.

**Verification:** Querying a report, creating a second room, and querying again returns both rooms.

### 24. Debug instrumentation was left in runtime code

**Files:** `app/auth.py`, `app/routers/auth.py`, `app/routers/bookings.py`, `app/services/export.py`, `app/services/refunds.py`, `app/services/notifications.py`, `app/timeutils.py`, `app/debug_log.py`

**Bug:** A prior AI pass left debug imports and absolute-path logging calls in request paths.

**Rule risk:** Extra runtime side effects are unrelated to the API contract and can fail or slow requests in different environments.

**Fix:** Removed debug logging calls and deleted `app/debug_log.py`.

**Verification:** Source scan finds no `debug_log`, `dlog`, or `agent log` references in `app/`.

## Contract Preservation

No endpoint paths, documented JSON field names, documented success status codes, or documented application error codes were intentionally changed. Fixes are limited to behavior required by the PDF contract.
