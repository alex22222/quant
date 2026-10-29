"""Versioned risk decisions; prose never grants a market-gate exception."""
from copy import deepcopy

from trading_team.decision_pipeline import APPROVAL_FIELDS
from trading_team.order_conditions import finite_number
from trading_team.schemas import is_buy, validate_plan

RISK_CONTRACT_VERSION = "a_share_risk.v1"
RISK_POLICY_VERSION = "structured_risk.v1"
REASON_CODES = frozenset({"market_gate", "position_limit", "portfolio_limit", "stop_distance",
                          "liquidity", "data_quality", "thesis_invalidated", "other"})
RESERVED_PROPOSAL_FIELDS = {*APPROVAL_FIELDS, "approval_source", "risk_review", "risk_clause"}
REVIEW_FIELDS = {"code", "direction", "decision", "reason_codes", "position_pct", "reason", "clause"}


def _fail(detail):
    raise ValueError("risk contract: " + detail)


def validate_review(review: dict, proposal: dict, market_ok: bool | None) -> None:
    if not isinstance(review, dict) or set(review) - REVIEW_FIELDS:
        _fail("review must contain only documented fields")
    if review.get("code") != proposal.get("code"):
        _fail("review code differs from proposal")
    if "direction" in review and review["direction"] != proposal.get("direction"):
        _fail("review direction differs from proposal")
    decision = review.get("decision")
    reasons = review.get("reason_codes")
    if (not isinstance(reasons, list) or any(not isinstance(code, str) or code not in REASON_CODES for code in reasons)
            or len(set(reasons)) != len(reasons)):
        _fail("reason_codes must be a unique array of known codes")
    if decision not in ("通过", "降仓通过", "否决"):
        _fail("unknown review decision")
    if (decision == "通过") != (not reasons):
        _fail("pass requires no adverse codes; reduce/reject requires reasons")
    if not isinstance(review.get("reason", ""), str) or not isinstance(review.get("clause", ""), str):
        _fail("reason and clause must be text")
    if decision != "通过" and not review.get("reason", "").strip():
        _fail("reduce/reject requires an explanation")
    pct = proposal.get("position_pct")
    buy = is_buy(str(proposal.get("direction", "")), pct) and pct > 0 and proposal.get("exit") is None
    if "market_gate" in reasons and (market_ok is not False or not buy or decision != "否决"):
        _fail("market_gate is a rejection of a buy under a closed deterministic gate only")
    if decision == "降仓通过":
        reduced = review.get("position_pct")
        if not buy or not finite_number(reduced) or not 0 < reduced < pct:
            _fail("降仓后仓位未降低 or invalid positive buy target")
    elif "position_pct" in review:
        _fail("only a reduction may supply position_pct")


def _reviewed(proposal, review):
    plan = deepcopy(proposal)
    if review["decision"] == "降仓通过":
        plan["position_pct"] = review["position_pct"]
    elif review["decision"] == "否决":
        plan.update(direction="回避", position_pct=0.0, entry=None, stop=None)
        plan.pop("exit", None)
        plan.pop("protection", None)
    if review.get("clause", "").strip():
        plan["risk_clause"] = review["clause"].strip()
    if review.get("reason", "").strip():
        plan["note"] = "风控：" + review["reason"].strip()
    plan["risk_review"] = {"contract": RISK_CONTRACT_VERSION, **deepcopy(review)}
    return plan


def apply_risk_reviews(proposals: list[dict], reviews: list[dict], *, market_ok: bool | None = None) -> list[dict]:
    if not isinstance(proposals, list) or not isinstance(reviews, list):
        _fail("proposals and reviews must be arrays")
    for proposal in proposals:
        if not isinstance(proposal, dict) or RESERVED_PROPOSAL_FIELDS & proposal.keys():
            _fail("trader proposal contains reserved risk or approval fields")
        if not finite_number(proposal.get("position_pct")) or validate_plan(proposal, strict=True):
            _fail("invalid source proposal")
    if any(not isinstance(review, dict) or not isinstance(review.get("code"), str) for review in reviews):
        _fail("every review requires a string code")
    by_code = {review["code"]: review for review in reviews}
    codes = [proposal["code"] for proposal in proposals]
    if len(by_code) != len(reviews) or len(set(codes)) != len(codes) or set(by_code) != set(codes):
        _fail("reviews must uniquely and completely cover proposals")
    final = []
    for proposal in proposals:
        review = by_code[proposal["code"]]
        validate_review(review, proposal, market_ok)
        final.append(_reviewed(proposal, review))
    return final


def derive_paper_proposals(trader: list[dict], final: list[dict], *, market_ok: bool | None = None) -> list[dict]:
    raw_by_code = {plan["code"]: plan for plan in trader}
    derived = []
    for reviewed in final:
        metadata = reviewed.get("risk_review")
        if metadata is None:
            derived.append(deepcopy(reviewed))
            continue
        if not isinstance(metadata, dict) or metadata.get("contract") != RISK_CONTRACT_VERSION:
            _fail("unknown materialized review contract")
        raw = raw_by_code.get(reviewed.get("code"))
        if raw is None:
            _fail("original trader proposal missing")
        review = {key: value for key, value in metadata.items() if key != "contract"}
        expected = apply_risk_reviews([raw], [review], market_ok=market_ok)[0]
        if reviewed != expected:
            _fail("reviewed proposal differs from deterministic derivation")
        if review["decision"] == "否决" and review["reason_codes"] == ["market_gate"]:
            derived.append({**deepcopy(raw), "risk_review": deepcopy(metadata)})
        else:
            derived.append(deepcopy(reviewed))
    return derived


def build_reviewed_plan(day: str, data_asof: str, market_ok: bool,
                        proposals: list[dict], reviews: list[dict]) -> dict:
    if not isinstance(market_ok, bool):
        _fail("market_ok must be a deterministic boolean")
    final = apply_risk_reviews(proposals, reviews, market_ok=market_ok)
    return {"date": day, "data_asof": data_asof, "market_ok": market_ok,
            "risk_contract": RISK_CONTRACT_VERSION, "trader_proposals": deepcopy(proposals),
            "risk_reviews": deepcopy(reviews), "proposals": final,
            "paper_proposals": derive_paper_proposals(proposals, final, market_ok=market_ok)}


def validate_risk_document(document: dict) -> bool:
    """False means legacy evidence missing; malformed declared contracts raise."""
    if not any(key in document for key in ("risk_contract", "trader_proposals", "risk_reviews")):
        return False
    if document.get("risk_contract") != RISK_CONTRACT_VERSION:
        _fail("missing or unsupported document contract")
    expected = build_reviewed_plan(document.get("date"), document.get("data_asof"), document.get("market_ok"),
                                    document.get("trader_proposals"), document.get("risk_reviews"))
    if any(document.get(key) != expected[key] for key in ("proposals", "paper_proposals")):
        _fail("derived proposals differ from original intent and risk reviews")
    return True


def risk_review_summary(document: dict) -> dict:
    result = {"status": "legacy_missing", "contract": document.get("risk_contract"),
              "reviewed_count": None, "passed_count": None, "reduced_count": None,
              "gate_only_count": None, "risk_rejected_count": None, "reviews": []}
    try:
        if not validate_risk_document(document):
            return result
    except ValueError as exc:
        return {**result, "status": "invalid", "error": str(exc)}
    reviews = document["risk_reviews"]
    gate_only = sum(review["decision"] == "否决" and review["reason_codes"] == ["market_gate"] for review in reviews)
    result.update(status="validated_contract", reviewed_count=len(reviews),
                  passed_count=sum(review["decision"] == "通过" for review in reviews),
                  reduced_count=sum(review["decision"] == "降仓通过" for review in reviews),
                  gate_only_count=gate_only,
                  risk_rejected_count=sum(review["decision"] == "否决" for review in reviews) - gate_only,
                  reviews=deepcopy(reviews))
    return result
