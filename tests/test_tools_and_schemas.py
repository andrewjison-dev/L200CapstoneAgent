"""Tests for Category 1: Tool & Interface Design (Docstrings, Naming, Schemas, Guided Errors)."""

from __future__ import annotations

import inspect
import unittest

from concierge_agent.schemas import (
    RecipeSearchInputSchema,
    SchemaValidationError,
    validate_payload,
)
from concierge_agent.tools import (
    ALL_CONCIERGE_TOOLS,
    collect_weekly_blocker_inputs_for_andrew,
    search_high_protein_kid_friendly_recipes,
    validate_recipe_against_family_allergens_and_time,
)


class TestToolAndInterfaceDesign(unittest.TestCase):
    def test_all_tools_have_descriptive_names_and_comprehensive_docstrings(self) -> None:
        self.assertGreaterEqual(len(ALL_CONCIERGE_TOOLS), 10)
        for tool_fn in ALL_CONCIERGE_TOOLS:
            name = tool_fn.__name__
            # Descriptive multi-word verb+noun names (no vague names like `get_data`)
            self.assertGreaterEqual(len(name.split("_")), 3, f"Tool name too vague: {name}")
            doc = inspect.getdoc(tool_fn) or ""
            self.assertGreater(len(doc), 120, f"Docstring too short on {name}")
            self.assertIn("Args:", doc, f"Missing Args section on {name}")
            self.assertIn("Returns:", doc, f"Missing Returns section on {name}")

    def test_explicit_json_schemas_forbid_unknown_fields_and_invalid_ranges(self) -> None:
        schema = RecipeSearchInputSchema.json_schema()
        self.assertIn("properties", schema)

        with self.assertRaises(SchemaValidationError):
            validate_payload(
                RecipeSearchInputSchema,
                {
                    "meal_category": "dinner",
                    "unexpected_extra_field": "not_allowed",
                },
            )

        with self.assertRaises(SchemaValidationError):
            validate_payload(
                RecipeSearchInputSchema,
                {
                    "meal_category": "invalid_slot",
                    "max_active_cook_minutes": 25,
                },
            )

    def test_guided_error_handling_returns_actionable_recovery_instructions(self) -> None:
        bad_checkin = collect_weekly_blocker_inputs_for_andrew(week_start_date="not-a-date")
        self.assertEqual(bad_checkin["status"], "error")
        self.assertEqual(bad_checkin["error_code"], "INVALID_WEEKLY_CHECKIN_INPUT")
        self.assertIn("YYYY-MM-DD", bad_checkin["recovery_instructions"])

        missing_recipe = validate_recipe_against_family_allergens_and_time(
            recipe_id="unknown_dish_xyz"
        )
        self.assertEqual(missing_recipe["status"], "error")
        self.assertIn(
            "search_high_protein_kid_friendly_recipes",
            missing_recipe["recovery_instructions"],
        )

    def test_allergen_filtering_excludes_peanut_recipes(self) -> None:
        res = search_high_protein_kid_friendly_recipes(
            meal_category="dinner",
            max_active_cook_minutes=30,
            excluded_allergens=["peanuts", "tree_nuts"],
        )
        self.assertEqual(res["status"], "success")
        returned_ids = {r["recipe_id"] for r in res["recipes"]}
        self.assertNotIn("dinner_thai_peanut_noodle_skillet", returned_ids)


if __name__ == "__main__":
    unittest.main()
