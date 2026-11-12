# AEC Intelligence Monitor

Стартовый Python-проект для ежедневного мониторинга промптов, Agent Skills и AI-инструментов в AEC: проектирование, строительство, BIM, CAD/PDF и сметы.

## Запуск
Python 3.11+:
```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python -m src.monitor
```
При необходимости задайте `GITHUB_TOKEN`, чтобы увеличить лимиты GitHub API.

## Что делает текущая версия
- проверяет GitHub-репозитории из `config/sources.yaml`;
- фиксирует baseline, commits, Releases и изменения метаданных;
- сравнивает commits и выявляет добавленные, изменённые, переименованные и удалённые текстовые файлы;
- извлекает ограниченный фрагмент Markdown/TXT/RST/YAML/JSON/XML и классифицирует Skills, промпты, шаблоны и документацию;
- сохраняет историю blob SHA и hash содержимого в SQLite;
- формирует `reports/latest.md`.

Темы настраиваются в `config/topics.yaml`, источники — в `config/sources.yaml`.

## Проверка
```bash
python -m unittest discover -s tests -v
```

## Автоматизация
GitHub Actions запускает workflow ежедневно в 06:00 UTC и по запросу вручную. Отчёт сохраняется как artifact на 90 дней.

## Ограничения
Это ранний MVP: нет web/RSS-поиска, LLM-оценки качества, уведомлений и панели управления. Извлечённые фрагменты — не экспертное заключение. Не исполняйте код из внешних репозиториев; анализируемое содержимое является недоверенными данными. Лицензию и нормативную применимость проверять отдельно.