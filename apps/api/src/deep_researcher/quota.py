from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_researcher.model_gateway import BudgetExceededError
from deep_researcher.models import QuotaPolicy, ResearchRun, UsageLedger


def usd_to_micros(value: float) -> int:
    """把美元金额转换为整数微单位"""
    return int((Decimal(str(value)) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class QuotaService:
    """在单个事务内完成 Workspace 配额预留、结算和释放"""

    def __init__(self, *, token_limit: int, cost_limit_usd: float) -> None:
        self._token_limit = token_limit
        self._cost_limit_micros = usd_to_micros(cost_limit_usd)

    def reserve(
        self,
        session: Session,
        run: ResearchRun,
        *,
        token_budget: int,
        cost_budget_usd: float,
    ) -> None:
        """在模型调用前为运行预留 token 与费用预算"""
        if run.reservation_status != "none":
            return
        policy = self._get_or_create_policy(session, run.workspace_id)
        cost_budget_micros = usd_to_micros(cost_budget_usd)
        if (
            policy.used_tokens + policy.reserved_tokens + token_budget > policy.token_limit
            or policy.used_cost_micros + policy.reserved_cost_micros + cost_budget_micros
            > policy.cost_limit_micros
        ):
            raise BudgetExceededError("Workspace 预算不足")
        policy.reserved_tokens += token_budget
        policy.reserved_cost_micros += cost_budget_micros
        run.reservation_status = "reserved"
        run.reserved_token_budget = token_budget
        run.reserved_cost_micros = cost_budget_micros

    def settle(
        self,
        session: Session,
        run: ResearchRun,
        usage: Mapping[str, int | float] | None,
    ) -> None:
        """将模型实际用量写入台账并结算预留"""
        if run.reservation_status != "reserved":
            return
        policy = self._get_policy(session, run.workspace_id)
        input_tokens = int((usage or {}).get("input_tokens", 0))
        output_tokens = int((usage or {}).get("output_tokens", 0))
        total_tokens = int((usage or {}).get("total_tokens", input_tokens + output_tokens))
        cost_micros = usd_to_micros(float((usage or {}).get("cost_usd", 0.0)))
        policy.reserved_tokens = max(0, policy.reserved_tokens - run.reserved_token_budget)
        policy.reserved_cost_micros = max(
            0, policy.reserved_cost_micros - run.reserved_cost_micros
        )
        policy.used_tokens += total_tokens
        policy.used_cost_micros += cost_micros
        session.add(
            UsageLedger(
                workspace_id=run.workspace_id,
                run_id=run.id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cost_micros=cost_micros,
            )
        )
        run.reservation_status = "settled"

    def release(self, session: Session, run: ResearchRun) -> None:
        """在取消或失败时释放尚未结算的预算预留"""
        if run.reservation_status != "reserved":
            return
        policy = self._get_policy(session, run.workspace_id)
        policy.reserved_tokens = max(0, policy.reserved_tokens - run.reserved_token_budget)
        policy.reserved_cost_micros = max(
            0, policy.reserved_cost_micros - run.reserved_cost_micros
        )
        run.reservation_status = "released"

    def _get_or_create_policy(self, session: Session, workspace_id: UUID) -> QuotaPolicy:
        """读取或初始化 Workspace 配额策略"""
        policy = session.scalar(
            select(QuotaPolicy)
            .where(QuotaPolicy.workspace_id == workspace_id)
            .with_for_update()
        )
        if policy is None:
            policy = QuotaPolicy(
                workspace_id=workspace_id,
                token_limit=self._token_limit,
                cost_limit_micros=self._cost_limit_micros,
            )
            session.add(policy)
            session.flush()
        return policy

    def _get_policy(self, session: Session, workspace_id: UUID) -> QuotaPolicy:
        """读取已存在的 Workspace 配额策略"""
        policy = session.scalar(
            select(QuotaPolicy)
            .where(QuotaPolicy.workspace_id == workspace_id)
            .with_for_update()
        )
        if policy is None:
            raise RuntimeError("Workspace 配额策略不存在")
        return policy
