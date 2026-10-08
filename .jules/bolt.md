## 2024-10-08 - Redundant Database Query Fetching Same Overrides
**Learning:** `_with_usage` in the admin dashboard was unnecessarily fetching user overrides from the database, even though they were already part of the `user` dict fetched in `search_users` and `get_user_detail`. This caused an N+1 query problem, creating an extra DB roundtrip per user displayed in the admin UI.
**Action:** When working on lists that assemble data, reuse data already fetched by the caller instead of repeating queries.
## 2024-10-08 - Caching Considerations for Global Settings
**Learning:** `quota.get_settings()` executes a `SELECT name, value FROM setting` database query on each invocation. While I optimized the user overrides in `_with_usage`, calling `get_settings()` repeatedly inside `_with_usage` (which is invoked in a list comprehension in `search_users`) causes the same N+1 query issue for the settings table.
**Action:** When a helper function used in a loop makes a database query that is identical across iterations, hoist the query outside the loop and pass the result as an argument.
