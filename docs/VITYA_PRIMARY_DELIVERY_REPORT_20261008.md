# Вітя: audit доставки та bounded production dispatch recovery

Дата: 2026-10-08. Гілка: `network-delivery-primary-v1`.
Implementation commit: `d79b2fe52b066184eebda94516cf6101fa51c0cb`.

## Рішення за результатом

**Не замінювати champion цим candidate.** Реалізація коректно працює в
перевірених сценаріях, але screen не довів економічно значущого поліпшення.
Native середній total cost зменшився на **0,04216% у Small** і **0,00204% у Full**.
Обидва paired 90% intervals містять нуль. У Full середня shortage cost навіть
зросла; головний виграш там — зменшення disposal.

Офіційний RSS candidate **не отримано**. Ці результати не підтверджують +0,005
RSS, наближення до 0,9 або можливість promotion. PR має залишатися draft:
candidate ізольований, production primary і frozen actor не змінені.

## 1. Синхронізація й точна база

- Отримано актуальні remote refs через `git fetch origin`.
- Створено чистий worktree від `origin/main`, без зміни старої робочої гілки.
- Base: `d929d12d7cf113e954d15ea3925bfa4a87d63acf`.
- Policy promotion у його історії: `0af4b5a711265990c411461d124c49514a7ff0ab`.
- Actor champion: `agents/team_agent`.
- Immutable actor: `agents/team_agent_0781480`.
- Experimental actor: `agents/team_agent_vitya_v1`.
- Автор комітів: `Victor Danylenko <vv.danylenko@ukma.edu.ua>`.

Перевірені submission SHA-256:

| Submission | SHA-256 |
| --- | --- |
| Champion | `efa7c00d1d7ac112f269950d020860ed839ed141bd2d60dcc2d7f24361016dbe` |
| Immutable Full reference | `cc7c0cf6712cd16f7b0c538b3930fa6b96f8055d9aebb287dd2105b3576e1d51` |
| Final candidate | `1d00584591cecd1795fff683e68a505464e00a2d7ac0873d4f5b1258c8a5dea4` |
| Initial neutral candidate, retained under outputs | `89483882ffd6220679e5967633334a359afa7d096a46551be5049f6169db72e5` |

Перший ZIP зі старого Windows checkout мав інший SHA. Причина встановлена:
`contracts.py`, `network.py`, `observations.py`, `risk.py`, `state.py` мали CRLF
замість LF. Після нормалізації байтів їхній вміст тотожний. Чистий worktree має
точний expected champion SHA. Додано `-text` для candidate у `.gitattributes`,
щоб наступний Linux checkout не змінив submission bytes.

Старі `network-delivery`/`analytics` не merged поверх winning actor. Це
виключає непомітний перехід до replacement allocator, який програвав champion.

## 2. Яка архітектура реально активна

Primary працює через `_heuristic_action`. `allocation_enabled` відсутній у
winning params; `DecisionPipeline`, planner needs, FIFO ETA allocation і
forecast budget не є активним шляхом цієї політики.

Послідовність candidate:

```text
entry nominal capacity × action_mask
→ observed closure correction
→ DispatchPriority
→ ProductionHorizon
→ requests snapshot
→ RoutePreferences
→ preferred snapshot
→ StockRebalancer
→ SalesDispatch та наявні upstream/raw/fuel stages
→ FuelMPC (спеціальний режим вимкнений)
→ ProductionResidual (єдина нова policy idea, opt-in)
→ FuelBatch
→ FuelLookahead (спеціальний режим вимкнений)
→ FuelRelease
→ ActionValidator
→ requested action
```

Відносний порядок наявних stages збережений. Нова корекція стосується лише
входів Fab/OSAT. Вона не додає LNG, crude або nucfuel, не редагує overrides,
не обходить fuel pulses і не вмикає існуючі experimental planners.

Candidate — self-contained copy. У порівнянні з champion змінені тільки
`agent.py`, `params.json`, додано `production_residual.py`. Решта copied helpers
зберігають вихідні байти. Великий PR diff папки є наслідком ізоляції submission,
а не переписування всіх цих модулів.

## 3. Фізична послідовність simulator

За locked benchmark source:

1. Pipeline arrivals до chokepoints приєднуються до queue lots.
2. Виконується queue release: overrides і default FIFO.
3. Нові flows проходять permission, entry-edge capacity і shared source stock.
4. Спільний fleet slack масштабує відповідні **dispatch + queue release**.
5. Виконані flows залишають stock і входять до pipeline.
6. Звичайні arrivals до інших nodes надходять у stock.
7. Відбуваються production, generation, consumption, packaging та serving.

Тому звичайне надходження на джерело цього тижня не є запасом для його
dispatch цього тижня. Натомість надходження до chokepoint цього тижня може
вийти з queue у release phase. Candidate ці правила не змінює.

`ActionValidator` перевіряє interface та masks. Він не замінює фізичну перевірку
benchmark і не доводить майбутню feasibility всього маршруту.

## 4. Audit: що встановлено

`examples/15_primary_dispatch_audit.py` записує для кожного slot:

- source, destination, commodity, його native unit;
- observed source stock та observed entry capacity;
- quantity до/після RoutePreferences, після rebalance, sales і batch;
- final request, виконання до fleet та final executed dispatch;
- override requested і executed у власному slot namespace;
- fleet caps, usage, items до/після scaling;
- cost components та invalid-entry diagnostics.

Hooks викликають оригінальні clip/fleet functions один раз і відновлюються
після завершення чи помилки. Actor одержує тільки штатний observation.
Offline execution evidence не використовується всередині submission.

Primary audit: **Full, root 67890, replay seed 0, episodes 0 і 1, по 104 тижні**.
Цей старий root використаний для пояснення baseline, не для candidate tuning.

### Fleet і clipping

На обох цих Full trajectories жоден fleet pool не binding. Fleet clip dispatch
дорівнює нулю для всіх commodities. Тому гіпотеза «спершу координувати dispatch
і queue release за fleet» не підтвердилася саме на цих епізодах.

Це не універсальне твердження: окремий Tiny audit/root 67890 мав 4 тижні
binding tanker fleet. Для Full потрібна своя вибірка і свої execution records.

Приклади інших reductions у Full episode 0:

| Commodity | Requested | Executed | Маскування/entry/stock reduction |
| --- | ---: | ---: | ---: |
| LNG, GWh | 9 985 886,72 | 9 983 631,90 | 2 254,83 |
| crude, GWh | 836 435,78 | 835 497,61 | 938,17 |
| nucfuel, GWh | 4 026 864 | 4 026 864 | 0 |
| wafer, wafer-eq 300 mm | 150 663 542,53 | 150 640 271,89 | 23 270,63 |

Товари з різними units не сумуються. Об'єднаний edge/stock/mask reduction не
названий доведеним ефектом одного конкретного constraint.

Queue overrides episode 0: LNG requested 487 096,95, executed 484 024,62 GWh;
crude requested 333 595,12, executed 331 589,52 GWh. Ці quantities теж не є
гарантованими arrivals до кінцевої енергомережі.

### Конкретна route suppression opportunity

Full episode 0, **week 52**, source `fab_jp_memory_1`, `chip_le_raw`:

- Source stock: **176 718,33 wafer-eq 300 mm**.
- Залишок після baseline requests: **80 298,93** у тих самих одиницях.
- Estimated receiver coverage: `osat_my` **0,612 тижня**, `osat_ph` **1,296**.
- Slots 222/223: до RoutePreferences **172 672,80** на кожний;
  після preference/request **25 006,79** на кожний.
- Їхня shared entry capacity після baseline має **122 659,21** вільної кількості.
- Favored alternatives 224–227 мають **нуль residual entry capacity** після
  врахування всіх commodities. Просте refill цих alternatives нічого не дає.

Два slots використовують той самий залишок 80 298,93: його не можна видати
кожному незалежно. Per-slot spare у trace є opportunity, а не additive capacity.
Nominal transit цих маршрутів — 2 тижні; через chokepoint це не точний ETA.

Це підтверджує фізичну можливість обмежено послабити suppression, але не
доводить економічної користі всього додаткового обсягу. Її перевірено окремим
matched native screen нижче.

## 5. Реалізований candidate

Єдина нова ідея: **additive production dispatch recovery за спільними поточними
ресурсами, receiver cover і м'яким route preference**.

Прапорець: `production_residual_enabled`. Default у коді — `False`; у params
experimental submission — `True`. Champion params залишаються незмінними.

Для можливого додавання `x_s` до baseline `f_s`:

```text
0 ≤ x_s ≤ filtered_requests_s − f_s
Σ_source x_s ≤ observed_stock_source − Σ_source f_s
Σ_entry_edge x_s ≤ observed_capacity_edge − Σ_entry_edge f_s
Σ_receiver_prefix x_s ≤ estimated_cover_budget − existing_receiver_requests
```

Eligibility:

- Existing slots тільки до production inputs Fab/OSAT.
- Source stock, receiver stock і entry capacity мають бути visible.
- Observed zero, action-mask zero та observed route prohibition не отримують
  additions. Hidden padding не використовується як нуль чи capacity.
- Explicit preferred zero не відновлюється.
- Нові requests не перевищують snapshot після closure і ProductionHorizon.
- Якщо output storage отримувача вже full, extra production inputs не додаються.
- На entry slots із positive duplicate fleet weight extra дорівнює нулю:
  candidate не вводить невраховану конкуренцію зі спільним queue-release fleet.

Одиниці:

- Flows — native commodity quantities, не частки capacity.
- Source/receiver budgets рахуються по `(node, commodity)`.
- Entry capacity застосовується в units benchmark для дозволених goods.
- Fleet term — **quantity × додаткові transit weeks**. Різні pools не сумуються.
- Downstream edges і future chokepoint throughput не резервуються сьогодні.

Objective використовує proxy з чинного SalesDispatch:
discounted shortage penalty мінус freight і tariff, помножене на relative
`preferred / filtered_requests`. Baseline quantity є незмінним floor; preference
ранжує додаткові shipments. Це не повна оцінка marginal episode cost.

Receiver budget: observed input stock, conditional rate, відомі final-stage
arrivals, nominal transit + один тиждень cover та всі baseline requests до
отримувача. Credit baseline cargo є консервативним обмеженням extra quantity,
а не твердженням про своєчасну доставку через queues. Hidden rates/costs мають
nominal fallback; hidden tariff тут, як у поточному helper, оцінюється як нуль.
Ці припущення не є observed facts.

Один LP на act, тільки для eligible residual slots. HiGHS: `maxiter=200`,
`time_limit=0.05`. Якщо solve unsuccessful, повертається baseline vector.
Після solve додаткова quantity зменшується за потреби для roundoff safety.
Fuel quantities, release quantities/modes і existing baseline flows не зменшуються.

## 6. Експерименти й усі результати

Для candidate screen користувач погодив **новий development root 202610081**.
Replay seed 0. Small і Full: episodes **0–7**, повні horizons, без quick.
Це native replay із незалежними trajectories на тих самих generated episodes.
Немає official references, harmonic-strata pooling, CPU fallback enforcement
або scorer policy-seed salting. **USD interval не є RSS interval.**

Перший варіант мав hard upper bound після RoutePreferences. Full 8/8 costs
точно збіглися з champion, LP recovery не спрацював. Frozen ZIP і JSON retained.
Його не подано як improvement. Audit показав saturated alternatives;
final candidate змінив саме цей bound, зберігши preference як marginal weight.

### Final matched native screen

Δ = candidate cost − champion cost; негативне значення краще. USD у млрд:

| Task | Champion mean | Candidate mean | Mean Δ | Paired 90% t interval Δ | Wins / ties / losses |
| --- | ---: | ---: | ---: | --- | --- |
| Small, 8 pairs | 2 596,0641 | 2 594,9695 | −1,0945 | [−2,5981; +0,4091] | 3 / 3 / 2 |
| Full, 8 pairs | 6 841,2992 | 6 841,1595 | −0,1397 | [−0,3817; +0,1023] | 3 / 1 / 4 |
| Tiny, 1 pair | 1,2492 | 1,2492 | 0 | Не визначено для однієї пари | 0 / 1 / 0 |

Intervals: paired Student t, 90%, по raw episode cost differences. Зокрема,
Full candidate програв половину епізодів. Improvement статистично не доведено.

### Поепізодні Δ, USD млн

| Episode | Small | Full |
| --- | ---: | ---: |
| 0 | −6 110,7185 | +4,5208 |
| 1 | 0 | −1 025,3894 |
| 2 | 0 | 0 |
| 3 | +3,5500 | +7,5752 |
| 4 | −18,3578 | +0,4277 |
| 5 | −2 739,0460 | −131,4787 |
| 6 | 0 | −1,9198 |
| 7 | +108,4144 | +28,7906 |

### Компоненти: mean candidate − champion, USD млн

| Component | Small | Full |
| --- | ---: | ---: |
| Freight | −0,7897 | +0,1269 |
| War risk | 0 | +0,0050 |
| Tariff | +88,2453 | −0,7619 |
| Holding | −9,7850 | −8,8126 |
| Queue holding | +5,6340 | +23,5724 |
| Shortage | −1 149,4035 | **+89,2155** |
| Disposal | −26,9545 | **−243,8376** |
| Shed | −1,3584 | +1,0672 |

Final total cost також включає terminal salvage credit; через нього сума
component deltas не повинна точно дорівнювати total delta.

Висновок щодо причинності: контрольований dispatch intervention дає наведені
зміни total cost на цій native вибірці. Він не пояснює головні Full shortage/shed
втрати й не довів корисність у leaderboard normalization.

## 7. Перевірки correctness і CPU

Фінальний focused regression suite: **210 passed**. Candidate-specific suite
містить 20 cases, включно з Tiny/Small/Full integration і flag-off full-horizon
parity. Primary parity suite окремо раніше пройшов 8/8.

Перевірено shared stock, shared entry capacity, shared receiver budget,
observed ban/zero, masks, hidden padding, unknown transit, bounds,
route-preference ranking, duplicate fleet weights, output storage, solver timeout,
dispatch-source arrivals і правильну pre-release phase chokepoint arrivals.
Enabled candidate на однакових observations зберігає fuel pulses та overrides;
flag-off дає точні champion actions кожного тижня. Output shapes, finite values,
nonnegativity, masks і відсутність mutation observation перевірені.

`ruff check` нових helper, tests і scripts пройшов. Champion/frozen git diff
від base порожній. ZIP validation та import checks candidate проходять.

Native timing, constructor включено у week 1:

| Task | Candidate max week CPU | Max week 1 | Native would-over-budget |
| --- | ---: | ---: | ---: |
| Small | 0,171875 s | 0,171875 s | 0 / 416 weeks |
| Full | 0,203125 s | 0,203125 s | 0 / 832 weeks |

Native whole-week failures: 0. Positive/unclassified invalid entries: 0.
Full мав 103 ignored zero masked override diagnostics на епізод і для candidate,
і для champion; це не 103 whole-week fallbacks. У trace є фактичні reasons.
Solver status `recovered` траплявся 45 разів у Full і 50 у Small; цей лічильник
показує successful solve, не обов'язково positive addition у кожному випадку.

Windows `process_time` має грубу дискретність; частина native runs перекривалася
з тестами/іншим task. Це не isolation і не server CPU certificate.

`sbf check` запускали для champion і candidate на Small/Full. Static ZIP/import
stage проходить. Timed stage зупиняється на `ModuleNotFoundError: fcntl`.
`examples/09_team_evaluate.py` score mode також відхиляє Windows.
Benchmark package не патчився, Linux/WSL не встановлювався.

Первинний Tiny audit мав implementation error у offline accessor `Env.state`;
виправлено на public `trajectory.records`, після чого Tiny audit і native smoke
пройшли. Цей failed run не є policy screen і не включений у matched averages.

## 8. Артефакти, відтворення й передача Нікіті

У Git додано compact result JSON усіх completed screens, baseline audit summary,
week-52 excerpt і manifest: `docs/experiments/vitya-primary-20261008/`.
Вони доступні після pull; великі JSONL traces, logs і ZIPs залишаються під
gitignored `outputs/`.

Локальні artefacts:

```text
outputs/handoff/candidate.zip
outputs/handoff/baseline.zip
outputs/audit-primary-full/episode-0.jsonl
outputs/audit-primary-full/episode-1.jsonl
outputs/native-full-screen/candidate.zip         # neutral previous variant
outputs/native-full-suppression-recovery/result.json
outputs/native-small-suppression-recovery/result.json
outputs/regression-final.log
outputs/candidate-small-check-final.log
outputs/candidate-full-check-final.log
```

Native reproduction, Windows чи Linux, із locked environment:

```bash
uv run --locked python examples/15_primary_dispatch_audit.py --task=full --entropy=67890 --episodes='[0,1]'
uv run --locked python examples/16_primary_native_screen.py --task=full --entropy=202610081 --episodes=8
uv run --locked python examples/16_primary_native_screen.py --task=small --entropy=202610081 --episodes=8
```

Якщо команда вирішить витратити час на official screen, для Нікіти на Linux:

```bash
uv run --locked sbf check team_agent_vitya_v1 --task=small
uv run --locked sbf check team_agent_vitya_v1 --task=full
uv run --locked python examples/09_team_evaluate.py --agent=team_agent_vitya_v1 --against=team_agent --task=full --entropy=202610081 --episodes=16 --quick=False --cpu_budget=True --n_jobs=1
uv run --locked python examples/09_team_evaluate.py --agent=team_agent_vitya_v1 --against=team_agent --task=small --entropy=202610081 --episodes=16 --quick=False --cpu_budget=True --n_jobs=1
```

Перевірити обидва SHA перед score. Для promotion потрібні balanced sets на
кількох roots, untouched confirmation, positive paired RSS interval та відсутність
CPU fallback. Старий final root 20261013 не використовувати для tuning.

До рішення команди: не promote candidate, не змінювати champion params і
не upload на Codabench. Подальший дорогий пошук цієї самої recovery зміни не
виправданий поточним Full signal. Наступна гіпотеза має пояснювати значний
shortage/shed через конкретний trace, а не лише disposal чи більші requests.
