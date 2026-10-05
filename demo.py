"""Live demo of the Agent3-based agent ('mine').

Runs an episode week-by-week and prints the agent's internal reasoning:
- Warning levels & estimated risk
- Projected inventory & shortage detection
- Route scoring & flows allocated
- Weekly cost breakdown

Usage:
    uv run python demo.py
    uv run python demo.py --task=small --episode=0
"""

import fire
import gymnasium as gym
import numpy as np
from shockbench_flow_agent import agent_config
import shockbench_flow_gym  # noqa: F401

from sbf_starter import env_id
from sbf_starter.agents import load


def main(task: str = "tiny", episode: int = 0) -> None:
    print(f"\n" + "=" * 80)
    print(f"🚀 ЗАПУСК ДЕМО АГЕНТА (ShockBench Team Roadmap) | Мережа: {task.upper()} | Епізод: {episode}")
    print("=" * 80 + "\n")

    env = gym.make(env_id(task))
    obs, info = env.reset(options={"episode": episode})

    # Завантаження нашого агента
    agent_cls = load("mine")
    cfg = agent_config(info["static"], info["policy_seed"], env.unwrapped.layout, obs)
    agent = agent_cls(cfg)

    total_cost = 0.0
    terminated, truncated = False, False

    while not (terminated or truncated):
        current_week = int(obs["week"][0])

        # Отримуємо внутрішні оцінки Маркіяна перед кроком
        snapshot = agent.state_mgr.capture_snapshot(obs)
        risk_state = agent.risk_analyzer.analyze_risks(obs)
        global_risk = risk_state.global_risk
        delivery_needs = agent.needs_forecaster.generate_delivery_needs(obs, snapshot, global_risk)

        # Дія агента
        action = agent.act(obs)
        flows = action["flows"]

        # Крок середовища
        obs, reward, terminated, truncated, info = env.step(action)
        week_cost = -reward
        total_cost += week_cost

        # Виводимо аналітику за тиждень
        active_flows = int(np.sum(flows > 0))
        total_flow_vol = float(np.sum(flows))
        open_fracs = obs.get("graph_now.open", [1.0])
        min_open = float(np.min(open_fracs)) if len(open_fracs) > 0 else 1.0

        status_emoji = "🟢" if global_risk < 0.25 else ("🟡" if global_risk < 0.55 else "🔴")

        # Пріоритетні заявки
        urgent_needs = [n for n in delivery_needs if n.priority <= 2]
        urgent_qty = sum(n.qty for n in urgent_needs)

        # Прогноз черги попереду (V3)
        max_q_delay = 0.0
        for q_fc in snapshot.queue_forecasts.values():
            if q_fc.expected_delay_weeks > max_q_delay:
                max_q_delay = q_fc.expected_delay_weeks

        print(
            f"Тиждень {current_week:2d} | "
            f"{status_emoji} Ризик: {global_risk:.2f} | "
            f"Протоки: {min_open * 100:3.0f}% | "
            f"Запас (підтв): {snapshot.total_confirmed_stock:8,.0f} | "
            f"В дорозі (оцін): {snapshot.total_estimated_arrivals:8,.0f} | "
            f"Заявки: {len(delivery_needs):2d} (терм: {urgent_qty:6,.0f}) | "
            f"Черга delay: +{max_q_delay:.1f}т | "
            f"Витрати: ${week_cost:11,.0f}"
        )

        # На першому тижні покажемо детальну роздруківку структури DeliveryNeed та StateSnapshot
        if current_week == 1 and delivery_needs:
            print("\n  🔍 [Приклад StateSnapshot & DeliveryNeed на Тижні 1]:")
            print(f"     Підтверджений запас: {snapshot.total_confirmed_stock:,.1f} одиниць")
            print(f"     Оцінені надходження в дорозі: {snapshot.total_estimated_arrivals:,.1f} одиниць")
            print(f"     Поточний backlog: {snapshot.total_backlog:,.1f} одиниць")
            print(f"     Всього заявок DeliveryNeed: {len(delivery_needs)}")
            for idx, nd in enumerate(delivery_needs[:3]):
                print(
                    f"     -> Need #{idx+1}: [{nd.need_id}] {nd.destination_name} | {nd.commodity_name} | "
                    f"К-сть: {nd.qty:,.1f} | Due: тижд {nd.due_week} | Пріоритет: {nd.priority} ({nd.reason}) | "
                    f"Штраф: ${nd.shortage_cost_est:,.0f} | Впевненість: {nd.confidence:.0%}"
                )
            for (chk_node, pool), q_fc in list(snapshot.queue_forecasts.items())[:2]:
                print(
                    f"     -> V3 Queue Forecast [{q_fc.chokepoint_name} / pool {pool}]: "
                    f"Черга попереду: {q_fc.work_ahead_total:,.0f} од. | "
                    f"Пропускна здатність: {q_fc.effective_throughput:,.0f}/тижд | "
                    f"Очікувана затримка: {q_fc.expected_delay_weeks:.2f} тижнів"
                )
            print()

    print("\n" + "=" * 80)
    print(f"🏁 СИМУЛЯЦІЮ ЗАВЕРШЕНО")
    print(f"📊 Загальні витрати за епізод: ${total_cost:,.0f} USD")
    print(f"ℹ️  Для порівняння з іншими агентами запустіть:")
    print(f"   uv run sbf compare mine heuristic --quick")
    print(f"   uv run sbf compare mine template --quick")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    fire.Fire(main)
