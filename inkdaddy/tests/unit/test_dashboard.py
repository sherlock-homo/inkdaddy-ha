import unittest

from inkdaddy_hub.services.dashboard import (
    MAX_YAML_BYTES,
    MAX_YAML_DEPTH,
    DashboardValidationError,
    detect_layout_collisions,
    load_dashboard_yaml,
    validate_dashboard_config,
)
from inkdaddy_hub.services.dashboard_content import dashboard_config


class DashboardValidationTests(unittest.TestCase):
    def test_cached_dashboard_returns_an_independent_config(self):
        expected = dashboard_config(None)
        edited = dashboard_config(None)
        edited["widgets"][0]["source"]["type"] = "text"
        self.assertEqual(dashboard_config(None), expected)
        self.assertNotEqual(edited, expected)

    def test_yaml_requires_bounded_json_compatible_metadata(self):
        with self.assertRaisesRegex(DashboardValidationError, "64 KiB"):
            load_dashboard_yaml("name: " + "a" * MAX_YAML_BYTES)
        with self.assertRaisesRegex(DashboardValidationError, "too complex"):
            load_dashboard_yaml("metadata: " + "[" * MAX_YAML_DEPTH + "1" + "]" * MAX_YAML_DEPTH)
        with self.assertRaisesRegex(DashboardValidationError, "JSON-compatible"):
            load_dashboard_yaml("metadata: 2020-01-01\nwidgets: []")

    def test_small_yaml_aliases_remain_supported(self):
        config = load_dashboard_yaml("defaults: &defaults {label: Weather}\ncopy: *defaults\nwidgets: []")
        self.assertEqual(config["copy"], {"label": "Weather"})

    def test_valid_dashboard(self):
        config = {
            "version": 1,
            "grid": {"cols": 4, "rows": 2},
            "widgets": [
                {"id": "a", "type": "big_number", "x": 0, "y": 0, "w": 1, "h": 1},
                {"id": "b", "type": "line_chart", "x": 1, "y": 0, "w": 2, "h": 1},
                {"id": "c", "type": "text_note", "x": 0, "y": 1, "w": 4, "h": 1},
            ],
        }
        normalized = validate_dashboard_config(config)
        self.assertEqual(normalized["grid"], {"cols": 4, "rows": 2})
        self.assertEqual(len(normalized["widgets"]), 3)

    def test_rejects_overlap(self):
        config = {
            "grid": {"cols": 4, "rows": 2},
            "widgets": [
                {"id": "a", "type": "big_number", "x": 0, "y": 0, "w": 1, "h": 1},
                {"id": "b", "type": "on_off_state", "x": 0, "y": 0, "w": 1, "h": 1},
            ],
        }
        with self.assertRaises(DashboardValidationError):
            validate_dashboard_config(config)

    def test_collision_helper_reports_errors(self):
        errors = detect_layout_collisions(
            [
                {"id": "a", "type": "big_number", "x": 0, "y": 0, "w": 1, "h": 1},
                {"id": "b", "type": "on_off_state", "x": 0, "y": 0, "w": 1, "h": 1},
            ]
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("overlaps", errors[0])

    def test_rejects_invalid_footprint(self):
        with self.assertRaises(DashboardValidationError):
            validate_dashboard_config(
                {
                    "grid": {"cols": 4, "rows": 2},
                    "widgets": [{"id": "bad", "type": "big_number", "x": 0, "y": 0, "w": 4, "h": 2}],
                }
            )
