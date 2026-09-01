# susucal

Синк расписания ЮУрГУ-Онлайн и Moodle в календари iCloud (CalDAV).

## Настройка

```bash
cp settings.example.yml settings.yml && $EDITOR settings.yml
```

Креды и фильтры - только в `settings.yml` (файл в `.gitignore`).

## Запуск

```bash
pip install -e .
python -m susucal                 # один прогон
python -m susucal --interval 3h   # цикл
python -m susucal --plan          # показать план, iCloud не трогать
```

## Docker

```bash
docker build -t susucal .
docker run -d --name susucal --restart unless-stopped \
  -e SUSUCAL_INTERVAL=3h -e TZ=Asia/Yekaterinburg \
  -v "$PWD/settings.yml:/config/settings.yml:ro" \
  -v susucal-state:/state \
  susucal
```

`compose.yaml` - то же самое для тех, кто предпочитает compose.
