INVOICE_STATUS = {"available": "可用", "reserved": "已选入批次", "submitted": "已提交", "reimbursed": "已报销", "returned": "已退回"}


def selectable(item):
    return (item.get("verification", {}).get("passed", False) and not item.get("archived")
            and not item.get("duplicate") and item.get("company_match")
            and item.get("reimbursement_status") in {"available", "returned"})


def state_label(item: dict) -> str:
    if item.get("archived"):
        return "已移出"
    status = item.get("reimbursement_status", "available")
    if status in {"reserved", "submitted", "reimbursed"}:
        return INVOICE_STATUS[status]
    if item.get("duplicate"):
        return "同票冲突"
    if not item.get("verification", {}).get("passed"):
        return "核验异常"
    if not item.get("company_match"):
        return "公司不匹配"
    return INVOICE_STATUS[status]
