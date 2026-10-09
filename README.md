# TokenHarbor free-chat parser

OpenAI-совместимый HTTP-прокси над бесплатным веб-чатом
[tokenharbor.ai](https://tokenharbor.ai/chat).
Принимает `POST /v1/chat/completions` (обычный JSON и `stream: true` SSE),
отдаёт `/v1/models`. Зависимостей нет — только Python 3.10+ stdlib.

Модели:

| id | upstream |
|---|---|
| `sim-base` | TH-Rudder (безлимитный фри-чат) |
| `deepseek-v4.1-flash` | DeepSeek V4.1 Flash (free-маршрут) |
| `mimo-v2.6-flash` | MiMo V2.6 Flash (free-маршрут) |
| `claude-haiku-5.5` | Claude Haiku 5.5 (free-маршрут) |

## Настройка

1. Зарегистрируйся на tokenharbor.ai и включи free-модели в дашборде
   (кнопка **Enable free models**).
2. `cp config.example.json config.json` и заполни:
   - `anon_key` — Supabase anon key. Найти в JS-бандлах сайта:
     искать строку `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9` в
     `https://tokenharbor.ai/_next/static/chunks/*.js`.
   - `refresh_token` — из куки `sb-auth-auth-token.*` в DevTools
     (Application → Cookies): декодировать base64-блок после префикса
     `base64-`, взять поле `refresh_token`.
   - `sid` — значение куки `th_sid` (необязательно, но желательно).
   Вместо файла можно задать env: `TH_ANON_KEY`, `TH_REFRESH_TOKEN`, `TH_SID`.
3. Запуск: `python3 parser.py 31090`
4. Проверка: `curl localhost:31090/v1/models`

Сессия Supabase обновляется автоматически через
`https://auth.tokenharbor.ai/auth/v1/token?grant_type=refresh_token`,
состояние хранится в `session.json` (не коммитить — он в `.gitignore`).
