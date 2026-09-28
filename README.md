# Order Book Pressure Screener → Telegram

Скрипт цілодобово стежить за стаканом топ-30 пар USDT на Binance Spot і
шле в Telegram алерт, коли на якомусь рівні обʼєм суттєво перевищує
сусідні (медіану рівнів тієї ж сторони) — тобто зʼявляється "стіна".

Працює як звичайний Python-процес: доки він запущений на якійсь машині
(VPS, Raspberry Pi тощо), моніторинг триває — незалежно від того, чи
відкритий у вас браузер чи увімкнений ваш особистий компʼютер.

---

## 1. Створити Telegram-бота і дізнатись chat_id

1. У Telegram знайдіть **@BotFather**, надішліть `/newbot`, дайте боту імʼя —
   отримаєте **токен** виду `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
2. Напишіть своєму новому боту будь-яке повідомлення (наприклад "привіт") —
   боти не можуть писати першими, тому це обовʼязковий крок.
3. Дізнайтесь свій **chat_id**: відкрийте в браузері
   `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`
   (підставте свій токен замість `<ВАШ_ТОКЕН>`), знайдіть у відповіді
   `"chat":{"id": 123456789, ...}` — це число і є chat_id.

## 2. Встановити залежності

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. Налаштувати

Найпростіше — задати змінні оточення перед запуском:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
export TELEGRAM_CHAT_ID="123456789"
```

Або відкрити `orderbook_screener.py` і вписати значення прямо в змінні
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` на початку файлу.

Інші параметри (теж через env, або прямо у файлі):

| Змінна | За замовчуванням | Що робить |
|---|---|---|
| `TOP_N_COINS` | 30 | скільки монет моніторити |
| `POLL_INTERVAL_SEC` | 20 | пауза між повними циклами опитування |
| `SENSITIVITY` | 5.0 | у скільки разів обʼєм рівня має перевищувати медіану, щоб рахуватись стіною |
| `MIN_USD_WALL` | 30000 | мінімальний обʼєм рівня в $, нижче якого не алертити (фільтр шуму) |
| `DEPTH_LIMIT` | 50 | скільки рівнів стакану з кожної сторони тягнути |
| `REFRESH_LIST_EVERY` | 1800 | як часто (сек) оновлювати топ-N список монет |

## 4. Перевірити локально

```bash
python3 orderbook_screener.py
```

У консолі побачите лог циклів; коли зʼявиться нова стіна — прилетить
повідомлення в Telegram.

## 5. Запустити цілодобово

### Варіант А (безкоштовно, без сервера, без термінала) — GitHub Actions

Найпростіший спосіб мати це працюючим 24/7 без оренди сервера й без
Python на своєму компʼютері. Все робиться через веб-інтерфейс GitHub.
Перевірятиме стакан раз на ~10 хв (обмеження безкоштовного тарифу).

1. Зареєструйтесь на [github.com](https://github.com) (безкоштовно), якщо ще
   немає акаунту.
2. Створіть новий **публічний** репозиторій (кнопка "New repository";
   публічний — щоб Actions були безлімітно безкоштовними).
3. Завантажте в нього всі файли з цього комплекту, зберігаючи структуру
   папок:
   - `orderbook_screener.py`
   - `requirements.txt`
   - `.github/workflows/screener.yml`

   (кнопка "Add file → Upload files" на GitHub підтримує перетягування
   цілих папок разом з підпапками).
4. Додайте два секрети: **Settings → Secrets and variables → Actions →
   New repository secret**:
   - `TELEGRAM_BOT_TOKEN` — ваш токен бота
   - `TELEGRAM_CHAT_ID` — ваш chat_id

   Секрети ніде не відображаються після збереження — це безпечне місце
   для токена, на відміну від коду чи чату.
5. Перейдіть у вкладку **Actions** репозиторію, знайдіть workflow
   "Order Book Pressure Screener", натисніть **Run workflow** один раз
   вручну — перевірити, що все працює (прийде повідомлення в Telegram,
   якщо на той момент є активні стіни; якщо ні — просто побачите
   зелену галочку успішного запуску в логах).
6. Далі воно вже само запускатиметься кожні ~10 хв за розкладом
   (`cron` у файлі `screener.yml`) — комп'ютер вимикати можна,
   моніторинг триває на серверах GitHub.

### Варіант Б — VPS (для інтервалу 15-20 сек замість 10 хв): systemd-сервіс

Скопіюйте файли на сервер (`scp` або git), встановіть залежності як вище,
тоді створіть `/etc/systemd/system/obp-screener.service`:

```ini
[Unit]
Description=Order Book Pressure Screener
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/obp-screener
Environment=TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Environment=TELEGRAM_CHAT_ID=123456789
ExecStart=/home/YOUR_USER/obp-screener/venv/bin/python3 /home/YOUR_USER/obp-screener/orderbook_screener.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Потім:

```bash
sudo systemctl daemon-reload
sudo systemctl enable obp-screener
sudo systemctl start obp-screener
sudo systemctl status obp-screener   # перевірити, що працює
journalctl -u obp-screener -f        # дивитись лог в реальному часі
```

`Restart=always` означає, що навіть якщо процес впаде чи сервер
перезавантажиться, він сам піднімається знову.

### Варіант В — домашній ПК / Raspberry Pi: tmux/screen

Якщо немає VPS, можна запустити у фоновій сесії, яка переживе закриття
термінала (але не переживе вимкнення самого компʼютера):

```bash
tmux new -s obp
python3 orderbook_screener.py
# Ctrl+B, потім D — щоб відʼєднатись, сесія продовжить працювати
# повернутись: tmux attach -t obp
```

Для Raspberry Pi краще одразу налаштувати systemd-сервіс (варіант А) —
тоді скрипт автоматично стартуватиме після кожного перезавантаження Pi.

---

## Обмеження та застереження

- Це технічний індикатор ліквідності стакану, а не торговий сигнал —
  великі заявки можуть бути spoofing (скасовуються до виконання).
- Використовується публічний Binance API без ключа — жодних торгових
  операцій скрипт не виконує, тільки читає дані.
- Дотримуйтесь лімітів Telegram Bot API (не більше ~20 повідомлень/хв
  в один чат) — за замовчуванням скрипт агрегує всі нові стіни одного
  циклу в одне повідомлення, тож цього зазвичай достатньо.
