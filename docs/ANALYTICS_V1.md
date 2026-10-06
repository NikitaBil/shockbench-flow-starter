# Стан, потреби та прогноз черг

Оновлення 2026-10-06: ці модулі об'єднано з network V2/V3 та opt-in allocator V4.
Свіжі перевірки й виявлена прогалина заявок на поповнення терміналів описані в
[ALLOCATION_V4.md](ALLOCATION_V4.md). Default Agent досі працює heuristic;
новий режим увімкнений лише в заморожених експериментальних кандидатах.

Реалізація задачі state/needs у спільному `agents/team_agent`, без заміни
`agent.py`, канонічних контрактів або `network.py`. Старі файли Маркіяна з
review ZIP не копіювалися поверх каркаса. Модулі використовують лише stdlib
та NumPy; simulator/Gym потрібні тільки тестам.

## API

```python
from state import StateBuilder
from needs import NeedPlanner
from queue_forecast import QueueForecaster

# V1: кінцевий ETA через черги невідомий.
builder = StateBuilder(config)
planner = NeedPlanner(config)
snapshot = builder.build(observation, network)
needs = planner.plan(snapshot, observation, network)

# V3: умовна оцінка черг, увімкнена явно.
builder_v3 = StateBuilder(config, queue_forecaster=QueueForecaster(config))
snapshot_v3 = builder_v3.build(observation, network)
```

Модулі створюються один раз на епізод. Вони не читають env, runner, приховані
marks чи готовий план майбутніх disruptions. Індекси беруться з `config`.

## StateSnapshot

- `week`, `horizon`: абсолютний тиждень рішення та `config['T']`.
- `available_stock[(node, commodity)]`: `Quantity` для кожної пари
  `layout.stock_slots`. За реальною послідовністю simulator 0.1.2 dispatch
  використовує `I[t-1]`: звичайні надходження, supply lift та WIP цього тижня
  відбуваються пізніше. Їх не додаємо в ресурс для поточного dispatch.
- `backlog`: усі пари `layout.demands`; приховане значення лишається unknown.
- `pipeline`: живі поточні ребра, lane status, кількість, дата прибуття до head
  поточного ребра, відомий кінцевий пункт, решта edge-ів та номінальна ETA після
  поточного edge. Відома пара edge/lane перевіряється Вітиним `transit_progress`.
  ETA виключає поточне ребро та очікування в чергах. Hidden lane не стає lane 0. Off-lane можна
  встановити лише на ребрі, яке не належить жодній lane і не торкається choke.
- `queues`: Tiny padded lot list та Small/Full dense `(lot_key, cohort_week)`;
  observed mask застосовується до живих кількостей і метаданих.
- `wip`: спостережений gross WIP. У календарі його майбутній обсяг estimated,
  бо майбутній scrap не відомий.
- `arrivals`: `ExpectedArrival` з незалежним provenance кількості та дати.
  Прибуття на останньому ребрі має observed дату; через ще не пройдені черги
  без V3 дата `None`. Queue cargo з відомим пунктом не зникає через unknown ETA.
- `arrival_calendar[(node, commodity, week_or_None)]`: tuple окремих записів,
  не сума різних товарів і не додатковий запас. Повторне читання ідемпотентне.
- `supply_availability`: спостережена доступність supply, не фактичний lift.
  Lift залежить від storage та зроблених dispatches, тому availability не
  видається за гарантоване надходження.
- `issues`: метадані, які не дозволили відновити джерело/маршрут; не сховуються
  за нульовими значеннями. Невідома destination не отримує вигаданий календар.

`observed` означає дані observation, не гарантію майбутнього. `unknown` —
`value=None`, не числовий нуль. Snapshot не мутує вхідні масиви, mappings readonly.
ID pipeline/WIP детермінований у snapshot, але grouped wire не має стабільного
shipment ID між тижнями; не використовувати row ID як глобальний трекер вантажу.

## DeliveryNeed

Dataclass у `needs.py`: `need_id`, `destination_node`, `commodity_id`,
`quantity`, `due_week`, `priority`, `reason`,
`shortage_cost_per_unit_usd`, `confidence`; додатково `quantity_source`
та `assumptions`. Одиниці — native commodity units. Ціна — USD за одиницю
для одного тижня дефіциту, не загальна вартість.

Планувальник проходить вимоги у календарному порядку, витрачаючи покриття
один раз. Backlog має пріоритет перед попитом поточного тижня. Одне надходження
не додається повторно на кожному горизонті; прихований або нульовий попит
не втрачає надходження між відомими тижнями. Заявка позначає додатковий
непокритий обсяг, не обсяг кожного попереднього боргу знову.

Пріоритети: backlog 4, demand/grid fuel 3, production 2, safety stock 1.
Це початкова політика ранжування, не доведена оптимальна стратегія.
`need_order_key`: більше priority, раніший due_week, стабільний ID.
Безперервний backlog зберігає найраніший тиждень, коли його побачив цей planner;
це не твердження про недоступну дату початкового замовлення.

Джерела потреб:

- Sink demand — опублікований forecast, не realized demand. Грошова ставка
  береться з `static.sinks.pi`.
- Fab/OSAT inputs — лише від published downstream package forecast, обмежені
  observed `cap_eff`/`thr_eff`; без forecast capacity не створює потребу.
  OSAT не ділить throughput порівну. Номінальний Fab BOM використовує
  співвідношення input/output 1:1; `w_scr` — вік обліку scrap у тижнях, а
  `tau` — тривалість у тижнях. Жоден із них не є коефіцієнтом втрати матеріалу.
  BOM input buffer за замовчуванням дорівнює нулю; додатковий обсяг можна
  увімкнути окремим `SafetyBufferPolicy(input_buffer_fraction=...)`.
  `e` та публічна fuel share задають пов'язану потребу енергії/палива.
- Grid fuels — щотижнева потреба для static base load плюс рівномірно
  розкладена по горизонту grounded Fab energy, обмежена поточним deliverable
  `G_bar`, за public fuel shares. Marginal shortage cost для sink дорівнює
  `static.sinks.pi` (USD/native unit/week). Для grid fuel формула
  `VOLL [USD/MWh] * fuel_share * 1000 [MWh/GWh]`, якщо fuel unit — GWh;
  припущення — втрачена генерація дорівнює частці нестачі fuel. Для інших
  виробничих inputs використовується найбільша sink penalty як conservative
  proxy при BOM 1:1. Ціни товарів не використовуються; це оцінки збитку, не
  відкалібровані значення.
- Grid safety stock — опціональна кінцева reserve target `ibar` після покриття
  споживання. Це не нова витрата запасу щотижня.

Виробничий горизонт за замовчуванням 4 тижні; current observed targets
переносяться на нього як явне припущення. Налаштування:
`production_horizon`, `safety_stock`, `include_estimated_arrivals` і окремий
`safety_buffer_policy`. `safety_stock` як і раніше керує тільки grid `ibar`
reserve; він не змінює номінальний BOM.
Останнє за замовчуванням False: V3/WIP оцінки не приховують дефіцит автоматично.
Arrivals з unknown датою ніколи не покривають конкретний deadline.
Unknown stock не стає нульовим покриттям: відповідна потреба не генерується,
а planner додає issue. Для виробничих target unknown запас готової продукції
або input блокує наступний крок BOM, щоб пропуск не створив зайве поповнення.

`confidence=None`: калібрування імовірностей не виконано. Маска видимості,
warning score або ступінь optimism не є готовою confidence. Hidden demand
не замінюється вигаданим прогнозом. Пропуски доступні в `planner.last_issues`.

Окремі параметричні пресети для контрольованого порівняння лежать у
`agents/team_agent/experiments/presets.json`; це конфігурації експерименту,
не підігнані результати. `NeedPlanner.export_examples(needs)` видає JSON-ready
рядки з priority rank та assumptions для handoff.

## V3: Робота Перед Вантажем

`QueueForecaster.forecast(snapshot, observation, network)` моделює observed
queued cargo й known inbound pipeline по тижнях до T (`max_weeks` може обмежити
горизонт). Опціональний `proposed_pipeline` дозволяє додати кандидатів, які
справді пройдуть через чергу; caller має надати відомий edge/lane/arrival week.

Алгоритм відтворює default FIFO логіку публічного simulator: arrivals додаються
перед release, старі когорти першими, у когорті pro rata по next-edge capacity,
потім shared pool throughput, fleet slack duplicate routes наприкінці.
`kappa` вже містить open fraction; вдруге на `open` не множиться.
Observed open=0 не отримує штучних 5% throughput.

Вантаж рухається через tandem queues. Release може розділити його на кілька
майбутніх надходжень: календар містить частини, а не дубль повної партії плюс
частини. Невідправлений/неприбулий за горизонт залишок має unknown дату.

`queue_forecast.visits` містить для source/choke/cohort:
`arrival_week`, `evaluated_week`, projected `work_ahead`,
`same_cohort_competition`, `first_release_week`, `completion_release_week`.
Для майбутнього прибуття work_ahead оцінено на момент його прибуття після
попередніх releases, а не просто today's queue / rate. Для cargo, який уже
стоїть у черзі, історичний book невідомий: evaluated_week = current week.
`completion_weeks[source_id]` — завершення доставки всієї партії або None.

Це **умовний сценарій**, не hidden-future oracle:

- Поточні observed caps, throughput і bans зберігаються; нові disruptions,
  зміни weekly averages та майбутні рішення агента не відомі.
- Немає майбутніх dispatches/overrides/holds; враховано лише існуючі cargo
  та явно передані proposed pipeline.
- `closure_end` не перетворюється автоматично на гарантоване reopening.
- Неоднозначні inbound/queue metadata або невідомі необхідні ресурси не
  отримують скінченний ETA. Відоме повне закриття зберігається у сценарії.
- Усі projected arrival quantity/timing мають estimated provenance,
  незалежно від того, наскільки точна формула в цьому сценарії.

`retrospective_backtest(archived_predictions, later_snapshots)` є окремою
evaluation-only функцією. Вона звіряє агрегати (destination, commodity, week),
бо wire-групи не мають стабільних ID між тижнями. Відсутнє фактичне спостереження
залишається `None`; ця функція не імпортується і не викликається decision loop.

## Підключення та перевірка

Тести перевіряють реальні модулі через `DecisionPipeline` з test-only
алокатором, а також змінні pipeline/queues у повних епізодах Tiny/Small/Full.
Контрольна FIFO-сценарна перевірка звіряє releases із публічним simulator.

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv run pytest tests/test_team_analytics.py tests/test_team_contracts.py tests/test_network.py tests/test_team_agent.py -q
uv run sbf check team_agent --task=small
uv run sbf check team_agent --task=full
```

Ці файли вже у спільному репозиторії, не тільки в review ZIP. Але
`build_pipeline()` поки повертає None: production agent залишається старою
евристикою до підключення реального allocator Віті. Новий score не заявляється.
Після wiring потрібні повні Linux CPU-check і парний `sbf compare` з baseline.

## Результат перевірки 2026-10-06

- Analytics, contracts, network, agent та evaluation regressions:
  **98 passed, 4 skipped** (`pytest -n 3 --dist=loadfile`). Пропущено
  Unix Bash orchestration та server extractor, який потребує `os.fchmod`.
- Повні епізоди Tiny/Small/Full: state + needs + V3 перевірені на кожному кроці
  через `DecisionPipeline`, з test-only нульовим allocator. Середовище при
  цьому крокує старою евристикою, щоб pipeline/queues реально змінювалися.
- Local process CPU максимум лише analytics/pipeline/gate: Tiny 0.015625 с,
  Small 0.203125 с, Full 0.859375 с. Це Windows Python 3.12 з locked packages,
  не серверний CPU meter і не бюджет майбутнього allocator.
- Ruff check та format check проходять.
- ZIP scorer validation та статичні імпорти проходять, 8 submission-файлів.
- Офіційний `sbf check` не виконано: WSL Service повертає E_ACCESSDENIED
  у цьому Codex-сеансі. Команди для повторення наведені вище.
- Ні commit, ні push, ні upload не виконувалися; score поки не змінюється
  через ці модулі, бо default Agent їх ще не викликає.
