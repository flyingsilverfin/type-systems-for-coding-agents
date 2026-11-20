"""Output for members, member, contact, inactive, top, fees, member-fees."""
from __future__ import annotations

from collections import Counter

from lending.model import LoanFee, Member, MemberContact, MemberFee, MemberLoan, MemberName
from lending.report import or_none


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def members(items: list[MemberContact]) -> list[str]:
    ordered = sorted(items, key=lambda m: m.id)
    return [f"{m.id} {m.name} {or_none(m.email)} {or_none(m.phone)}" for m in ordered]


def member(m: Member, loans: list[MemberLoan]) -> list[str]:
    open_loans = sorted((loan for loan in loans if loan.is_open()), key=lambda loan: (loan.due, loan.id))
    lines = [
        f"member {m.id}: {m.name}",
        f"email: {or_none(m.email)}",
        f"phone: {or_none(m.phone)}",
        f"joined: {m.joined.isoformat()}",
        f"open loans: {len(open_loans)}",
    ]
    lines += [f"  {loan.id} {loan.isbn} due {loan.due.isoformat()}" for loan in open_loans]
    lines.append(f"lost loans: {sum(1 for loan in loans if loan.is_lost())}")
    return lines


def contact(m: MemberContact) -> list[str]:
    via = m.email or m.phone or "(no contact)"
    return [f"{m.name} {via}"]


def inactive(items: list[MemberName]) -> list[str]:
    return [f"{m.id} {m.name}" for m in sorted(items, key=lambda m: m.id)]


def top(borrowers: list[MemberName], limit: int) -> list[str]:
    counts = Counter(b.id for b in borrowers)
    names = {b.id: b.name for b in borrowers}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [f"{rank}. {mid} {names[mid]} ({count} loans)" for rank, (mid, count) in enumerate(ranked, start=1)]


def fees(items: list[MemberFee]) -> list[str]:
    totals: Counter[str] = Counter()
    names: dict[str, str] = {}
    for item in items:
        totals[item.member_id] += item.fee_cents
        names[item.member_id] = item.name
    ranked = sorted(((mid, total) for mid, total in totals.items() if total > 0), key=lambda kv: (-kv[1], kv[0]))
    return [f"{mid} {names[mid]} {money(total)}" for mid, total in ranked]


def member_fees(member_id: str, items: list[LoanFee]) -> list[str]:
    charged = sorted((item for item in items if item.fee_cents > 0), key=lambda item: item.loan_id)
    lines = [f"fees {member_id}: {money(sum(item.fee_cents for item in charged))}"]
    lines += [f"  {item.loan_id} {money(item.fee_cents)}" for item in charged]
    return lines
