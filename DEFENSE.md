# Шпаргалка для защиты: полная последовательность демонстрации

Все команды выполняются из корня репозитория. Работает и без сети:
демо 1–7 идут на детерминированной scripted-модели, демо 8 — опционально
на живой (Ollama, `qwen3.5:9b`).

## Сброс состояния между демо

```bash
rm -rf runs                          # состояние запусков (в .gitignore)
git checkout -- workspace/order.py   # вернуть баг-версию control-проекта
```

Если что-то пошло не так — всегда можно начать демо заново с этой пары команд.

---

## Демо 0. Автотесты runtime (≈30 с)

```bash
python -m pytest tests -q
# ожидание: 44 passed
```

Что показывает: блоки 1–3 и уровни 8–10 покрыты детерминированными тестами
(контрольная задача e2e, инструменты, журнал, бюджеты, сбои, задачи,
снапшоты/откат/конфликты).

## Демо 1. Контрольная задача целиком (блок 1 + уровни 9)

```bash
python -m agent run --plan --script demo/checkpoint_script.json --auto-approve
```

На экране: модель сама изучает проект → `submit_plan` (структурированный
план: цель, шаги, инструменты, риски, критерий готовности) → утверждение →
чеклист задач `[~]/[x]` → `write_file` → **`Bash: python -m unittest -v`** →
`complete_task` ×3, каждая задача закрыта ТОЛЬКО после запуска runtime-ом
команды проверки → `[статус] completed`.

Проверка результата (то, что требует задание):

```bash
python -m unittest discover -s workspace -v   # тесты зелёные — фикс реален
cat workspace/order.py                        # исправление на месте
git diff --stat workspace/test_order.py       # пусто — тесты не тронуты
RUN=$(ls runs | tail -1)
python -m agent status $RUN    # бюджет: 9 запросов, 9 tool calls, 2 side effects
python -m agent tasks $RUN     # чеклист + команда проверки каждой задачи
```

Указать на журнале: `runs/$RUN/journal.jsonl` — каждый `tool_call_started`
закрыт `tool_call_finished`; вызовы исполнения несут `task_id`, вызовы
планирования — нет.

## Демо 2. Plan Mode: до утверждения нет побочных эффектов (блок 3)

```bash
python -m agent run --plan --script demo/checkpoint_script.json --no-input
```

→ план напечатан, статус `awaiting_approval`, процесс ждёт. Пояснить:
в фазе планирования инструментам записи и `Bash` **не передаются модели
вовсе** (tools_for_phase в `agent/tools/__init__.py`) — побочный эффект
невозможен по конструкции. Доказательство в тестах:
`test_plan_mode_blocks_all_side_effects` (0 `started`-событий с
`side_effect=true`).

Вариант А — утвердить из другого процесса (показывает, что решение persist-ится):

```bash
python -m agent approve $RUN --script demo/checkpoint_script.json --script-skip 3
```

Вариант Б — отклонить:

```bash
python -m agent reject $RUN && python -m agent status $RUN   # aborted
```

## Демо 3. Сбой между выполнением и сохранением + resume (блок 2, уровень 8+10)

```bash
rm -rf runs; git checkout -- workspace/order.py
python -m agent run --plan --script demo/checkpoint_script.json \
    --auto-approve --failpoint after_execute:c6
echo $?        # 70 — процесс «погиб» сразу после исполнения write_file(c6)
```

Что видно: `order.py` УЖЕ исправлен (действие выполнилось), а результата
в журнале нет. Продолжение:

```bash
RUN=$(ls runs | tail -1)
python -m agent status $RUN        # вызов c6: попыток 1, последняя: in_flight
python -m agent resume $RUN --decision retry \
    --script demo/checkpoint_script.json --script-skip 5
```

На экране: карточка неопределённого вызова → **сверка дерева** («дерево
отличается от журнала: order.py — при retry/skip изменения будут отнесены
к данному запуску») → retry как НОВАЯ ПОПЫТКА того же `tool_call_id` →
`write_file` отвечает `already_applied` (файл повторно НЕ меняется) →
работа доезжает до `completed`, тесты зелёные.

Два других решения (можно показать на свежих крашах):

```bash
python -m agent resume $RUN --decision skip  ...   # шаг неуспешен, цикл продолжается
python -m agent resume $RUN --decision abort ...   # статус aborted
```

Сброс между повторами: `rm -rf runs; git checkout -- workspace/order.py`.

Что ещё показать из блока 2 (по просьбе):

- повтор `tool_call_id` с теми же аргументами → сохранённый результат, без
  исполнения (`tool_call_replayed`); с другими → `PROTOCOL_ERROR`
  (`test_duplicate_tool_call_id_replay_and_protocol_error`);
- не начатые вызовы пачки → `NOT_EXECUTED_AFTER_CRASH`
  (`test_crash_after_start_marks_pending_batch_calls`);
- бюджет пережил сбой: `status` показывает счётчики до/после resume
  (`model_requests 9/40` — не обнулился).

## Демо 4. Бюджеты (уровень 8)

Исчерпание запросов к модели:

```bash
rm -rf runs
python -m agent run --plan --script demo/checkpoint_script.json \
    --no-input --limits '{"model_requests": 2}'
echo $?        # 3 — limit_reached
```

Пачка целиком при нехватке лимита tool_calls (c2+c3 не влезают после c1):

```bash
rm -rf runs
python -m agent run --plan --script demo/checkpoint_script.json \
    --auto-approve --limits '{"tool_calls": 2}'
# → «пачка отклонена целиком»: c2, c3 → BUDGET_EXCEEDED, ни один не исполнялся
```

Обрезка вывода и пометка неполности — `test_output_truncation_then_refusal`
(списывание: усечённый результат занимает весь выделенный остаток, поэтому
следующий вызов при нулевом остатке не стартует).

## Демо 5. Diff, конфликт, откат (уровень 10)

```bash
rm -rf runs; git checkout -- workspace/order.py
python -m agent run --plan --script demo/checkpoint_script.json --auto-approve
RUN=$(ls runs | tail -1)

python -m agent diff $RUN        # unified diff order.py + хеши до/после

echo "# посторонняя правка" >> workspace/order.py
python -m agent rollback $RUN    # конфликт: файл правили извне, exit 6
head -2 workspace/order.py       # правка на месте — НИЧЕГО не перезаписано

sed -i '/посторонняя правка/d' workspace/order.py
python -m agent rollback $RUN    # восстановлен исходный (баговый) order.py
python -m unittest discover -s workspace   # тесты падают = исходное состояние
```

Пояснение: откат трогает только файлы этого `run_id`; перед откатом хеши
сверяются с журналом; до разрешения неопределённого вызова откат запрещён
(`test_undefined_effect_attributed_then_rollback`).

## Демо 6. Коды завершения CLI

| Код | Значение |
|---|---|
| 0 | completed / ожидание утверждения плана |
| 3 | limit_reached (бюджет) |
| 4 | aborted (пользователь / отклонён план) |
| 5 | failed (ошибка API модели и т.п.) |
| 6 | конфликт отката (посторонние правки) |
| 70 | управляемый сбой (failpoint) |

## Демо 7. Bash: границы (если спросят)

- таймаут: `AGENT_BASH_TIMEOUT=2 python -m agent run --task "Запусти sleep 30 через Bash и покажи вывод" --no-input` (на живой модели) → `TIMEOUT`;
- секреты не наследуются: `test_bash_env_is_clean` (`OPENAI_API_KEY` не виден);
- вывод обрезается с пометкой `truncated`: `test_bash_output_truncated`.

## Демо 8. Живая модель (опционально, ~2 мин)

`.env` уже настроен на Ollama + `qwen3.5:9b`. Проверка сервера:

```bash
curl -s http://localhost:11434/api/tags | head -c 200
```

Запуск без scripted-сценария:

```bash
rm -rf runs; git checkout -- workspace/order.py
python -m agent run --plan --auto-approve
```

Та же траектория, что в демо 1, но решения принимает живая модель
(12 запросов, ~1–2 мин на RTX 4070 Ti). Сбой/resume на живой модели —
без `--script`: история восстановится из журнала, модель продолжит сама:

```bash
AGENT_FAILPOINT=after_execute:* python -m agent run --plan --auto-approve  # упадёт на первом побочном вызове
python -m agent resume $(ls runs | tail -1) --decision retry --auto-approve
```

---

## Типовые вопросы на защите → где смотреть

| Вопрос | Ответ | Код |
|---|---|---|
| Почему модель выбрала инструмент? | Модель сама по descriptions/JSON Schema; runtime только валидирует | `agent/tools/base.py` (definition/validate), `agent/loop.py` (_handle_batch) |
| Что при ошибке инструмента? | Конверт с кодом последнего достигнутого слоя, цикл живёт | `agent/tools/base.py` (коды), `execute()` |
| Что при исчерпании бюджета? | Действие не начинается; пачка целиком; списание до действия и не возвращается | `agent/budget.py`, `agent/loop.py` (_handle_batch/_execute) |
| Что при сбое? | `started` без `finished` = неопределённый вызов, молча не повторяется; retry = новая попытка | `agent/events.py`, `agent/recovery.py` |
| Как подтверждается задача? | Только exit code команды проверки, запущенной runtime-ом | `agent/tasks.py` (complete_task/run_verification) |
| Как работает откат? | Снапшот до действия, diff после; сверка хешей перед откатом; конфликт = ничего не трогаем | `agent/changes.py` |
| Как Plan Mode запрещает запись? | Инструменты записи не передаются модели в списке tools | `agent/tools/__init__.py` (tools_for_phase) |
| Почему повтор write_file безопасен? | Полная перезапись идемпотентна + already_applied | `agent/tools/fs.py` (_write_file) |
| Где хранится состояние? | `runs/<run_id>/journal.jsonl` — append-only, fsync; state = replay | `agent/events.py`, `agent/state.py` |

## Ключевые обоснования (спросят почти наверняка)

1. **Единицы бюджета** зафиксированы: model_request = обращение к API;
   tool_call = исполненный вызов; side_effect = вызов с side_effect=true;
   output_bytes = байты JSON результатов. Служебные проверки задач (уровень 9)
   бюджет не расходуют — это механизм runtime, а не действие модели.
2. **Списание обрезанного вывода = вся выделенная аллокация**: иначе остаток
   никогда не достиг бы нуля и лимит output_bytes нельзя было бы исчерпать
   действиями с заранее неизвестным размером результата.
3. **Усечение вывода помечается явно**: `"truncated": true` + маркер в тексте.
4. **Bash не ограничен «безопасными» командами** (сопоставление по тексту —
   не граница): вместо этого детерминированная граница — снапшоты и хеши
   дерева (ловят и создание, и удаление файлов).
5. **Исключения снапшотов**: `__pycache__/`, `*.pyc` — генерируемые артефакты;
   исходники и тесты контрольного проекта они не скрывают.
6. **Защита тестов**: deny-правило в политике write_file (`test_order.py`) +
   наблюдение — сверка содержимого в acceptance-тесте. Правило без
   наблюдения — пожелание; у нас пара «механизм + наблюдение».
