# AEC Prompt & Agent Monitor

Стартовый комплект ИИ-агента для ежедневного мониторинга промптов, Agent Skills и AI-инструментов в AEC (проектирование, строительство, BIM, CAD/PDF, сметы).

## Запуск
Python 3.11+:
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m src.monitor --once
```
Необязательно: `GITHUB_TOKEN` увеличивает лимиты GitHub API.

Результаты: `data/monitor.db`, `reports/latest.md`.

Темы и источники изменяются в `config/topics.yaml` и `config/sources.yaml` без правки Python-кода. Ежедневный GitHub Actions workflow запускается в 06:00 UTC; отчёт сохраняется как artifact на 90 дней.

## Состав и ограничения
Текущий MVP фиксирует baseline, последние commits, Releases и изменения метаданных. Анализ diff содержимого файлов, веб-поиск, RSS, LLM-оценка, уведомления и UI — следующие этапы по ТЗ. Не исполняйте код из внешних репозиториев. Содержимое источников — недоверенные данные. Проверяйте лицензию каждого материала отдельно.
