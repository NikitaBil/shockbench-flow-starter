# Контракти та підключення модулів V1

Статус: реалізований формат V1 **для погодження командою**. Поки Маркіян і Вітя
не підтвердили поля та сигнатури, не називаємо його погодженим. Власник спільного
контракту та інтеграції — Нікіта. Типи лежать у `agents/team_agent/contracts.py`.

У `contracts.py` немає обчислень: `StateSnapshot` та `DeliveryNeed` залишаються
структурними інтерфейсами `Protocol`. Реальні dataclasses і модулі тепер є в
`state.py`, `needs.py`, `queue_forecast.py`; опис реалізації та припущень:
[ANALYTICS_V1.md](ANALYTICS_V1.md). V4 allocator тепер підключається явно через
`allocation_enabled=true`; за замовчуванням лишається heuristic. Мережа містить
V2/V3. Актуальний опис злиття, resource accounting і перевірок:
[ALLOCATOR_V1.md](ALLOCATOR_V1.md). Контракт лишився початковим V1; діагностика
detour fleet передається через наявні reasons, без нового kind resource_usage.

## Індекси та одиниці

| Поле | Тип | Значення |
| --- | --- | --- |
| `destination_node`, `chokepoint_node` | `int` | Індекс `config['static']['nodes']`, не рядок layout |
| `commodity_id` | `int` | Індекс `static.commodities` |
| `edge_id`, `next_edge_id` | `int` або `None` | Індекс `static.edges`; `None` — невідомий |
| `lane_id` | `int` або `None` | Індекс `static.lanes`; інтерпретується разом з `lane_status` |
| `slot_id` | `int` | Позиція у `static.action_slots` і масиві `flows` |
| `(node, commodity)` | `tuple[int, int]` | Спільна пара індексів static; layout вказує, які пари існують |
| `week`, `due_week` | `int` | Абсолютний тиждень `1..T`, не відстань від поточного тижня |
| кількість | `float` | `static.units[static.commodities.id[commodity_id]]` |
| ціна дефіциту | `float` або `None` | USD за одиницю цього товару для одного тижня дефіциту |

`due_week < state.week` означає прострочену потребу: не переносимо її автоматично
на поточний тиждень. Запит після `T` не входить у контракт V1. Не складаємо
кількості різних товарів без перевірки одиниць/погодженого перетворення.

## Спостереження, оцінки та невідомі дані

`Quantity(value: float | None, source: str, confidence: float | None = None)`:

- `observed`: спостережена скінченна невід'ємна кількість, зокрема нуль;
- `estimated`: оцінена скінченна невід'ємна кількість;
- `unknown`: `value=None`, `confidence=None`, а не підставлений нуль.

`confidence` — число `[0, 1]` з поясненим змістом або `None`. Некалібрований
`warning.score` не можна записувати сюди як імовірність. Приховані/padding рядки
не стають нульовими спостереженими вантажами.

## StateSnapshot: інтерфейс Маркіяна

| Атрибут | Тип | Семантика |
| --- | --- | --- |
| `week`, `horizon` | `int` | Поточний тиждень і `config['T']` |
| `available_stock` | `Mapping[(node, commodity), Quantity]` | Доступний для відправлення ресурс за узгодженим порядком операцій тижня |
| `backlog` | така сама mapping | Невиконаний попит, не запас; усі пари `layout.demands` |
| `pipeline` | `Sequence[PipelineLot]` | Живі вантажі на поточних ребрах |
| `queues` | `Sequence[QueueLot]` | Вантажі, що вже очікують на chokepoint |
| `arrivals` | `Sequence[ExpectedArrival]` | Календар надходжень у кінцевий запас; невідомі дати дозволені |

`available_stock` включає всі пари `layout.stock_slots`, навіть невідомі.
Власник стану має явно описати, які надходження та supply цього тижня доступні
до dispatch. Просте копіювання `stock.qty` на момент `t-1` не гарантує цього.
Pipeline/queue не додаються до доступного запасу лише тому, що їх видно.

Поля records у `contracts.py`:

- `PipelineLot`: `lot_id`, `edge_id`, `commodity_id`, `lane_id`, `lane_status`,
  `quantity`, `edge_arrival_week`, `destination_node`.
- `QueueLot`: `lot_id`, `chokepoint_node`, `commodity_id`, `lane_id`, `lane_status`,
  `next_edge_id`, `entered_week`, `quantity`.
- `ExpectedArrival`: `arrival_id`, `source_id`, `source_kind`, `destination_node`,
  `commodity_id`, `quantity`, `arrival_week`, `source`.

Ідентифікатори — непорожні `str`; `source_kind` має значення `pipeline`, `queue`,
`wip` або `supply`. `source_id` пов'язує календар із вихідним вантажем. Один
вантаж не рахується двічі як запас та майбутнє надходження. `arrival_week` —
абсолютний тиждень або `None`; `source` позначає походження саме дати, окремо від
`quantity.source`. При невідомій даті використовуємо `source='unknown'`.
`entered_week` може бути `0` для початкового вантажу, якщо так задає симулятор.

### Звірка pipeline з Вітею

Маркіяну: звір обробку pipeline з `network.transit_progress(edge_id, lane_id)`.

- `arrival_node` — head поточного ребра; `destination_node` — кінець усієї лінії.
- `pipeline.arrival_week` стосується `arrival_node`, не автоматично destination.
- `remaining_nominal_transit_weeks` не включає поточне ребро, очікування в
  чергах і майбутні обмеження. Це не гарантована дата доставки.
- `lane_status='known'` потребує відомого `lane_id`; `off_lane` означає
  підтверджений рух поза лінією; `unknown` означає приховану/неоднозначну лінію.
- Не передавай `None` у `transit_progress` лише через `pipeline.lane.observed=0`:
  відсутня маска може позначати off-lane, padding або приховані дані. Спочатку
  перевір живий рядок і контекст; якщо off-lane не встановлено, збережи unknown.
- Без прогнозу роботи черги попереду вантажу точна дата завершення невідома:
  `ExpectedArrival.arrival_week=None`. Номінальний ETA можна передавати лише
  як явно позначену оцінку з описаними припущеннями.

Парсер pipeline реалізовано в `StateBuilder`, а умовний прогноз черг V3 —
в `QueueForecaster`. Прогноз опціональний, не читає приховане майбутнє та
не перетворює сценарний ETA на спостережену дату. `build_pipeline()` досі
залишається заглушкою до підключення справжнього алокатора.

## DeliveryNeed: інтерфейс Маркіяна

| Поле | Тип | Правило |
| --- | --- | --- |
| `need_id` | `str` | Унікальний у поточному циклі; стабільний для того самого зобов'язання |
| `destination_node`, `commodity_id` | `int` | Спільні індекси static |
| `quantity` | `float` | Скінченний невід'ємний обсяг заявки, не частка capacity |
| `due_week` | `int` | Коли товар потрібен у кінцевому пункті |
| `priority` | `float` | Більше число — вищий пріоритет; скінченне значення |
| `reason` | `str` | Наприклад `backlog`, `current_demand`, `production`, `safety_stock` |
| `shortage_cost_per_unit_usd` | `float | None` | Невід'ємна оцінка marginal shortage cost; `None`, якщо невідома |
| `confidence` | `float | None` | `[0,1]` або `None`; зміст пояснює автор прогнозу |

Нікіта передає allocation заявки у порядку `(-priority, due_week, need_id)`.
Це порядок розгляду, не зобов'язання allocator ігнорувати реальну доступність.
Залишок заявки зменшується після призначення; алгоритм належить Віті.

## AllocationResult: повернення Віті

```python
from contracts import AllocationResult, DecisionReason, ResourceUsage, UnmetNeed

result = AllocationResult(
    flows=flows,  # np.ndarray, float64, shape з config.spaces.action.flows
    unmet_needs=(UnmetNeed(need_id="n1", remaining_quantity=2.0, reason="no_stock"),),
    resource_usage=(),
    reasons=(DecisionReason(code="no_stock", message="Not enough dispatchable stock", need_id="n1"),),
)
```

`flows[s]` — запит на відправлення кількості товару slot `s` цього тижня.
Усі компоненти беруть спільні класи через `from contracts import ...`, не
`agents.team_agent.contracts`: пакет submission завантажує sibling modules.

`UnmetNeed` містить ID існуючої заявки, залишок `0..need.quantity` у її одиницях
і причину. Не дублюємо ID у цьому списку. Відсутність заявки в `unmet_needs`
означає, що allocator вважає її повністю призначеною, не вже доставленою.

`ResourceUsage(kind, resource_index, unit, used, limit, limit_source, pool=None)`:

| `kind` | Що індексує `resource_index` | `pool` |
| --- | --- | --- |
| `stock` | Рядок `layout.stock_slots` | `None` |
| `edge` | Рядок `static.edges` | `None` |
| `chokepoint_pool` | Позицію `layout.chokepoints`, **не node ID** | `tb` або `ct` |

`used` — сумарний запланований обсяг поточного dispatch/release. `limit` —
скінченна невід'ємна межа або `None` з `limit_source='unknown'`. Для запасу
`unit` точно збігається з одиницею товару; для ребра/пулу — узгоджена одиниця
ресурсу. Один shared resource звітується один раз. Цей звіт не резервує
capacity майбутніх сегментів лінії й не доводить повну фізичну допустимість.

`DecisionReason(code, message, need_id=None, slot_id=None)` пояснює рішення.
Діагностика залишається в `agent.last_allocation`; не потрапляє у action.
За потреби Вітя повертає `override_qty` та `release_mode` у відповідних
config-shapes. Якщо вони відсутні, залишається default release, не hold.

## Порядок викликів

Усі компоненти створюються один раз у `build_pipeline(config, network)`:

```text
state_builder.build(observation, network) -> StateSnapshot
need_planner.plan(state, observation, network) -> Sequence[DeliveryNeed]
allocator.allocate(state, ordered_needs, observation, network) -> AllocationResult
ActionValidator.validate(action, observation) -> action
```

У `Agent.act()` цей порядок уже реалізовано. Всі модулі отримують той самий
state/network та observation з масками видимості; поточні обмеження читає
allocator, ризики може читати planner. Після отримання реальних класів підключаємо
їх у `integration.build_pipeline`. Нині функція повертає `None` і працює стара
евристика. Тести активного циклу використовують підставні модулі лише в tests.

Фінальна перевірка контролює shapes/dtypes із config, finite/nonnegative,
release modes і спостережені заборони. Межа інтеграції також перевіряє індекси,
унікальність заявок і узгодженість діагностики. Вона не обрізає flows мовчки,
не вважає unknown нулем і не підміняє фізичний allocation простим clipping
до `stock.qty`. Винятки модулів не приховуються поверненням нульової дії.

## Перевірка після підключення

У WSL з кореня репозиторію та налаштованим `UV_PROJECT_ENVIRONMENT`:

```bash
uv run pytest tests/test_network.py tests/test_team_agent.py tests/test_team_contracts.py tests/test_team_check_script.py tests/test_team_evaluate.py -q -o cache_dir=/tmp/shockbench-contracts-pytest
uv run sbf check team_agent --task=small
uv run sbf check team_agent --task=full
uv run sbf compare team_agent baseline --task=small --entropy=67890 --episodes=16 --cpu_budget=True
uv run sbf compare team_agent baseline --task=full --entropy=67890 --episodes=16 --cpu_budget=True
```

`test_team_agent` перевіряє рівність поточної евристики baseline. Коли нова
стратегія справді змінює flows, ці тести треба явно перевести на перевірку
heuristic-only режиму, а не називати нову стратегію регресією через інші дії.

Для збереження точних ZIP-версій, CPU-звітів і JSON порівнянь:

```bash
bash scripts/check_team_integration.sh
# Для швидшої первинної повної перевірки на власному root, не --quick:
EPISODES=2 bash scripts/check_team_integration.sh
```

Скрипт не виконує upload. Small і Full проходять на однаковому root/count
для кандидата та baseline. Full може довго будувати еталони. Порівняння з
CPU-бюджетом враховує fallback; інтервал, що містить нуль, не підтверджує перевагу.
Кілька епізодів — smoke-перевірка процесу, не надійний доказ покращення.

Попередні успішні CPU-checks належали старому ZIP. Після зміни файлів hash
змінився, тому офіційні checks треба повторити. Codex не має доступу до WSL;
нові офіційні порівняння/CPU-checks не видаються за вже виконані.
