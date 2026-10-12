import io
import json
import unittest
import zipfile
from unittest.mock import MagicMock, Mock, patch

from v2_api.tracker import (
    _scan_collection_filter,
    _scan_parts_filter,
    _request_get,
    fetch_scan_collection_records,
    fetch_child_scan_metrics,
    fetch_scan_part_records,
    extract_subscan_ids,
    filter_scan_collection_records,
    filter_scan_part_records,
    sync_scan_part_records_for_scan,
)
from v2_api.scan_progress_helpers import load_scan_part_records
from v2_api.scan_bigid_sync import (
    fetch_file_download_token,
    download_scan_parts_report,
    parse_scan_parts_report_csv,
    fetch_scan_parts_report_errors,
    SCAN_PARTS_REPORT_ERROR_COLUMN,
)


class ScanPartsTests(unittest.TestCase):
    @patch("v2_api.tracker.fetch_child_scan_insights")
    @patch("v2_api.tracker._stored_subscan_ids", return_value=["sub-1"])
    def test_child_scan_metrics_use_scan_counters(self, stored_ids, fetch_child):
        fetch_child.return_value = {
            "data": {
                "scanChildInfo": {
                    "Scan": {
                        "totalParts": 10000,
                        "partsCompleted": 9990,
                        "partsFailed": 10,
                    },
                    "Classification": {"totalParts": 384379, "partsCompleted": 384379},
                }
            }
        }

        result = fetch_child_scan_metrics(Mock(), "https://bigid.example", "scan-1", "token")

        self.assertEqual(
            result,
            {"total_scan_parts": 10000, "completed_scan_parts": 9990, "failed_scan_parts": 10},
        )

    @patch("v2_api.scan_progress_helpers.pg_table_exists", return_value=True)
    def test_load_collection_records_returns_latest_five_and_new_count(self, table_exists):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        count_result = MagicMock()
        count_result.scalar.return_value = 6
        new_count_result = MagicMock()
        new_count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [
            (index, "sub-1", "Completed", f"key-{index}", json.dumps({"updated_at": f"2026-08-18T00:0{index}:00Z"}), "captured")
            for index in range(1, 7)
        ]
        conn.execute.side_effect = [count_result, new_count_result, rows_result]

        result = load_scan_part_records(engine, "scan-1", limit=500, collection="scan_parts")

        self.assertEqual(result["count"], 6)
        self.assertEqual(result["new_filter_count"], 0)
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual(result["new_items"], [])
        self.assertEqual(result["items"][0]["record_key"], "key-6")

    @patch("v2_api.scan_progress_helpers.pg_table_exists", return_value=True)
    def test_load_collection_records_limits_new_items_to_display_window(self, table_exists):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        count_result = MagicMock()
        count_result.scalar.return_value = 25
        new_count_result = MagicMock()
        new_count_result.scalar.return_value = 25
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [
            (index, "sub-1", "Completed", f"key-{index}", json.dumps({}), "captured")
            for index in range(1, 26)
        ]
        new_rows_result = MagicMock()
        new_rows_result.fetchall.return_value = [
            (index, "sub-1", "New", f"new-key-{index}", json.dumps({}), "captured")
            for index in range(1, 26)
        ]
        conn.execute.side_effect = [count_result, new_count_result, rows_result, new_rows_result]

        result = load_scan_part_records(engine, "scan-1", limit=500, collection="scan_parts")

        self.assertEqual(len(result["items"]), 5)
        self.assertEqual(len(result["new_items"]), 5)
        self.assertEqual(result["new_items"][0]["record_key"], "new-key-25")

    @patch("v2_api.scan_progress_helpers.pg_table_exists", return_value=True)
    def test_load_scans_collection_keeps_only_current_scan_records(self, table_exists):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        count_result = MagicMock()
        count_result.scalar.return_value = 3
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [
            (1, None, "Completed", "key-current", json.dumps({"_id": "scan-1"}), "captured"),
            (2, None, "Completed", "key-child", json.dumps({"_id": "sub-1", "parent_scan_id": "scan-1"}), "captured"),
            (3, None, "Completed", "key-other", json.dumps({"_id": "scan-2"}), "captured"),
        ]
        conn.execute.side_effect = [count_result, rows_result]

        result = load_scan_part_records(engine, "scan-1", limit=5, collection="scans")

        self.assertEqual(result["count"], 1)
        self.assertEqual([row["record_key"] for row in result["items"]], ["key-current"])

    @patch("v2_api.scan_progress_helpers.pg_table_exists", return_value=True)
    def test_load_scans_collection_returns_latest_fifteen_records(self, table_exists):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        count_result = MagicMock()
        count_result.scalar.return_value = 20
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [
            (
                index,
                None,
                "Completed",
                f"key-{index}",
                json.dumps({"_id": "scan-1", "updated_at": f"2026-08-18T00:{index:02d}:00Z"}),
                "captured",
            )
            for index in range(1, 21)
        ]
        conn.execute.side_effect = [count_result, rows_result]

        result = load_scan_part_records(engine, "scan-1", limit=15, collection="scans")

        self.assertEqual(result["count"], 20)
        self.assertEqual(len(result["items"]), 15)
        self.assertEqual(result["items"][0]["record_key"], "key-20")

    @patch("v2_api.tracker.time.sleep")
    @patch("v2_api.tracker.requests.get")
    def test_request_get_retries_transient_transport_error(self, request, sleep):
        response = Mock(status_code=200)
        request.side_effect = [RuntimeError("incomplete read"), response]

        result = _request_get("https://bigid.example/api", "token")

        self.assertIs(result, response)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_fetch_collection_records_decodes_zip_wrapped_json(self):
        payload = json.dumps([
            {"_id": "scan-1"},
            {"_id": "sub-1", "parent_scan_id": "scan-1"},
        ]).encode("utf-8")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("scans.json", payload)
        response = Mock(status_code=200, content=archive.getvalue())
        response.json.side_effect = json.JSONDecodeError("not JSON", "", 0)
        response.raise_for_status.return_value = None

        with patch("v2_api.tracker._request_get", return_value=response):
            result = fetch_scan_collection_records("https://bigid.example", "scan-1", "token")

        self.assertEqual([row["_id"] for row in result], ["scan-1"])

    def test_fetch_scan_parts_stops_when_download_endpoint_ignores_limit(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {"id": f"part-{index}", "scan_id": "sub-1", "state": "New"}
                for index in range(501)
            ]
        }
        response.raise_for_status.return_value = None

        with patch("v2_api.tracker._request_get", return_value=response) as request:
            result = fetch_scan_part_records(
                "https://bigid.example", "scan-1", "token", ["sub-1"], limit=500
            )

        self.assertEqual(len(result), 500)
        request.assert_called_once()

    def test_filter_scan_part_records_keeps_only_new_requested_subscan(self):
        records = [
            {"id": "new", "subscan_id": "sub-1", "state": "New"},
            {"id": "old", "subscan_id": "sub-1", "state": "Done"},
            {"id": "other", "subscan_id": "sub-2", "state": "New"},
        ]

        result = filter_scan_part_records(records, scan_id="scan-1", subscan_ids=["sub-1"])

        self.assertEqual(result, [records[0]])

    def test_filter_scan_part_records_accepts_scan_id_as_subscan_id(self):
        records = [
            {"id": "part-1", "scan_id": "sub-1", "parent_scan_id": "scan-1", "state": "New"},
            {"id": "part-2", "scan_id": "sub-2", "parent_scan_id": "scan-1", "state": "New"},
        ]

        result = filter_scan_part_records(records, scan_id="scan-1", subscan_ids=["sub-1"])

        self.assertEqual(result, [records[0]])

    def test_filter_scan_part_records_accepts_parent_scan_id_without_subscan_id(self):
        records = [
            {"id": "part-1", "parent_scan_id": "scan-1", "state": "New"},
            {"id": "part-2", "parent_scan_id": "scan-2", "state": "New"},
        ]

        result = filter_scan_part_records(records, scan_id="scan-1", subscan_ids=[])

        self.assertEqual(result, [records[0]])

    def test_filter_scan_part_records_drops_unscoped_rows_for_parent_scan(self):
        records = [
            {"id": "keep", "parent_scan_id": "scan-1", "state": "Completed"},
            {"id": "other", "parent_scan_id": "scan-2", "state": "Completed"},
            {"id": "unscoped", "subscan_id": "sub-unknown", "state": "Completed"},
        ]

        result = filter_scan_part_records(records, scan_id="scan-1", state=None)

        self.assertEqual([row["id"] for row in result], ["keep"])

    def test_filter_scan_part_records_without_state_keeps_all_states(self):
        records = [
            {"id": "completed", "subscan_id": "sub-1", "state": "Completed"},
            {"id": "new", "subscan_id": "sub-1", "state": "New"},
            {"id": "other", "subscan_id": "sub-2", "state": "Completed"},
        ]

        result = filter_scan_part_records(records, scan_id="scan-1", subscan_ids=["sub-1"], state=None)

        self.assertEqual([row["id"] for row in result], ["completed", "new"])

    def test_scan_parts_filter_contains_state_and_subscan_constraints(self):
        query = json.loads(_scan_parts_filter(["sub-1"]))

        self.assertEqual(query[0], {"field": "subscan_id", "value": ["sub-1"], "operator": "in"})
        self.assertEqual(query[1], {"field": "state", "value": ["New"], "operator": "in"})

    def test_scan_parts_filter_uses_parent_scan_id_and_state(self):
        query = json.loads(_scan_parts_filter(scan_id="scan-1", subscan_ids=["sub-1"]))

        self.assertEqual(query, [
            {"field": "parent_scan_id", "value": ["scan-1"], "operator": "in"},
            {"field": "state", "value": ["New"], "operator": "in"},
        ])

    def test_fetch_scan_part_records_filters_response_when_server_returns_extra_rows(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {"id": "keep", "subscan_id": "sub-1", "state": "New"},
                {"id": "drop", "subscan_id": "sub-1", "state": "Completed"},
            ]
        }
        response.raise_for_status.return_value = None

        with patch("v2_api.tracker._request_get", return_value=response) as request:
            result = fetch_scan_part_records("https://bigid.example", "scan-1", "token", ["sub-1"])

        self.assertEqual([row["id"] for row in result], ["keep"])
        self.assertNotIn("filter", request.call_args.kwargs["params"])

    def test_fetch_scan_part_records_uses_subscan_id_from_returned_document(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [{
                "_id": "part-1",
                "parent_scan_id": "scan-1",
                "subscan_id": "sub-1",
                "state": "New",
            }]
        }
        response.raise_for_status.return_value = None

        with patch("v2_api.tracker._request_get", return_value=response):
            result = fetch_scan_part_records(
                "https://bigid.example", "scan-1", "token", state="New"
            )

        self.assertEqual(result[0]["parent_scan_id"], "scan-1")
        self.assertEqual(result[0]["subscan_id"], "sub-1")

    def test_scan_parts_empty_subscan_allowlist_keeps_nothing(self):
        result = filter_scan_part_records(
            [{"id": "new", "subscan_id": "sub-1", "state": "New"}],
            subscan_ids=[],
        )

        self.assertEqual(result, [])

    def test_extract_subscan_ids_from_subscans_container(self):
        payload = {
            "data": {
                "scanInsights": {
                    "subScans": [
                        {"_id": "sub-1", "state": "In Progress"},
                        {"id": "sub-2", "state": "Completed"},
                    ]
                }
            }
        }

        self.assertEqual(extract_subscan_ids(payload), ["sub-1", "sub-2"])

    @patch("v2_api.tracker.fetch_scan_collection_records")
    @patch("v2_api.tracker.fetch_scan_part_records")
    def test_completed_scan_does_not_fetch_collections(self, fetch_parts, fetch_collection):
        result = sync_scan_part_records_for_scan(
            Mock(), "https://bigid.example", {"id": 1}, "token", "scan-1",
            scan_status="Completed",
        )

        self.assertEqual(result, 0)
        fetch_parts.assert_not_called()
        fetch_collection.assert_not_called()

    @patch("v2_api.tracker.fetch_scan_collection_records", return_value=[])
    @patch("v2_api.tracker.fetch_scan_part_records", return_value=[])
    @patch("v2_api.tracker.ensure_scan_tracking_tables")
    def test_manual_refresh_fetches_terminal_scan_collections(
        self, ensure_tables, fetch_parts, fetch_collection
    ):
        sync_scan_part_records_for_scan(
            Mock(), "https://bigid.example", {"id": 1}, "token", "scan-1",
            scan_status="Completed", allow_terminal=True,
        )

        fetch_collection.assert_called_once()
        self.assertEqual(fetch_parts.call_count, 2)
        self.assertEqual(
            [call.kwargs["state"] for call in fetch_parts.call_args_list],
            ["New", None],
        )

    @patch("v2_api.tracker.fetch_scan_collection_records", return_value=[])
    @patch("v2_api.tracker.fetch_scan_part_records", return_value=[])
    @patch("v2_api.tracker.ensure_scan_tracking_tables")
    def test_running_scan_fetches_new_and_unfiltered_scan_parts_scoped_by_parent(
        self, ensure_tables, fetch_parts, fetch_collection
    ):
        sync_scan_part_records_for_scan(
            Mock(), "https://bigid.example", {"id": 1}, "token", "scan-1",
            scan_status="InProgress",
        )

        self.assertEqual(fetch_parts.call_count, 2)
        self.assertEqual(
            [call.kwargs["state"] for call in fetch_parts.call_args_list],
            ["New", None],
        )
        self.assertTrue(all("subscan_ids" not in call.kwargs for call in fetch_parts.call_args_list))

    @patch("v2_api.tracker.fetch_scan_collection_records", return_value=[])
    @patch("v2_api.tracker.fetch_scan_part_records", return_value=[])
    @patch("v2_api.tracker.ensure_scan_tracking_tables")
    def test_running_scan_uses_stored_status_when_payload_status_missing(
        self, ensure_tables, fetch_parts, fetch_collection
    ):
        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchone.side_effect = [
            ("InProgress",),
        ]

        sync_scan_part_records_for_scan(
            engine, "https://bigid.example", {"id": 1}, "token", "scan-1",
        )

        fetch_collection.assert_called_once()
        fetch_parts.assert_called_once()

    @patch("v2_api.tracker.fetch_scan_collection_records", return_value=[{"_id": "scan-1"}])
    @patch("v2_api.tracker.fetch_scan_part_records")
    @patch("v2_api.tracker.ensure_scan_tracking_tables")
    def test_collection_scope_scans_does_not_refresh_scan_parts(
        self, ensure_tables, fetch_parts, fetch_collection
    ):
        sync_scan_part_records_for_scan(
            Mock(), "https://bigid.example", {"id": 1}, "token", "scan-1",
            scan_status="InProgress", collection="scans",
        )

        fetch_collection.assert_called_once()
        fetch_parts.assert_not_called()

    def test_scans_collection_filters_exact_scan_id_and_sorts(self):
        records = [
            {"_id": "scan-1", "state": "Started"},
            {"_id": "sub-1", "parent_scan_id": "scan-1"},
            {"_id": "other", "parent_scan_id": "scan-2"},
        ]

        self.assertEqual(
            [row["_id"] for row in filter_scan_collection_records(records, "scan-1")],
            ["scan-1"],
        )
        self.assertEqual(
            json.loads(_scan_collection_filter("scan-1")),
            [{"field": "_id", "value": ["scan-1"], "operator": "in"}],
        )

    def test_fetch_scans_collection_filters_response_locally(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {"_id": "scan-1"},
                {"_id": "sub-1", "parent_scan_id": "scan-1"},
                {"_id": "other", "parent_scan_id": "scan-2"},
            ]
        }
        response.raise_for_status.return_value = None

        with patch("v2_api.tracker._request_get", return_value=response) as request:
            result = fetch_scan_collection_records("https://bigid.example", "scan-1", "token")

        self.assertEqual([row["_id"] for row in result], ["scan-1"])
        self.assertNotIn("filter", request.call_args.kwargs["params"])


class ScanPartsReportDownloadTests(unittest.TestCase):
    def test_parse_csv_keeps_only_rows_with_error_message(self):
        csv_text = "part_name,subscan_id,errorMessage\npart-a,sub-1,\npart-b,sub-1,conn refused\npart-c,sub-1,\n"
        result = parse_scan_parts_report_csv(csv_text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["part_name"], "part-b")
        self.assertEqual(result[0]["errorMessage"], "conn refused")

    def test_parse_csv_respects_custom_error_column(self):
        csv_text = "id,err\na,\nb,something failed\n"
        result = parse_scan_parts_report_csv(csv_text, error_column="err")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["err"], "something failed")

    def test_parse_csv_returns_empty_list_for_empty_input(self):
        self.assertEqual(parse_scan_parts_report_csv(""), [])
        self.assertEqual(parse_scan_parts_report_csv("   "), [])

    @patch("v2_api.scan_bigid_sync.requests.get")
    def test_fetch_file_download_token_extracts_token(self, request_get):
        response = Mock(status_code=200)
        response.content = b'{"data": {"token": "jwt-123"}}'
        response.json.return_value = {"data": {"token": "jwt-123"}}
        response.raise_for_status.return_value = None
        request_get.return_value = response

        token = fetch_file_download_token("https://bigid.example", "auth-token")

        self.assertEqual(token, "jwt-123")
        request_get.assert_called_once()

    @patch("v2_api.scan_bigid_sync.requests.get")
    def test_download_scan_parts_report_uses_download_token_header(self, request_get):
        response = Mock(status_code=200)
        response.text = "part_name,errorMessage\npart-a,err\n"
        response.raise_for_status.return_value = None
        request_get.return_value = response

        text = download_scan_parts_report("https://bigid.example", "jwt-123", "sub-1")

        self.assertEqual(text, "part_name,errorMessage\npart-a,err\n")
        call = request_get.call_args
        self.assertEqual(call.kwargs["headers"], {"download-token": "jwt-123"})
        self.assertEqual(call.kwargs["params"], {"subscan_id": "sub-1"})

    @patch("v2_api.scan_bigid_sync.Get_auth_token")
    @patch("v2_api.scan_bigid_sync.load_environment_for_scan_id")
    @patch("v2_api.scan_bigid_sync.fetch_file_download_token")
    @patch("v2_api.scan_bigid_sync.download_scan_parts_report")
    @patch("v2_api.scan_bigid_sync.fetch_parent_scan_insight_http")
    def test_fetch_scan_parts_report_errors_orchestrates_and_returns_rows(
        self,
        fetch_insights,
        download_report,
        fetch_token,
        load_env,
        get_auth_token,
    ):
        load_env.return_value = {
            "id": 1,
            "env_name": "test-env",
            "base_url": "https://bigid.example",
        }
        get_auth_token.return_value = "auth-token"
        fetch_insights.return_value = {
            "data": {
                "scanInsights": {
                    "subScans": [{"_id": "sub-1"}, {"_id": "sub-2"}]
                }
            }
        }
        fetch_token.return_value = "jwt-123"
        download_report.side_effect = [
            "part_name,errorMessage\npart-a,error one\n",
            "part_name,errorMessage\n\n",
        ]

        result = fetch_scan_parts_report_errors(Mock(), "scan-1")

        self.assertTrue(result["available"])
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["subscan_ids"], ["sub-1", "sub-2"])
        self.assertEqual(result["errors"][0]["part_name"], "part-a")
        self.assertEqual(result["errors"][0]["errorMessage"], "error one")
        self.assertEqual(result["errors"][0]["_subscan_id"], "sub-1")

    @patch("v2_api.scan_bigid_sync.Get_auth_token")
    @patch("v2_api.scan_bigid_sync.load_environment_for_scan_id")
    def test_fetch_scan_parts_report_errors_returns_unavailable_when_env_missing(
        self, load_env, get_auth_token
    ):
        load_env.return_value = None

        result = fetch_scan_parts_report_errors(Mock(), "scan-1")

        self.assertFalse(result["available"])
        self.assertIn("No environment linked", result["error"])
        get_auth_token.assert_not_called()

    @patch("v2_api.scan_bigid_sync.Get_auth_token")
    @patch("v2_api.scan_bigid_sync.load_environment_for_scan_id")
    @patch("v2_api.scan_bigid_sync.fetch_file_download_token")
    @patch("v2_api.scan_bigid_sync.download_scan_parts_report")
    @patch("v2_api.scan_bigid_sync.fetch_parent_scan_insight_http")
    def test_fetch_scan_parts_report_errors_uses_db_subscan_fallback(
        self,
        fetch_insights,
        download_report,
        fetch_token,
        load_env,
        get_auth_token,
    ):
        load_env.return_value = {
            "id": 1,
            "env_name": "test-env",
            "base_url": "https://bigid.example",
        }
        get_auth_token.return_value = "auth-token"
        fetch_insights.side_effect = RuntimeError("insights purged")
        fetch_token.return_value = "jwt-123"
        download_report.return_value = "part_name,errorMessage\npart-b,error two\n"

        engine = MagicMock()
        conn = MagicMock()
        engine.connect.return_value.__enter__.return_value = conn
        conn.execute.return_value.fetchall.return_value = [("sub-3",)]

        result = fetch_scan_parts_report_errors(engine, "scan-1")

        self.assertTrue(result["available"])
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["subscan_ids"], ["sub-3"])
        self.assertEqual(result["errors"][0]["errorMessage"], "error two")
        self.assertEqual(result["errors"][0]["_subscan_id"], "sub-3")
