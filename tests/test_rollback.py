import unittest
from unittest.mock import patch, MagicMock
import datetime
from scripts.ops.rollback_today_actions import rollback_today_actions

class TestRollback(unittest.TestCase):

    def setUp(self):
        from src.persistence import Base, reset_engine_for_testing, SessionLocal
        self.engine = reset_engine_for_testing()
        Base.metadata.create_all(bind=self.engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        from src.persistence import Base
        Base.metadata.drop_all(bind=self.engine)

    @patch('scripts.ops.rollback_today_actions.OperationalService')
    @patch('scripts.ops.rollback_today_actions.SessionLocal')
    def test_rollback_today_actions(self, mock_session_local, mock_op_service_class):
        from src.persistence import ActionLog, DailyAbnormalQuery
        
        mock_db = mock_session_local.return_value
        
        # Mock today's actions
        mock_action = MagicMock(spec=ActionLog)
        mock_action.brand = "atlas"
        mock_action.action_type = 2 # demote
        mock_action.product_id = "S1"
        mock_action.product_name = "N1"
        mock_action.query_text = "Q1"
        
        mock_blacklist_action = MagicMock(spec=ActionLog)
        mock_blacklist_action.brand = "atlas"
        mock_blacklist_action.action_type = 1 # blacklist
        mock_blacklist_action.product_id = "S2"
        mock_blacklist_action.product_name = "N2"
        mock_blacklist_action.query_text = "Q2"
        
        # Mock query return values
        mock_query = mock_db.query.return_value.filter.return_value
        mock_query.all.side_effect = [
            [mock_action],          # for action_type == 2
            [mock_blacklist_action], # for action_type == 1
            [MagicMock(spec=DailyAbnormalQuery)] # for queries to mark rolled_back
        ]
        
        mock_op = mock_op_service_class.return_value
        
        rollback_today_actions()
        
        # Verify calls
        mock_op.sync_broken_code_to_redis.assert_not_called()
        mock_op.remove_from_blacklist.assert_called()
        mock_db.commit.assert_called()

    @patch('scripts.ops.rollback_today_actions.OperationalService')
    @patch('scripts.ops.rollback_today_actions.SessionLocal')
    def test_rollback_no_actions(self, mock_session_local, mock_op_service_class):
        mock_db = mock_session_local.return_value
        mock_query = mock_db.query.return_value.filter.return_value
        mock_query.all.return_value = []
        
        rollback_today_actions()
        
        mock_op_service_class.return_value.sync_broken_code_to_redis.assert_not_called()
        mock_db.commit.assert_called()

if __name__ == '__main__':
    unittest.main()
