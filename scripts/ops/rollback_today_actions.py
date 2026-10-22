import datetime
import sys
import os

# 将项目根目录加入路径
sys.path.append(os.getcwd())

from src.persistence import SessionLocal, ActionLog, DailyAbnormalQuery
from src.integrations.operational_service import OperationalService

def rollback_today_actions():
    """
    一键回滚当日的所有自动化运维操作。
    逻辑：
    1. 查找当日产生的 action_log。
    2. 对于拉黑操作，目前接口不支持直接从黑名单移除的自动化回滚（需手动或等下次打标），
       但分层调整（降权）可以通过再次调用接口恢复。
    3. 标记当日异常词状态为已回滚。
    """
    print(f"[{datetime.datetime.now()}] 启动自动化运维回滚程序...")
    db = SessionLocal()
    op_service = OperationalService()
    
    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    # 1. 获取当日执行的所有降权操作 (action_type=2)
    actions = db.query(ActionLog).filter(ActionLog.created_at >= today_start, ActionLog.action_type == 2).all()
    
    skipped_query_demotion_count = len(actions)
    if not actions:
        print("今日未检测到可回滚的降权操作。")
    else:
        print(
            f"检测到 {len(actions)} 条 Query Demotion 日志；通用回滚无法证明完整配置，"
            "已跳过。请使用 authoritative action rollback。"
        )

    # 2. 获取当日执行的所有拉黑操作 (action_type=1)
    blacklist_actions = db.query(ActionLog).filter(ActionLog.created_at >= today_start, ActionLog.action_type == 1).all()
    
    if not blacklist_actions:
        print("今日未检测到可回滚的拉黑操作。")
    else:
        print(f"检测到 {len(blacklist_actions)} 条拉黑记录，正在尝试移出黑名单...")
        for action in blacklist_actions:
            print(f" - 正在移出黑名单: {action.product_name} ({action.product_id}) for Query: {action.query_text}")
            success = op_service.remove_from_blacklist(action.brand, action.product_id, [action.query_text])
            if not success:
                print(f"   ⚠️ 移出黑名单失败: {action.product_id}")
            
    # 3. 标记当日异常词为 failed/rolled_back
    if not actions:
        queries = db.query(DailyAbnormalQuery).filter(DailyAbnormalQuery.created_at >= today_start).all()
        for q in queries:
            q.ops_effect = 'rolled_back'
            q.remark = f"于 {datetime.datetime.now()} 被人工回滚"
    
    db.commit()
    db.close()
    print("回滚操作完成。注意：拉黑操作的移出可能会触发异步打标流程，需一定时间生效。")
    return {
        "queryDemotionStatus": (
            "authoritative_action_required" if actions else "not_found"
        ),
        "queryDemotionSkipped": skipped_query_demotion_count,
        "blacklistAttempted": len(blacklist_actions),
    }

if __name__ == "__main__":
    rollback_today_actions()
