"""Customer Support (#19, support cluster) - draft responses + triage.

Turns a buyer's questions into a support case and a DRAFT reply. It
never sends anything (`auto_sent` is always False) and it minimises
exposed personal data: the customer is referenced by first name + a
masked email unless a field is genuinely needed to answer.
"""

from __future__ import annotations

import re

from .agent import Agent
from .messages import Result, Task

_CATEGORIES = (
    ("refund", ("refund", "money back", "cancel", "chargeback")),
    ("delivery", ("when", "how long", "not received", "eta", "delivery", "waiting")),
    ("scope", ("what do i get", "include", "does it cover", "scope", "expect")),
    ("technical", ("link", "broken", "error", "can't open", "download", "pdf")),
)
_ESCALATE = ("refund", "chargeback", "legal", "lawyer", "complaint", "scam", "fraud")

_TEMPLATES = {
    "refund": "Your Customer Launch Plan can be refunded in full via PayPal if it "
              "has not been delivered. Confirm the order id ({order}) and a human "
              "will process it.",
    "delivery": "Your plan for order {order} is prepared as a personalised PDF "
                "within 3 business days of your intake form. If that window has "
                "passed, a human will follow up today.",
    "scope": "The plan covers: business & customer analysis, 5-10 acquisition "
             "opportunities, a prioritised strategy, a 14-day action plan and "
             "outreach templates. It is research and strategy, not a guarantee "
             "of customers.",
    "technical": "Sorry about the trouble with order {order}. A human will re-send "
                 "your plan file to your PayPal email.",
    "other": "Thanks for reaching out about order {order}. A human will reply "
             "shortly.",
}


def _mask_email(email: str) -> str:
    m = re.match(r"^([^@])[^@]*@([^.]).*(\.[^.]+)$", str(email or ""))
    return f"{m.group(1)}***@{m.group(2)}***{m.group(3)}" if m else ""


def _classify(text: str) -> str:
    t = str(text).lower()
    for name, kws in _CATEGORIES:
        if any(k in t for k in kws):
            return name
    return "other"


def build_support_case(intake: dict, questions, order: dict | None = None) -> dict:
    intake = intake or {}
    order = order or {}
    qs = [str(q).strip() for q in (questions or []) if str(q).strip()]
    if not qs and intake.get("question"):
        qs = [str(intake["question"]).strip()]

    joined = " ".join(qs)
    category = _classify(joined) if qs else "other"
    order_id = str(order.get("order_id") or intake.get("order_id") or "")
    first_name = str(intake.get("name") or "").split(" ")[0]
    escalate = [w for w in _ESCALATE if w in joined.lower()]

    response = _TEMPLATES[category].format(order=order_id or "<order id>")

    required_action = {
        "refund": "human: process refund via PayPal (read-only agent cannot)",
        "delivery": "human: confirm the plan is on track / send it",
        "scope": "human: send the scope reply, no action needed",
        "technical": "human: re-send the plan file",
        "other": "human: review and reply",
    }[category]

    return {
        "support_case": {
            "id": f"case-{order_id}" if order_id else "case-unknown",
            "category": category,
            "priority": "high" if escalate else ("normal" if qs else "low"),
            "customer_ref": {"first_name": first_name,
                             "email_masked": _mask_email(intake.get("email"))},
            "questions": qs,
        },
        "response_draft": response,
        "required_action": required_action,
        "escalation_reason": ("customer raised: " + ", ".join(escalate)) if escalate else "",
        "auto_sent": False,
        "delivery": "draft only - a human sends this from the business email",
        "data_minimised": True,
    }


class CustomerSupportAgent(Agent):
    role = "customer_support"
    objective = "Draft a support reply and triage; never contact the customer."
    capabilities = ("support_customers",)

    def run(self, task: Task) -> Result:
        intake = task.payload.get("intake")
        if not isinstance(intake, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['intake'] must be a dict")
        questions = task.payload.get("questions")
        if questions is not None and not isinstance(questions, list):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['questions'] must be a list when given")
        order = task.payload.get("order")
        if order is not None and not isinstance(order, dict):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="payload['order'] must be a dict when given")
        if not questions and not intake.get("question"):
            return Result(task_id=task.id, agent=self.name, status="error",
                          error="no customer question supplied")
        out = build_support_case(intake, questions, order)
        return Result(task_id=task.id, agent=self.name, status="ok", output=out)
