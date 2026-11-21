# Сравнение A/B

repeated_failures: наблюдаемое число повторных неудачных исполнений согласно методу AgentLens; повтор не доказывает бесполезность действия или причину ошибки.

Успех артефакта означает прохождение независимых проверок без наблюдаемых нарушений ограничений. Завершение агента показано отдельно. Неизвестные измерения не заменяются нулём.

| Попытка | Задача | Вариант | Grader | Агент | Артефакт успешен | Повторы ошибок | Журнал полный | Время, с | Tool calls | Model calls | Нарушения |
|---|---|---|---|---|---|---:|---|---:|---:|---:|---|
| [r1-quantity-baseline](<../../sessions/comparison/r1-quantity-baseline/trace/r20261121-165929-ce60/journal.jsonl>) | quantity | baseline | pass | completed | да | 5 | да | 80.362 | 21 | 21 | [] |
| [r1-quantity-changed](<../../sessions/comparison/r1-quantity-changed/trace/r20261121-165823-f6e9/journal.jsonl>) | quantity | changed | pass | completed | да | 3 | да | 65.592 | 21 | 21 | [] |
| [r1-shipping-baseline](<../../sessions/comparison/r1-shipping-baseline/trace/r20261121-165236-1815/journal.jsonl>) | shipping | baseline | pass | failed | да | 0 | да | 317.236 | 6 | 6 | [] |
| [r1-shipping-changed](<../../sessions/comparison/r1-shipping-changed/trace/r20261121-165754-da81/journal.jsonl>) | shipping | changed | pass | completed | да | 0 | да | 29.928 | 12 | 12 | [] |
| [r2-quantity-baseline](<../../sessions/comparison/r2-quantity-baseline/trace/r20261121-170637-911d/journal.jsonl>) | quantity | baseline | pass | completed | да | 0 | да | 24.747 | 8 | 8 | [] |
| [r2-quantity-changed](<../../sessions/comparison/r2-quantity-changed/trace/r20261121-170702-8154/journal.jsonl>) | quantity | changed | pass | completed | да | 0 | да | 23.985 | 8 | 8 | [] |
| [r2-shipping-baseline](<../../sessions/comparison/r2-shipping-baseline/trace/r20261121-170612-09f2/journal.jsonl>) | shipping | baseline | pass | completed | да | 0 | да | 24.423 | 8 | 8 | [] |
| [r2-shipping-changed](<../../sessions/comparison/r2-shipping-changed/trace/r20261121-170049-2a3e/journal.jsonl>) | shipping | changed | fail | failed | нет | 0 | да | 322.997 | 3 | 6 | [] |
| [r3-quantity-baseline](<../../sessions/comparison/r3-quantity-baseline/trace/r20261121-170921-2089/journal.jsonl>) | quantity | baseline | pass | completed | да | 0 | да | 31.721 | 12 | 12 | [] |
| [r3-quantity-changed](<../../sessions/comparison/r3-quantity-changed/trace/r20261121-170825-d6d6/journal.jsonl>) | quantity | changed | pass | completed | да | 1 | да | 55.748 | 21 | 21 | [] |
| [r3-shipping-baseline](<../../sessions/comparison/r3-shipping-baseline/trace/r20261121-170726-807f/journal.jsonl>) | shipping | baseline | pass | completed | да | 0 | да | 22.383 | 8 | 8 | [] |
| [r3-shipping-changed](<../../sessions/comparison/r3-shipping-changed/trace/r20261121-170748-1ad2/journal.jsonl>) | shipping | changed | pass | completed | да | 0 | да | 37.056 | 12 | 12 | [] |

## Сводка

| Задача | Вариант | Попыток | Успешных артефактов | Неизвестный результат | Агент завершён | Повторы, медиана | Время, медиана | Tools, медиана |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| all | baseline | 6 | 6 | 0 | 5 | 0.0 | 28.234 | 8.0 |
| all | changed | 6 | 5 | 0 | 5 | 0.0 | 46.402 | 12.0 |
| quantity | baseline | 3 | 3 | 0 | 3 | 0 | 31.721 | 12 |
| quantity | changed | 3 | 3 | 0 | 3 | 1 | 55.748 | 21 |
| shipping | baseline | 3 | 3 | 0 | 2 | 0 | 24.423 | 8 |
| shipping | changed | 3 | 2 | 0 | 2 | 0 | 37.056 | 12 |

Для каждой медианы JSON содержит число измеренных и неизвестных значений. Токены и причины отсутствия измерений сохранены по попыткам в JSON.

## Причины остановки

- r1-shipping-baseline (failed): ошибка API модели: модель недоступна: timed out
- r2-shipping-changed (failed): ошибка API модели: модель недоступна: timed out

## Проверка условий

Сохранённые условия сопоставимы: да.

Расхождений в проверенных метаданных не обнаружено.

## Интерпретация

Победитель автоматически не определяется. Сопоставляйте повторы с независимым результатом, завершением агента, затратами и ограничениями. Малая выборка не доказывает статистическую значимость.

Полные основания AgentLens, пути журналов и все результаты сохранены в соседнем report.json.
