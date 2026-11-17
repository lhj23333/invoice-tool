from .domain import UserError, normalize_name, validate_invoice, yuan
from .extract import extract


FIELDS = ("number", "code", "date", "amount_cents", "buyer_name", "buyer_tax_id", "seller_name")
REQUIRED = ("number", "date", "amount_cents", "buyer_name")
TITLES = {"number": "发票号码", "code": "发票代码", "date": "开票日期", "amount_cents": "价税合计",
          "buyer_name": "购买方抬头", "buyer_tax_id": "购买方税号", "seller_name": "销售方名称"}


def comparable(field, value):
    if field in {"buyer_name", "seller_name"}:
        return normalize_name(value)
    if field == "buyer_tax_id":
        return "".join(value.split()).upper()
    return value


def verify_record(item, original):
    issues, evidence = [], {}
    for file in item["files"]:
        try:
            path = original(file)
            parsed = extract(path.read_bytes(), path.suffix.lower())
        except (UserError, OSError) as exc:
            issues.append(f"{file['name']}：{exc}")
            continue
        issues.extend(f"{file['name']}：{warning}" for warning in parsed.get("warnings", []))
        for field in FIELDS:
            value = parsed.get(field)
            if value:
                normalized = comparable(field, value)
                if field in evidence and evidence[field] != normalized:
                    issues.append(f"原件之间的{TITLES[field]}不一致。")
                evidence[field] = normalized
                if comparable(field, item.get(field, "")) != normalized:
                    issues.append(f"记录的{TITLES[field]}与原件不一致。")
    for field in REQUIRED:
        if field not in evidence:
            issues.append(f"原件未能提取{TITLES[field]}。")
    if len(item.get("number", "")) == 8 and "code" not in evidence:
        issues.append("原件未能提取发票代码。")
    cleaned = {}
    try:
        cleaned = validate_invoice({**item, "amount": yuan(item["amount_cents"])})
    except UserError as exc:
        issues.append(str(exc))
    # 手工补填的关键字段也必须能在原件中找到对应内容。
    for field in ("buyer_tax_id", "code"):
        if item.get(field) and field not in evidence:
            issues.append(f"原件无法验证{TITLES[field]}。")
    if not issues:
        item.update(cleaned)
    return {"passed": not issues, "issues": list(dict.fromkeys(issues))}
