import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ui import color_schemes


class ActiveColorSchemeCacheTests(unittest.TestCase):
    def test_percent_lookup_reuses_the_activated_scheme_snapshot(self) -> None:
        color_schemes.set_active_color_scheme("classic")

        with patch.object(color_schemes, "_read_payload", side_effect=AssertionError("unexpected reload")):
            colors = [color_schemes.percent_gradient_style(value) for value in range(101)]

        self.assertEqual(len(colors), 101)
        self.assertTrue(all(color.startswith("#") for color in colors))

    def test_explicit_scheme_change_replaces_the_active_snapshot(self) -> None:
        color_schemes.set_active_color_scheme("classic")
        classic = color_schemes.percent_gradient_style(50)

        selected = color_schemes.set_active_color_scheme("icefire")
        icefire = color_schemes.percent_gradient_style(50)

        self.assertEqual(selected, "icefire")
        self.assertEqual(color_schemes.active_color_scheme_key(), "icefire")
        self.assertNotEqual(classic, icefire)


if __name__ == "__main__":
    unittest.main()
