# V5, перший етап: ETA з прогнозом черг

## Що змінюється

Одна гіпотеза: використання наявного FIFO-прогнозу для конкретного обсягу
відправлення дозволить обирати придатні морські маршрути замість відхиляти
їх через невідомий ETA або платити за пряму альтернативу.

Це продовження V3/V4 і перша частина V5 roadmap. Майбутні оголошені закриття,
Early Warnings, рішення чекати відкриття та диверсифікація сюди не входять.
Немає зміни потреб Маркіяна, контрактів Нікіти чи default policy.

Після fetch усіх гілок network-delivery оновлена fast-forward до main a0b48e2,
яка вже містить попередню роботу через merged PR #1 з analytics. Integration
48acf87 і analytics 43d7cb6 входять у цю історію.

Під час роботи з'явилася analytics 1bfb7ff з новим production NeedPlanner,
pipeline metadata і retrospective backtest; її також злито в network-delivery.
Нові optional remaining_edges/remaining_route_weeks заповнюються для proposed
pipeline. Route weeks — номінальний залишок без черг, не прогнозований ETA.

## Реалізація

Новий `delivery_eta.py` — адаптер мережі/доставки до існуючого
`QueueForecaster.forecast(..., proposed_pipeline=...)`. Сам engine
`queue_forecast.py`, state.py, needs.py і contracts.py у тасці ETA не
перероблялися; їхні нові версії підтягнуто через окремий merge analytics.

Для кандидата allocator спочатку визначає доступну кількість за поточними
stock, entry capacity і fleet. Морський кандидат стає гіпотетичним PipelineLot
на першому ребрі; його arrival_week означає head цього ребра, не кінцевий
пункт. Прогноз разом моделює видимі pipeline, queues, уже вибрані відправлення
і одного кандидата. Взаємовиключні альтернативи не моделюються одночасно.

Модель FIFO враховує cohort competition, next-edge capacity, pool throughput
і флот. Для майбутніх ребер адаптер передає поточний observed graph_now.tau
замість незмінного nominal transit. Поточні rates/transit/prohibitions
умовно зберігаються; невідомі майбутні події та наступні тижневі dispatch
не передбачаються. Це оцінка, а не гарантія або бронювання.

Кінцевий completion_week у прогнозі стосується всього запропонованого обсягу.
Allocator не додає до нього ще один batch delay. Ціна, порядок need_order_key
та критерій відомого вчасного/пізнього кандидата залишені як у V1.

Після вибору вантаж включається у наступні прогнози. Якщо новий кандидат
відсуває completion уже вибраного вантажу, його оцінка відхиляється з
queue_eta_delays_selected_shipment. Це консервативний захист від мовчазної
зміни ETA попередніх потреб; він може залишати частину capacity невикористаною.

Неповний own state, прихований transit, unresolved completion або вичерпаний
forecast budget не стають нульовою чергою чи nominal ETA. Морський кандидат
лишається невідомим; пряма допустима альтернатива все ще розглядається.

## Поля й межі CPU

Вхід: StateSnapshot.week/horizon, available_stock, pipeline, queues, issues;
DeliveryNeed.quantity/due_week/priority/shortage_cost_per_unit_usd; StaticNetwork
та graph_now.u/tau/open/kappa.tb/kappa.ct/prohibited разом з observed-масками.
Для existing own-state visibility використовується stock.qty.observed.
Ціна, санкції та поточний dispatch контролюються існуючими модулями.

Кеш належить одному allocate, keyed by slot/quantity при фіксованих вибраних
відправленнях. Після прийняття морського кандидата кеш очищається. Ліміт:
16 joint forecasts за allocate, горизонт до 24 тижнів, не далі T. Досягнення
лімітів дає явну причину невідомого ETA. Повтор allocate має нові бюджети.
Ці обмеження скорочують роботу, але не є доказом CPU на всіх можливих станах.

## Увімкнення й діагностика

У params.json потрібні обидва booleans:

```json
{"allocation_enabled": true, "queue_eta_enabled": true}
```

Без queue_eta_enabled allocator зберігає V1. Без allocation_enabled Agent
зберігає heuristic. Обидві зміни лишаються opt-in до парного оцінювання.

AllocationResult і його види ресурсів не змінені; release стандартний.
queue_eta_estimate пояснює conditional completion; queue_eta_forecast_usage
містить число прогнозів, горизонт і причини відхилень. UnmetNeed теж містить
конкретну причину, якщо доступні кандидати не мають придатної оцінки.

## Перевірка й відтворення

```text
uv run pytest tests/test_allocation.py -n 3 -q
uv run python examples/10_network_allocation.py --task=small --queue_eta=True
uv run python examples/10_network_allocation.py --task=full --queue_eta=True
```

При queue_eta=True приклад заморожує один source: baseline — allocation V1,
candidate — той самий V1 з FIFO ETA. Для кожного веде окрему траєкторію того
самого сценарію. При queue_eta=False лишається попереднє heuristic/V1 порівняння.

Нові тести: порожня/завантажена черга, inbound competition без доступного
stock, full-quantity completion без подвійного batching, shared queue для
двох вибраних відправлень, захист попереднього ETA, приховані own-state/tau,
ліміт кількості/горизонту, детермінованість, nominal unclipped sea dispatch
на Tiny/Small/Full і packed Agent з увімкненим параметром.

Спільна регресія network/delivery/allocation/Agent/contracts/analytics/evaluation:
150 passed, 3 skipped (Unix Bash orchestration). Після додавання ще трьох
сценаріїв окремо підтверджений фінальний allocator suite: 37 passed.
Ruff check/format і git diff --check пройшли.

Повні native епізоди ДО злиття analytics 1bfb7ff, 2026-10-06,
entropy 12345, episode 0 (історичні дані для незмінних старих needs):

| Мережа | Тижнів | Витрати V1, USD | Витрати з FIFO ETA, USD | Різниця | Max CPU кандидата | Clipped V1 / ETA |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Tiny | 26 | 2,983,847,536.85 | 2,799,033,566.87 | -6.194% | 0.046875 с | 2 / 1 |
| Small | 52 | 5,168,010,190,930.98 | 5,144,692,309,916.67 | -0.451% | 0.140625 с | 2 / 2 |
| Full | 104 | 15,151,628,946,225.86 | 15,149,797,178,587.97 | -0.012% | 0.250000 с | 6 / 6 |

Ініціалізація включена в перший тиждень. Морських призначень з прогнозом:
42 / 84 / 124 відповідно. Reports, source folders і fingerprints знаходяться
в outputs/10_network_allocation/2026-10-06_queue_eta_{tiny,small,full}.
Це по одному сценарію, сирі витрати та локальний CPU, не RSS або server meter.
Ці результати також не доводять перевагу повного allocator над heuristic.

Парна перевірка в середовищі з робочим official runner:

```text
uv run sbf compare <frozen_FIFO_ETA> <frozen_V1> --task=small --entropy=12345 --episodes=16 --cpu_budget=True
uv run sbf compare <frozen_FIFO_ETA> <frozen_V1> --task=full --entropy=12345 --episodes=4 --cpu_budget=True
```

Далі повторити на validation root 67890; root 0/dev зберегти для рідкісного
підтвердження. Не міняти needs/risk під час цього порівняння.

Статична sbf check packed FIFO-кандидата після merge analytics пройшла:
15 файлів, дозволені imports.
Ізольований check і sbf compare на Windows зупиняються до scoring через fcntl.
Загальний pytest з трьома workers і --maxfail=3: 40 passed, 3 skipped, 6 failed;
5 failures — той самий fcntl, ще 1 — Unix chmod 0600 у тесті Codabench на Windows.
Benchmark/runner і його platform tests у цій тасці не змінювалися.

## Фінальний стан після merge analytics 1bfb7ff

Регресія спільних модулів повторена: **156 passed, 3 skipped**.
Однакові нові needs використані і в baseline V1, і в candidate FIFO ETA;
зміна виробничих потреб не змішується з ефектом увімкнення прогнозу черг.
Повні native епізоди, той самий entropy 12345/episode 0:

| Мережа | Витрати V1, USD | Витрати з FIFO ETA, USD | Різниця | Max CPU кандидата | Clipped V1 / ETA |
| --- | ---: | ---: | ---: | ---: | ---: |
| Tiny | 2,888,291,263.98 | 2,735,281,615.63 | -5.298% | 0.078125 с | 1 / 0 |
| Small | 5,265,081,815,919.36 | 5,232,605,468,633.12 | -0.617% | 0.140625 с | 0 / 2 |
| Full | 15,334,467,096,343.70 | 15,328,037,896,111.97 | -0.042% | 0.218750 с | 11 / 11 |

Морських призначень з прогнозом: 34 / 108 / 86. Поточні frozen files, hashes
і reports: outputs/10_network_allocation/2026-10-06_queue_eta_analytics_{tiny,small,full}.
Усі попередні обмеження щодо одного сценарію, RSS і server CPU збережені.

Залишкові clip-події досліджено окремо, без передачі прихованих marks агенту.
Інформація graph_now.u — instantaneous capacity на t-1, а simulator dispatch
читає week-average marks.u. На Small/week 4 slots 76/78 і Full/week 67 slots
70/71/72/73/96/97 requested не перевищує видиму instantaneous capacity, але
executed точно дорівнює меншій фізичній week-average capacity. Отже observed=1
не гарантує незмінність ресурсу протягом тижня. Причина решти п'яти Full clips
на week 95 окремо не встановлена; вони збережені в report.

Наступний окремий етап V5 має перевірити використання оголошених початків/кінців
обмежень для відповідних дат проходження маршруту. Шумний Early Warning
або невидима майбутня подія не можуть стати відомим week-average значенням.
