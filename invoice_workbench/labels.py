"""Shared presentation labels for the main window and selection dialog."""

STATUS = {"confirmed": "已确认", "submitted": "已提交", "returned": "已退回", "reimbursed": "已报销", "cancelled": "已取消"}


def state_label(item: dict) -> str:
    if item.get("batch_id"):
        return "已占用"
    if item.get("archived"):
        return "已移出"
    if item.get("duplicate"):
        return "同票冲突"
    if not item.get("reviewed"):
        return "待核对"
    if not item.get("company_match"):
        return "公司不匹配"
    return "可用"
