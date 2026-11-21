# Защита ДЗ №2

PowerShell. Выполнять по порядку. Текст задачи и команды `status`, `tasks`, `diff`, `exit` вводить в чате агента.

Python, Bash и `.env` должны быть настроены по README.

## 1. Открыть терминал в корне проекта

## 2. Запустить Ollama через меню «Пуск»

## 3. Скопировать исходную задачу в папку для демонстрации

```powershell
mkdir -Force demo/defense
Copy-Item evals/tasks/discount/*.py demo/defense/
```

## 4. Запустить интерактивного агента

```powershell
python -m agent chat --workspace demo/defense --runs-dir runs/defense
```

В чате ввести:

```text
Исправь расчёт итоговой стоимости заказа: скидка должна считаться в процентах от суммы цен. Допустимая скидка — от 0 до 100, иначе ValueError. Тесты не меняй. Проверь исправление.
```

1. Показать чтение файлов и план.
2. Ввести `y` для утверждения.
3. Дождаться результата. Показать выполненные инструменты.
4. По очереди ввести:

```text
status
tasks
diff
exit
```

## 5. Проанализировать журналы демозапусков

```powershell
python -m agentlens analyze --input runs/defense
```

Показать фактический результат. Повторов может не быть. Эти запуски не входят в сохранённое A/B-сравнение.

## 6. Запустить тесты

```powershell
python -m pytest tests -q
```

Показать: `134 passed`.

## 7. Показать справку и анализ исходных сессий

```powershell
python -m agentlens analyze --help
python -m agentlens analyze --input evals/sessions/discovery
```

Показать: 3 сессии, 6 эпизодов, 18 повторов.

## 8. Показать одну сессию с ошибками

```powershell
python -m agentlens analyze --input evals/sessions/discovery/d02/trace/r20261121-164856-5846/journal.jsonl
```

Показать: 5 эпизодов, 17 повторов. Найти цепочку с 12 повторами: 13 исполнений, ожидаемый код 2, фактический 1.

## 9. Показать JSON и исходные события

```powershell
python -m agentlens analyze --input evals/sessions/discovery/d02 --json
```

1. Открыть `evals/reports/examples/session.json` — те же находки в сохранённом JSON.
2. Открыть `evals/sessions/discovery/d02/trace/r20261121-164856-5846/journal.jsonl`.
3. Перейти к строкам 67–72. Показать разные ID, одинаковую команду, ожидаемый код 2, фактический 1 и `ValueError`.
4. Открыть `evals/sessions/discovery/d02/result.json`: независимые проверки прошли, агент остановился по лимиту.

## 10. Показать реализацию метода

Открыть `agentlens/analysis.py`:

1. `analyze_session` — связь начала и результата по `(id, attempt)`.
2. `_execution` — фактический и ожидаемый исход.
3. `_findings` — совпадение команды, cwd и диагностики; число повторов `n - 1`.
4. `_between` — действия и изменения между повторами.

## 11. Показать тесты метода

```powershell
python -m pytest tests/test_agentlens.py -q
```

Показать: `56 passed`. Открыть `tests/test_agentlens.py` и показать случаи:

- Повтор есть: `test_repeated_failure_has_provenance_and_intervening_actions`.
- Другой cwd: `test_identical_command_in_different_working_directories_is_not_a_repeat`.
- Нет результата: `test_missing_result_stays_unknown_and_is_explained`.

## 12. Показать изменение инструкции и протокол

```powershell
Get-Content evals/variants/changed.txt
```

Открыть `evals/PROTOCOL.md`. Показать:

1. A — исходная инструкция; B — та же инструкция с добавкой.
2. Задачи `shipping` и `quantity`; `quantity` не использовалась при подборе изменения.
3. Две задачи × два варианта × три попытки = 12 новых запусков.
4. Одинаковые модель, лимиты, исходники и независимые проверки.
5. Критерий принятия: меньше повторов при сохранении успешности.

## 13. Пересчитать A/B-отчёт

```powershell
python -m evals.report --input evals/sessions/comparison --output runs/defense-report --comparison
```

Открыть `runs/defense-report/report.md`. Показать все 12 попыток и отсутствие замечаний о сопоставимости условий.

## 14. Показать два сбоя

Открыть `result.json` и `agent.log` в папках:

- `evals/sessions/comparison/r1-shipping-baseline`: API timeout; код независимую проверку прошёл.
- `evals/sessions/comparison/r2-shipping-changed`: неправильный план, затем API timeout; код независимую проверку не прошёл.

Показать: у неуспешной B-попытки ноль повторов, потому что агент до своих проверок не дошёл.

## 15. Показать итоговое решение

Открыть `evals/REPORT.md`:

- Повторы: 5 → 4.
- Успешные артефакты: 6/6 → 5/6.
- Вызовы инструментов: 63 → 77.
- Медиана времени: 28.234 → 46.402 с.
- Изменение отклонено: условие сохранения качества не выполнено.
