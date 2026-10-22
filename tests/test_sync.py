import unittest
from unittest.mock import patch
from src.integrations.operational_service import OperationalService

class TestBrokenCodeSync(unittest.TestCase):
    def setUp(self):
        self.service = OperationalService()

    def test_sync_logic_add_and_remove(self):
        before = {
            "success": True,
            "product_ids": ["SKU_OLD_1", "SKU_OLD_2"],
            "version": "v42",
        }
        after = {
            "success": True,
            "product_ids": ["SKU_NEW", "SKU_OLD_2"],
            "version": "v43",
        }

        with patch.object(self.service, "supports_ops_low_weight_atomic_compare", return_value=True), \
             patch.object(self.service, "get_ops_low_weight_config", side_effect=[before, after]), \
             patch.object(self.service, "set_ops_low_weight_config", return_value={
                 "success": True,
                 "conflict": False,
                 "version": "v43",
             }) as mock_write:
            ok = self.service.sync_broken_code_to_redis(
                "atlas",
                add_items={"q_old 女士": ["SKU_NEW"]},
                remove_items={"q_old 女士": ["SKU_OLD_1"]}
            )

        self.assertTrue(ok)
        mock_write.assert_called_once_with(
            "atlas",
            "q_old",
            ["SKU_NEW", "SKU_OLD_2"],
            expected_version="v42",
            external_request_id=None,
        )

if __name__ == '__main__':
    unittest.main()
