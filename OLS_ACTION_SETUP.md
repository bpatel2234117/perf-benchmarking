# OLS Feature Flags Action Step Setup

## Overview

The workflow runner supports a special "action" step type that can execute arbitrary API calls against BigID environments. One common use case is enabling OLS (Object Level Security) feature flags before running OLS-enabled scans.

## How It Works

1. **Workflow Step**: A workflow step with `ds_kind="action"` and `notes="ENABLE OLS FEATURE FLAGS"` is added to the workflow
2. **Action Template**: The workflow runner looks up an action template from the `workflow_templates` table using the notes field as the key
3. **Execution**: The action template (a JSON payload) is executed against each active environment assigned to the workflow
4. **API Call**: The template describes one or more BigID API calls to enable OLS feature flags

## Current Status

⚠️ **ACTION REQUIRED**: The OLS action template needs to be configured with the correct BigID API endpoint.

### What's Already Done

- ✅ Workflow defaults include the "ENABLE OLS FEATURE FLAGS" action step
- ✅ Workflow runner has logic to execute action steps
- ✅ React Workflow page displays action steps correctly
- ✅ Streamlit dashboard includes the action step in default workflows

### What Needs To Be Done

1. **Find the correct BigID API endpoint** for enabling OLS feature flags
   - Check BigID API documentation
   - Or use browser DevTools Network tab while enabling OLS in BigID UI
   - Common patterns:
     - `PUT /api/v1/system-settings`
     - `PUT /api/v1/configurations`
     - `POST /api/v1/feature-toggles`

2. **Update the action template** in `scripts/seed_ols_action_template.py`
   - Replace the placeholder API endpoint and payload
   - Test against a BigID environment

3. **Seed the template** into the database:
   ```bash
   python3 scripts/seed_ols_action_template.py
   ```

4. **Test the workflow**:
   - Create a workflow with OLS steps
   - Verify the action step executes successfully
   - Verify OLS is enabled on the target environment

## Action Template Format

Action templates are stored in the `workflow_templates` table with:
- `k` (key): The action name (e.g., "ENABLE OLS FEATURE FLAGS")
- `template_kind`: "action"
- `payload_json`: JSON describing the API call(s)

### Template Variables

Templates can use these variables (replaced at runtime):
- `{{BASE_URL}}`: The BigID environment base URL
- `{{TOKEN}}`: The authentication token

### Template Structure

Single API call:
```json
{
  "method": "PUT",
  "path": "/api/v1/system-settings",
  "body": {
    "ols.enabled": true
  }
}
```

Multiple API calls:
```json
[
  {
    "method": "PUT",
    "path": "/api/v1/system-settings",
    "body": {"ols.enabled": true}
  },
  {
    "method": "POST",
    "path": "/api/v1/feature-toggles",
    "body": {"feature": "objectLevelSecurity", "enabled": true}
  }
]
```

## Workflow Step Configuration

### In Streamlit Dashboard

The action step is included in the default workflow template:
```python
_stp("action", None, notes="ENABLE OLS FEATURE FLAGS")
```

### In React Workflow Page

Action steps are displayed in a special "Action Steps" section and can be:
- Enabled/disabled
- Reordered
- Assigned to specific environments

### In Database

Action steps are stored in `workflow_steps` with:
- `ds_kind`: "action"
- `notes`: "ENABLE OLS FEATURE FLAGS" (used as template lookup key)
- `environment_id`: The environment to run the action on (or NULL for all active environments)

## Environment Assignment

The action step should be configured to run on **all active environments** that will execute OLS scans. This ensures OLS is enabled before any OLS-dependent steps run.

### Options:

1. **NULL environment_id**: Run on all active environments in the workflow
2. **Specific environment_id**: Run only on a specific environment
3. **Multiple action steps**: Create separate action steps for each environment

## Troubleshooting

### Action Step Fails

Check the workflow step error message:
```sql
SELECT id, seq, notes, status, last_error 
FROM workflow_steps 
WHERE ds_kind = 'action' 
ORDER BY updated_at DESC 
LIMIT 10;
```

Common issues:
- Missing action template in database
- Incorrect API endpoint or payload
- Authentication failure
- BigID version incompatibility

### Template Not Found

If you see "Missing action template for key: ENABLE OLS FEATURE FLAGS":
1. Check if the template exists:
   ```sql
   SELECT * FROM workflow_templates WHERE k = 'ENABLE OLS FEATURE FLAGS';
   ```
2. If missing, run the seed script:
   ```bash
   python3 scripts/seed_ols_action_template.py
   ```

### API Call Fails

Enable debug logging in workflow_runner.py to see the actual API request/response:
```python
logger.info(f"Action API call: {method} {url}")
logger.info(f"Action response: {resp.status_code} {resp.text}")
```

## Related Files

- `scripts/seed_ols_action_template.py` - Script to seed the OLS action template
- `workflow_runner.py` - Lines 1472-1500: `run_action_template()` function
- `workflow_runner.py` - Lines 3745-3773: Action step execution logic
- `v2_api/workflow_runner.py` - Same logic for v2 API
- `v2_api/workflow_defaults.py` - Default workflow with OLS action step
- `dashboard.py` - Streamlit workflow planner with OLS action step
- `v2_frontend/src/pages/Workflow.tsx` - React workflow UI

## Next Steps

1. **Identify the BigID API endpoint** for enabling OLS
2. **Update `scripts/seed_ols_action_template.py`** with the correct endpoint
3. **Run the seed script** to populate the database
4. **Test with a workflow** that includes OLS steps
5. **Document the working configuration** for future reference

## Questions?

Contact the team or check:
- BigID API documentation
- BigID support for OLS configuration
- Network tab in browser when enabling OLS manually
