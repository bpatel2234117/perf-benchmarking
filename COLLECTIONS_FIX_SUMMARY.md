# Collections Data Sync Fix - Summary

## Problem
Collections data (scan_parts and scans) were not appearing in the scan-status UI page, even though the BigID API was returning the data. The user needed to see collections data displayed in the UI.

## Root Causes Identified & Fixed

### 1. **Token Authentication Format** ✅ FIXED
- **Issue**: Collections API requires raw token format in Authorization header, not "Authorization: Bearer <token>"
- **Fix Location**: `v2_api/tracker.py` lines 1619, 1695
  - Changed `_request_get(url, None, params=params...)` to `_request_get(url, token, params=params...)`
  - This allows the raw token to be sent first (which succeeds for collections API)
- **Status**: API now successfully authenticates with BigID collections endpoints

### 2. **Refresh-Collection Endpoint Was Stub** ✅ FIXED
- **Issue**: `/api/scan/{scan_id}/refresh-collection` endpoint (called by "Refresh both collections" button) was returning `count=0` without actually fetching any data
- **Fix Location**: `v2_api/main.py` lines 2495-2557
  - Now queries environment for the scan
  - Gets BigID base_url and obtains auth token via `Get_auth_token()`
  - Calls `sync_scan_part_records_for_scan()` with `allow_terminal=True`
  - Returns the count of records actually fetched
- **Status**: Endpoint now performs the actual sync operation

### 3. **Sync-Insights Endpoint Enhancement** ✅ ADDED
- **Location**: `v2_api/main.py` lines 2368-2393
  - Added collections sync after insights sync in `/api/scan/sync-insights` endpoint
  - Called with `allow_terminal=True` to support completed scans
  - Added logging for debugging: `logger.error(f"Failed to sync collections for {sid}: {ex}", exc_info=True)`

### 4. **Logging Added** ✅ IMPLEMENTED
- **Location**: `v2_api/main.py` lines 1, 18
  - Added `import logging` and `logger = logging.getLogger(__name__)`
  - Error logging in both endpoints to help debug future issues

## Files Changed
1. **v2_api/tracker.py**
   - Lines 1619, 1695: Pass token to `_request_get()` for proper authentication

2. **v2_api/main.py**
   - Lines 1, 18: Added logging setup
   - Lines 2368-2393: Collections sync in sync-insights endpoint
   - Lines 2495-2557: Implemented actual sync logic in refresh-collection endpoint

## Commits Made
1. `ebd2c1a` - Fix: Sync collections for completed/terminal scans via sync-insights endpoint
2. `6294503` - Fix: Implement collections sync in refresh-collection endpoint

## Current Status
- ✅ Token authentication fixed - Collections API is reachable
- ✅ Endpoints updated to call the sync function
- ❌ **Collections still not appearing in UI** - Needs further investigation
  - Possible causes: 
    - `sync_scan_part_records_for_scan()` may not be finding/storing data correctly
    - Database insertion might be failing
    - BigID API response parsing issue

## Next Steps for Debugging
1. Check API logs for error messages (now logged with `logger.error()`)
2. Verify `sync_scan_part_records_for_scan()` function is correctly extracting ZIP file contents
3. Confirm database `scan_part_records` table is receiving inserts
4. Review `_response_json_payload()` function for ZIP handling correctness
