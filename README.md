# MouseBridge

Простой шаринг мыши между двумя компьютерами (Windows ↔ macOS) — Python.

## Установка

### Windows (PowerShell)

```powershell
python -m pip install -r requirements.txt
```

### macOS (Terminal)

```bash
python3 -m pip install -r requirements-mac.txt
```

На macOS первый запуск попросит **Accessibility** и **Input Monitoring** для Terminal.app:
System Settings → Privacy & Security → Accessibility → добавить Terminal.app, потом то же для Input Monitoring. После выдачи — закрыть Terminal целиком (`Cmd+Q`) и открыть заново.

## Запуск

На обоих компах:

**Windows:**
```powershell
python mousebridge.py
```

**macOS:**
```bash
python3 mousebridge.py
```

С ручным IP пира (если mDNS не находит):
```
python3 mousebridge.py 192.168.1.143
```

## Как пользоваться

- Один из двух компов сразу станет «носителем курсора» (LOCAL), второй — приёмником (CAPTURED). Решается по UUID.
- Чтобы переслать курсор: довести его до правого/левого края экрана LOCAL'а — он автоматически прыгнет к peer'у.
- **Двойной Esc** (за 500мс) — panic disconnect: курсор немедленно возвращается локально, на 30 секунд оба работают независимо.

## Логика (упрощённо)

- Каждый узел в любой момент в одном из двух состояний: `LOCAL` или `CAPTURED`.
- В `LOCAL`: курсор виден, физическая мышь работает нативно. Если курсор касается границы экрана — отправляется `TakeOver`, мы переходим в `CAPTURED`, курсор скрыт.
- В `CAPTURED`: курсор скрыт + decoupled (на Mac), любые движения мыши превращаются в delta-сообщения и пересылаются peer'у. Кнопки и клавиши тоже пересылаются.
- При получении `TakeOver`: становимся `LOCAL`, курсор появляется у нужного края.

## Известные ограничения

- Кросс-OS клавиатурная раскладка пока примитивная (передаётся как символ или как `Key.<name>`).
- На Mac decoupling включается через `CGAssociateMouseAndMouseCursorPosition(False)` в момент `hide_cursor()` — нужен для убийства «магнита».
- В сильно нагруженной сети может быть лаг.
