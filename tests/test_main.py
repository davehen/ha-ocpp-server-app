import unittest

from app.main import charge_point_id_from_path


class ChargePointPathTests(unittest.TestCase):
    def test_origin_form_path(self) -> None:
        self.assertEqual(charge_point_id_from_path("/EVB-P123?token=ignored"), "EVB-P123")

    def test_absolute_form_path_used_by_elvi(self) -> None:
        self.assertEqual(
            charge_point_id_from_path("http://192.168.1.2:9000/EVB-P123"),
            "EVB-P123",
        )


if __name__ == "__main__":
    unittest.main()
