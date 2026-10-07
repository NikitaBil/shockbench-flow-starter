# Метрики, звітність і шлях до RSS 0.75

## Поточний стан

Ціль команди: RSS не менше **0.75**. Це ціль, не прогноз і не гарантія.
Наявні результати не підтверджують її досягнення.

| Варіант | Task / root / епізоди | RSS | Різниця проти baseline |
|---|---|---:|---:|
| Baseline у поточному порівнянні | Small / 67890 / 16 | 0.4900 | 0 |
| Pipeline без поповнення палива | Small / 67890 / 16 | -1.9408 | -2.4308 |
| Pipeline з поповненням палива | Small / 67890 / 16 | 0.0688 | -0.4212 |
| Baseline у поточному порівнянні | Full / 67890 / 16 | 0.3449 | 0 |
| Pipeline з поповненням палива | Full / 67890 / 16 | -0.1810 | -0.5259 |

Для останнього варіанта paired 90% interval різниці: **[-0.5539, -0.3136]**.
Він дорожчий за baseline у всіх 16 епізодах, середня вартість вища на **17.47%**.
Fallback у порівнянні: **0**. Проблема якості рішень існує навіть без fallback.
Full paired 90% interval: **[-0.6368, -0.4369]**; fallback = 0,
candidate програв у всіх 16 епізодах, середня вартість вища приблизно на 26.6%.
Раніший Full 0.1796 стосується старого parity-варіанта на 2 епізодах,
а не поточного allocator.

Root 67890 вже використаний для діагностики: це tuning/debug-набір, не holdout.
20 dev епізодів зі score 0.4004 не можна напряму порівнювати з цим набором.

## Що вже працює

1. `StaticNetwork` описує наявні action slots та маршрути; нових маршрутів не вигадуємо.
2. `StateBuilder` створює snapshot запасів, pipeline, черг та очікуваних надходжень.
   Значення мають provenance `observed`, `estimated`, `unknown`.
3. `NeedPlanner` створює потреби ринків, виробництва та енергомереж.
   Fuel replenishment додає потреби терміналів, без яких upstream-постачання не запускалося.
4. `Allocator` підбирає допустимі доставки, використовує deterministic greedy,
   спільно обліковує поточні запаси, capacity та fleet. ETA умовний, не гарантія.
5. `DecisionPipeline` перевіряє контракти; `ActionValidator` перевіряє фінальну дію.
6. Frozen candidate і baseline мають SHA256; paired оцінювання використовує ті самі епізоди.

Порядок: **стан -> потреби -> розподіл -> фінальна перевірка -> дія**.
Наявність цих модулів і проходження unit tests не означають, що політика сильніша.
Стандартний `team_agent` і frozen candidate можуть мати різні params: перевіряємо manifest.

## Новий збір метрик

`examples/12_team_diagnose.py` тепер записує trace schema v2, окремий JSONL на
кожну політику та епізод. Діагностика не редагує submission.

До `env.step` записуються стан, потреби, рішення, requested flows і CPU.
Після `env.step` записуються **публічні поля нового observation**, що описують
щойно завершений тиждень:

- `last_week.clip.requested` і `.executed`: виконання після clipping, не факт доставки отримувачу.
- `last_week.cost_components`: freight, war risk, tariff, holding, queue holding,
  shortage, disposal, shed, у порядку `config.layout.cost_components`.
- `last_week.sinks.demand`, `.served`, `.lost`: попит і обслуговування видимих ринків.
- `last_week.shed.qty`: фактичний видимий shed за grid, GWh.
- Cost з `reward_cents`, terminal salvage окремо: компоненти мінус salvage мають
  відтворити сумарну вартість. Останній тиждень також включений.

Маски видимості обов'язкові: hidden значення стають `None`, не нулем.
CPU розбитий на state / needs / allocation / other. Конструктор доданий до
першого act. Це local process CPU з taps, не вимір офіційного ізольованого runner.

`examples/13_team_metrics.py` читає існуючі результати без запуску середовища:
створює `metrics.json` і `report.md`. Можна передати один шлях або Fire list literal
до кількох comparison JSON і diagnostic folders. Вихідна папка повинна бути новою.

## Словник метрик

| Метрика | Значення / правило читання |
|---|---|
| RSS / score interval | Береться з official-style comparison, не перераховується нашим скриптом |
| Paired difference / interval | Candidate мінус baseline на тих самих епізодах; негативний інтервал = регресія |
| Score gap | `max(0, 0.75 - RSS)`; не переводиться у долари лінійною формулою |
| Episode win fraction | Частка епізодів з меншою вартістю; не `p_a_better` bootstrap |
| Harm-level raw extra cost | Середня різниця витрат за stratum, не leaderboard-weighted RSS |
| Planned unmet fraction | Сума залишків потреб / сума planner requests, окремо за commodity |
| Execution fraction | Виконані flows / requested flows тільки на парно видимих slot-weeks |
| Demand service fraction | Served / demand тільки на парно видимих sink rows |
| Late assignment estimate fraction | Кількість `eta_late` / `allocated_current_resources`; не realized on-time rate |
| Overdue need quantity | Planner requests зі строком до поточного тижня |
| Stock / backlog | Mean/max/last known, кількість unknown/estimated samples, за node/commodity |
| Pipeline / queue / calendar | Окремі native volumes і provenance; між блоками не сумуються |
| Resource utilization | Used / positive known limit; лише reported resources, не вся мережа |
| Saturation | Utilization >= 0.99; нульові й невідомі limits рахуються окремо |
| Cost reconciliation | Сума компонентів - salvage - total cost; невідомо за неповного coverage |
| CPU p95 / max | Локальний час act; stage totals і constructor окремо |

Потреби створюються знову кожного тижня: сума planner requests може повторно
враховувати той самий прогноз. Це **не** обсяг унікального зовнішнього попиту.
Причини unmet можуть перетинатися: не складаємо їхні обсяги в загальний дефіцит.
Різні товари не сумуються навіть коли назви одиниць збігаються.
У старих трасах execution/service/cost breakdown відсутні: у звіті `unknown`.

## Що вже видно з трас

Для candidate на епізодах 0/1:

- Умовно пізні призначення: **52.24% / 55.13%** allocation events.
- ETA forecast budget exhausted: **396 / 306** unmet reason records.
- Немає допустимого delivery slot: **47 / 165** records.
- Planned unmet для готових чипів приблизно **96-97%** повторюваних planner quantities.
- Backlog на видимих ринках нульовий; це **не** доказ відсутності lost demand або shortage cost.
- Частина stock і edge resources насичується майже щотижня.

Це сигнали для перевірки, не причинна атрибуція доларових втрат.
Наступний крок: зіставити shortage/shed і фактичне served/executed у trace v2.

## Порядок покращень

### 1. Повернути конкурентоспроможну основу

Власник: Нікіта. Заморожений baseline зберігаємо незмінним. Candidate з негативним
paired interval не просуваємо замість нього. Це не прихований fallback в allocator:
ми явно обираємо перевірену політику для submission, а іншу лишаємо експериментом.

Готовність: manifest, hashes, Small/Full CPU checks, однакові episode sets.

### 2. Виробництво, одиниці та строки

Власник: Маркіян. У `needs.py` є дві конкретні семантичні проблеми:

- `_shortage_cost` множить `grid.voll` на 1000, хоча simulator schema визначає
  VOLL у USD/GWh. У поточному candidate `shortage_cost_model=False`, тому цей
  дефект не пояснює його поточну регресію, але модель не слід вмикати до виправлення.
- `w_scr` у simulator schema означає вікно scrap у тижнях, а не частку BOM loss.
  Формула `target * (1 + w_scr/tau)` вигадує додатковий номінальний попит.
  Відділити правильний nominal BOM від явно обґрунтованого safety buffer.

Перевірити дедлайни upstream-inputs щодо виробничого lead time і доставки.
Не вимагати всю майбутню продукцію негайно та не дублювати pipeline/WIP у netting.

Готовність: hand-calculated fixtures, однакові одиниці з simulator, ablation
правильного BOM проти frozen candidate без інших змін.

### 3. ETA і відмови allocator

Власник: Вітя. Розібрати відмови `no_permitted_delivery_slot` за потребами та маршрутами.
Не шукати Dijkstra-шлях, який не відповідає action slots.

Перевірити порядок дорогих ETA forecasts: кандидати важливих потреб першими,
відсіювання без запасу/ресурсів до прогнозу, кеш тільки для тотожного стану та
обсягу. Не збільшувати ліміт forecasts без перевірки CPU Small/Full.
Для невідомого ETA не заявляти своєчасність; якщо вводимо ризикову доставку,
вона повинна бути явною опцією з окремою оцінкою, а не прихованим обходом перевірок.

Готовність: причина кожної відмови, ресурси без over-allocation,
менше корисних доставок втрачається через технічний forecast budget,
позитивний paired результат із CPU metering.

### 4. Підбирати горизонт та safety stock

Власники: Нікіта + Маркіян. Після семантичних виправлень окремо перевірити
production horizon 2/4/8, safety stock on/off, queue ETA on/off.
Кожен варіант має власний frozen folder, manifest і SHA256.
Не змінювати одразу всі параметри: спочатку one-change ablation, потім
перевірка сумісності двох підтверджених покращень.

Готовність: менший shortage/shed без неконтрольованого росту holding/queue cost,
позитивний paired interval на tuning-наборі.

### 5. Далі використати early warnings

Власники: Маркіян (ризик) + Вітя (розподіл). Лише після відновлення базового
ланцюга поставок: announced prohibitions, warning scores, notices для раннього
поповнення та зміни маршрутів. False alarms оцінювати через зайві holding/freight.
Сам A* або Dijkstra не замінює планування потреб, ресурсів і часових строків.

## Коли говоримо про 0.75

Генератор має консервативний локальний gate:

1. Не quick; completed score comparison; CPU metering ввімкнений; fallback = 0.
2. Candidate RSS >= 0.75 і нижня межа його score interval >= 0.75.
3. Нижня межа paired interval candidate - baseline > 0.
4. Це тільки `candidate_for_holdout_validation`, не автоматичний upload.

Для заяви про якість потрібна додаткова перевірка замороженого переможця:
Small **і** Full на свіжому root, який не використовувався для tuning, спочатку
32-64 епізоди за доступним часом. Якщо інтервал широкий, збільшуємо вибірку.
Dev root 0 використовуємо для фінального confirmation, не нескінченного підбору.
Одноразовий score 0.75 на відомих епізодах не гарантує 0.75 на leaderboard.

## Команди у VS Code / WSL

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/shockbench-flow-starter"
uv run pytest tests/test_team_metrics.py tests/test_team_diagnose.py -q

FIX="outputs/11_team_candidate/2026-10-06_14-49-25_762846"
STAMP=$(date +%Y%m%d-%H%M%S)
DIAG="$FIX/diagnostics-v2-small-$STAMP"
METRICS="outputs/13_team_metrics/small-$STAMP"

uv run python examples/12_team_diagnose.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=small --entropy=67890 --episodes=4 --out="$DIAG"
uv run python examples/13_team_metrics.py --comparison="$FIX/small-compare/result.json" --diagnostics="$DIAG" --target=0.75 --out="$METRICS"
cat "$METRICS/report.md"
```

Це діагностика і звіт, **не** новий RSS. Для нового Full RSS:

```bash
FULL="$FIX/full-compare-$STAMP"
uv run sbf check "$FIX/candidate" --task=small
uv run sbf check "$FIX/candidate" --task=full
uv run python examples/09_team_evaluate.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=full --entropy=67890 --episodes=16 --cpu_budget=True --out="$FULL"

DIAG_FULL="$FIX/diagnostics-v2-full-$STAMP"
uv run python examples/12_team_diagnose.py --agent="$FIX/candidate" --against="$FIX/baseline" --task=full --entropy=67890 --episodes=2 --out="$DIAG_FULL"
uv run python examples/13_team_metrics.py --comparison="$FULL/result.json" --diagnostics="$DIAG_FULL" --target=0.75 --out="outputs/13_team_metrics/full-$STAMP"
```

Нові експерименти повинні використовувати інший tuning root; confirmation root
не дивимось між ітераціями. Порівняння на 2 епізодах достатнє для пошуку грубих
збоїв, не для підтвердження цілі.

## Практики з документа «Як перемагати в хакатоні»

Джерело: наданий командою 8-сторінковий PDF. Це особистий гайд і добірка
LinkedIn-порад, не правила ShockBench і не дослідження ефективності алгоритмів.
Промпт для coding agent у PDF розглянуто як приклад, а не як окремі інструкції
цьому репозиторію. Наявні правила benchmark мають пріоритет.

### Що переносимо у нашу роботу

| Ідея гайду | Адаптація до ShockBench | Перевірка результату |
|---|---|---|
| Чітка проблема та рішення | Проблема: зриви поставок спричиняють shortage і shed. Рішення: керувати потоком з урахуванням потреб, запасів і доступних маршрутів | Менші витрати та вищий RSS на парних сценаріях |
| Робочий end-to-end шлях спочатку | Frozen agent -> check -> compare -> metrics -> вибір версії; самі модулі недостатні | Один відтворюваний прогін, hashes, звіт, явний висновок |
| Одна сильна фіча | Спочатку виправлений базовий ланцюг; потім окремий експеримент раннього поповнення перед announced disruption | Позитивний paired interval; окремо контролюємо false-alarm витрати |
| Маленькі PR і ранні merge | Один логічний дефект/експеримент на PR, перевірка контрактів до інтеграції | Unit/integration tests + Small/Full check; не merge лише тому, що код компілюється |
| Невеликі виправлення замість переписування | Reproduce -> гіпотеза -> patch -> regression test -> paired compare | Видно, яка конкретна зміна дала різницю |
| Демо і резервний шлях | Використати наявний dashboard/звіти; збережений replay позначити як recorded | Показуємо реальний task/root/hash/score, не вигадані успіхи |
| Заморожування останніх 20% часу | Для тижня це приблизно останні 34 години: жодних нових policy features | Фінальна версія проходить checks, має оцінку і запасну перевірену submission |

### Що не переносимо буквально

- «Пітч важливіший за проєкт»: у нашому GUIDE leaderboard RSS обчислюється
  з витрат агента. Окремі критерії презентації уточнюємо в організаторів;
  красивий UI не підвищує RSS.
- «Не писати код вручну»: AI допомагає, але команда відповідає за одиниці,
  контракти, правильність і оцінювання. Generated code не є доказом якості.
- «Додати вражаючі фреймворки»: у scored agent суворий import allowlist і CPU.
  Нові сервіси, orchestration, 3D та voice не додаємо без потреби задачі.
- «Живе демо за будь-яку ціну»: тривалий Full evaluation не запускаємо вперше
  на сцені. Збережені результати, графіки або replay допустимі як чесно
  позначений резерв; вимоги конкретної презентації уточнюємо окремо.
- «Ігнорувати рідкі edge cases»: випадки Full, порожніх черг, hidden даних,
  CPU і malformed action можуть коштувати RSS. Їх не відсуваємо заради polish.
- Заяви авторів про win rate не гарантують ні перемоги, ні RSS 0.75.

### Наступний короткий цикл для команди

1. **Нікіта:** перевірити нові metrics/diagnose tests у WSL і зняти trace v2
   для поточного candidate проти baseline. Встановити, які компоненти витрат
   ростуть; не плутати diagnosis з новим score.
2. **Маркіян:** окремий PR з правильним nominal BOM без `w_scr/tau`.
   Додати ручний fixture, не змінювати одночасно safety stock і горизонт.
3. **Нікіта:** заморозити цей варіант у новій папці. Порівняти зі старим
   candidate для ізоляції ефекту і з baseline для рішення про якість.
4. **Вітя:** після метрик окремий PR про корисні доставки, що відхиляються
   через ETA forecast budget; не переписувати весь allocator і не заявляти
   невідомий ETA як своєчасний.
5. **Нікіта:** перевірити Small/Full CPU та парні результати. Об'єднати лише
   підтверджені зміни, а їхню комбінацію оцінити ще раз: виграші можуть не складатися.
6. **Вся команда:** після відновлення конкурентної бази вибрати одну
   перспективну фічу: раннє поповнення перед відомим майбутнім обмеженням.
   Не витрачати паралельно час на новий UI, RL і новий оптимізатор.

Межа завершення циклу: є виміряний результат і рішення, а не просто нові файли.
Unit tests підтверджують коректність; compare перевіряє користь. Не вимагаємо,
щоб кожен проміжний патч одразу досягав 0.75, але регресію не просуваємо.

### Картка одного експерименту

```text
ID / власник / branch / commit:
Проблема і джерело доказу (trace/week/метрика):
Гіпотеза:
Одна зміна; що навмисно залишаємо незмінним:
Candidate folder / params / SHA256:
Reference candidate і baseline folders / SHA256:
Task / tuning root / episode IDs / quick=False / CPU setting:
Regression test і ризики для Small/Full:
RSS / paired difference / interval:
Cost breakdown / service / clipping / CPU / fallback:
Рішення: залишити / відхилити / недостатньо доказів:
Наступна перевірка; holdout тільки після вибору варіанта:
```

Картки зберігати разом із run artifacts під `outputs/`, а підтверджені
висновки переносити у versioned docs. Не редагувати frozen folders після запуску.

### Коротке демо без перебільшень

Історія на 10 секунд: «Показуємо, як збій у мережі впливає на поставки,
які рішення прийняв агент і скільки вони коштували порівняно з baseline».

Послідовність: scenario/task -> disruption -> рішення/маршрут -> shortage/shed
та загальна вартість -> paired результат. Якщо candidate програв, це теж
показуємо як експеримент, а не як підтверджене покращення. Один яскравий
епізод ілюструє поведінку; якість підтверджує набір, не вибрана вдала анімація.

Основне правило подальшої розробки: **менше одночасних змін, коротший цикл
вимірювання, більше підтверджених покращень**. Гайд змінює організацію роботи,
а не замінює технічне виправлення причин низького RSS.

## Потижнева локалізація регресії

`examples/14_team_timeline.py` читає лише збережені schema v2 JSONL і summary.
Не запускає політики, сценарії, RSS, upload чи CPU runner. Вихід:
`timeline.json` із усіма тижнями та `report.md` для команди.

Перевіряються completed status, незмінність submission за summary, відповідність
episode IDs, послідовність тижнів 1..T, totals/length, парний demand і відсутність
дублікатів. SHA256 усіх вхідних файлів записуються і перевіряються після читання.
Це hashes діагностичних файлів; hashes submission беруться окремо з summary.

Звіт містить перший тиждень погіршення net cost/shortage/shed, найбільші
тижневі втрати, cumulative cost gap, shed по grid і serviced volume по sink.
Для початку регресії показуються тиждень до, сам тиждень і тиждень після:
requested/executed за commodity, локальні service/shed зміни.

`--threshold_usd=1000000` за замовчуванням означає різницю понад 1 млн USD
за один тиждень, не тривалий тренд або статистичну значущість. Поріг можна змінити.
Для першої зміни shed/service використовується 1e-6 native units.
Хронологічний збіг ETA-відмов і росту дефіциту не доводить причинний зв'язок.

Hidden positive execution лишається unknown. Якщо action request дорівнює нулю,
новий dispatch на цьому slot також нульовий: це явний логічний висновок,
позначений `zero_request_inferences`, а не підстановка нуля у прихований стан.
Автоматичні releases, arrivals та кінцева доставка цим висновком не описуються.
Потоки окремі за commodity/unit; повторний рух одного вантажу на різних slot
не означає обсяг унікальних доставок споживачеві.

Stock/pipeline baseline у цих traces не збережені, бо baseline не має нашого
instrumented pipeline. Не вигадуємо порівняння цих блоків; використовуємо
наявні outcomes, які записані для обох політик.

```bash
cd /mnt/c/Users/nikit/projects/shockbench-flow-starter
uv run pytest tests/test_team_timeline.py tests/test_team_metrics.py tests/test_team_diagnose.py -q

FIX="outputs/11_team_candidate/2026-10-06_14-49-25_762846"
STAMP=$(date +%Y%m%d-%H%M%S)
uv run python examples/14_team_timeline.py --diagnostics="$FIX/diagnostics-v2-small-20261006-173658" --out="outputs/14_team_timeline/small-$STAMP"
uv run python examples/14_team_timeline.py --diagnostics="$FIX/diagnostics-v2-full-20261006-181113" --out="outputs/14_team_timeline/full-$STAMP"
```

На збережених traces при порозі 1 млн USD перший додатковий shortage на Full
в обох епізодах з'являється на тижні 7, додатковий shed на тижнях 3/4.
На Small додатковий shortage починається на тижнях 7/7/7/6.
Це вікно перевірки ранніх потреб і поставок, не встановлена першопричина.

### Матриця перевірки майбутніх виправлень

| Варіант | Що змінюємо | З чим порівнюємо | Статус |
|---|---|---|---|
| Baseline | Нічого | Контроль | Small 0.4900; Full 0.3449 на root 67890/16 |
| Поточний candidate | Нічого | Baseline | Small 0.0688; Full -0.1810; не просувати |
| Лише виправлення Маркіяна | Один BOM/needs patch поверх поточного candidate | Поточний candidate і baseline | Очікуємо commit; не оцінено |
| Лише виправлення Віті | Один delivery/ETA patch поверх поточного candidate | Поточний candidate і baseline | Очікуємо commit; не оцінено |
| Обидва виправлення | Об'єднання перевірених patches | Обидва одиночні варіанти та baseline | Не створено; ефекти можуть не складатися |

Кожному рядку потрібні власний frozen folder, params, commit provenance та SHA256.
Для tuning порівняння використовувати однакові task/root/episode IDs і CPU
налаштування. Якщо додаємо новий tuning root, на ньому оцінюємо обидві сторони,
а не порівнюємо новий результат з цифрою baseline зі старого root.
Holdout залишається закритим до вибору версії.
