from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from functools import reduce
from math import gcd
import re


class UserError(ValueError):
    pass


def money(value: str | int | Decimal) -> int:
    """Parse yuan strictly. Never round away a fractional cent."""
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0 or amount > Decimal("100000000"):
            raise UserError("金额须大于 0 且不超过 1 亿元。红字发票暂不参与选票。")
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise UserError("金额最多保留两位小数。")
        return int(cents)
    except (InvalidOperation, TypeError):
        raise UserError("请输入有效的人民币金额。") from None


def yuan(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def normalize_name(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")


def comparable(field: str, value: str) -> str:
    """Normalize one extracted field for equality checks.

    Full/half-width brackets, stray whitespace and tax id casing are
    harmless differences between a PDF text layer and local OCR, so they
    must never be reported as inconsistent originals.
    """
    if field in {"buyer_name", "seller_name"}:
        return normalize_name(value)
    if field == "buyer_tax_id":
        return "".join(value.split()).upper()
    return value


def identity(invoice: dict) -> str | None:
    number = invoice.get("number", "")
    code = invoice.get("code", "")
    if re.fullmatch(r"\d{20}", number):
        return "digital:" + number
    if re.fullmatch(r"\d{8}", number) and re.fullmatch(r"\d{10}|\d{12}", code):
        return code + ":" + number
    return None


def validate_invoice(fields: dict) -> dict:
    cleaned = {k: str(fields.get(k, "")).strip() for k in
               ("number", "code", "date", "buyer_name", "buyer_tax_id", "seller_name", "note")}
    if not re.fullmatch(r"\d{8}|\d{20}", cleaned["number"]):
        raise UserError("请核对发票号码：支持 8 位传统票号或 20 位数电票号。")
    if len(cleaned["number"]) == 8 and not re.fullmatch(r"\d{10}|\d{12}", cleaned["code"]):
        raise UserError("8 位票号还需要填写 10 位或 12 位发票代码，用于避免重复使用。")
    try:
        cleaned["date"] = date.fromisoformat(cleaned["date"]).isoformat()
    except ValueError:
        raise UserError("请填写有效开票日期。") from None
    if not cleaned["buyer_name"]:
        raise UserError("请填写购买方抬头。")
    tax_id = re.sub(r"\s+", "", cleaned["buyer_tax_id"]).upper()
    if tax_id and not re.fullmatch(r"[A-Z0-9]{15,20}", tax_id):
        raise UserError("请核对购买方税号：须为 15 至 20 位字母或数字。")
    cleaned["buyer_tax_id"] = tax_id
    cleaned["amount_cents"] = money(fields.get("amount", ""))
    return cleaned


def company_matches(invoice: dict, company: dict) -> bool:
    if not company.get("name") and not company.get("tax_id"):
        return False
    if company.get("tax_id") and invoice.get("buyer_tax_id") != company["tax_id"]:
        return False
    if company.get("name") and normalize_name(invoice.get("buyer_name", "")) != normalize_name(company["name"]):
        return False
    return True


def eligible_invoice(item, *, company_match, duplicate):
    return (item.get("verification", {}).get("passed", False) and not item.get("archived")
            and not duplicate and company_match and item.get("reimbursement_status") in {"available", "returned"})


def choose_invoices(invoices: list[dict], target: int, force_ids: list[str] | None = None,
                    exclude_ids: list[str] | None = None) -> dict:
    """Exact subset sum with integer cents, gcd compression and a memory budget."""
    if target <= 0 or any(i.get("amount_cents", 0) <= 0 for i in invoices):
        raise UserError("目标金额与候选发票金额必须大于零。")
    candidate_ids = {i["id"] for i in invoices}
    if len(candidate_ids) != len(invoices):
        raise UserError("候选发票不能重复。")
    forced, excluded = set(force_ids or []), set(exclude_ids or [])
    if forced & excluded:
        raise UserError("同一张票不能同时必选和排除。")
    if not forced <= candidate_ids:
        raise UserError("必选发票已不可用，请刷新后重试。")
    base = [i for i in invoices if i["id"] in forced]
    omitted = forced | excluded
    rest = sorted((i for i in invoices if i["id"] not in omitted),
                  key=lambda i: (i.get("date", ""), i["id"]))
    base_total = sum(i["amount_cents"] for i in base)
    available = base_total + sum(i["amount_cents"] for i in rest)
    if available < target:
        return {"ids": [], "total_cents": 0, "target_cents": target,
                "available_cents": available, "shortfall_cents": target - available, "optimal": False}
    need = target - base_total
    selected = list(base)
    if need > 0:
        unit = reduce(gcd, (i["amount_cents"] for i in rest))
        scaled_need = (need + unit - 1) // unit
        scaled = sorted(i["amount_cents"] // unit for i in rest)
        # 任何可行组合的总和都是最优解上界，取三种贪心里最紧的一个，
        # 让 bitset 只覆盖到“够用就好”的金额，减少位宽和计算量。
        ascending = 0
        for amount in scaled:
            ascending += amount
            if ascending >= scaled_need:
                break
        descending = 0
        for amount in reversed(scaled):
            descending += amount
            if descending >= scaled_need:
                break
        singles = [amount for amount in scaled if amount >= scaled_need]
        bound = min(ascending, descending, *singles)
        rest = [i for i in rest if i["amount_cents"] // unit <= bound]
        if bound > 20_000_000 or (bound + 8) // 8 * (len(rest) + 1) > 64_000_000:
            raise UserError("本次精确选票超过内存预算。请排除部分发票或降低目标后重试；本次未返回近似推荐。")
        mask = (1 << (bound + 1)) - 1
        prefixes = [1]
        for i in rest:
            bits = prefixes[-1]
            prefixes.append((bits | (bits << (i["amount_cents"] // unit))) & mask)
        above = prefixes[-1] >> scaled_need
        best = scaled_need + (above & -above).bit_length() - 1
        remaining = best
        for n in range(len(rest) - 1, -1, -1):
            if not (prefixes[n] >> remaining) & 1:
                selected.append(rest[n])
                remaining -= rest[n]["amount_cents"] // unit
        assert remaining == 0
    total = sum(i["amount_cents"] for i in selected)
    return {"ids": [i["id"] for i in selected], "total_cents": total, "target_cents": target,
            "excess_cents": total - target, "available_cents": available,
            "shortfall_cents": 0, "optimal": True}
