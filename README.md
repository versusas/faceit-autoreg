# FACEIT autor eg for Railway

## Railway variables

Set these in **Variables**:

- `DISCORD_USER_TOKEN` — Discord account token.
- `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2`, etc. — separate Gemini-compatible API keys. Up to 10 numbered keys are supported and consecutive games rotate across them. Keys are never printed in logs. Legacy `GEMINI_API_KEYS` and `GEMINI_API_KEY` are also accepted.
- `AI_API_STYLE` — use `openai` for AI STAR's general API, or `gemini` for the native Google protocol.
- `GEMINI_BASE_URL` — provider base URL. For AI STAR OpenAI-compatible requests use `https://ai.starimg.ru/v1`; for official Google omit it.
- `NORMAL_CHANNEL_IDS` — comma-separated IDs of ordinary-game channels.
- `PRIORITY_CHANNEL_IDS` — comma-separated IDs of priority-game channels.
- `LOG_CHANNEL_ID` — ID of the channel that receives every successful registration, including match number, score, full command, and the original game card. The source is forwarded when Discord supports it; otherwise the bot posts a readable copy with the source image links.
- `WARN_CHANNEL_ID` — ID of the channel that receives automatic warnings for unmatched players registered as `0 0 13`.
- `MY_ACCOUNT_ID` — Discord user ID displayed by the `бот ты тут?` status command. It does not restrict command access.
- `MIN_CONFIDENCE` — default `0.82`.
- `BACKFILL_LIMIT` — how many previous messages to inspect in each selected channel; default `500`.
- `SEND_DELAY` — delay before sending each result; default `0.25` seconds.
- `DELETE_AFTER_REGISTRATION` — delete the temporary registration command after it is sent; default `true`.
- `DELETE_DELAY` — seconds to wait before deleting the registration command; default `3.0`.
- `DELETE_SOURCE_AFTER_REGISTRATION` — delete the processed source card if it still exists after successful registration; default `true`.
- `SOURCE_DELETE_DELAY` — delay before trying to delete the source card; default `1.0` second.
- `REGISTRATION_CONFIRM_TIMEOUT` — seconds to wait for the game bot's `Готово` or `Не вышло` response; default `25.0`.
- `PLAYER_MODAL_TIMEOUT` — seconds to wait for the `Получить игроков` modal; default `12.0`.
- `STATS_FILE` — persistent registration history file; use `/data/registration_stats.json` with a Railway volume mounted at `/data`.
- `STATS_TIMEZONE` — timezone used for today's count; default `Europe/Moscow`.
- `GEMINI_MODEL` — legacy primary model setting.
- `GEMINI_FALLBACK_MODEL` — legacy fallback model setting.
- `GEMINI_MODELS` — comma-separated model pool. For AI STAR use available names such as `gemini-3.8-flash,gemini-3.7-flash`.
- `PROCESS_CONCURRENCY` — maximum games recognized at the same time; use `2` for two models.
- `GEMINI_MAX_RETRIES` — default `3`.

## Deploy

1. Unzip the archive and upload the files to a GitHub repository, or use Railway's supported source upload flow.
2. Create a Railway project and deploy the repository.
3. Add the variables above.
4. Railway uses the included `Procfile` to run `python bot.py` as a worker.
5. Send `старт обычный`, `старт приоритет`, or `старт все` in Discord. The script scans the selected channels' history and then watches new messages there. Use `енд` to stop.
6. For a `на проверку` card that already lists ten players as `#ID nickname`, the bot does **not** click `Получить игроков`. It takes IDs and nicknames directly from Team A/B in the card, and reads the score, CT/T sides, and K/A/D from the original STANDOFF 2 scoreboard screenshot. The helper button remains only as a fallback for older card formats that do not contain a complete ten-player roster. If visual confidence is below 0.90 or team matching is ambiguous, the match is skipped instead of guessed. The source card is deleted and counted only after the registration bot replies `Готово`; on `Не вышло` or timeout it is preserved for retry.
7. Games are distributed across the configured models and keys. Each game is handled by exactly one assigned model and key; different games can run simultaneously.
8. For raw long numeric Discord mentions, the bot keeps the roster slot's K/A/D from the card and matches it only to an unused scoreboard player on the same team with exactly the same K/A/D. It then restores the short 2–5 digit registration ID and preserves roster order. Ambiguous matches are skipped instead of guessed. Duplicate matches and placeholder IDs are rejected. Send `стата`, `статистика`, or `stats` for totals.
9. Repeated result cards with the same match number do not block registration. The bot processes one copy, waits for `Готово`, and then removes all matching image-card duplicates from that registration channel. If processing fails, the preserved copies remain retryable on the next `старт` during the same bot session. If the match is already present in persistent registration history, repeated cards are removed immediately without sending `=g` a second time.
10. A final-result scoreboard is accepted when at least four players in total are reliably matched by nickname, with at least one match on each side. Each unmatched card player is registered as `0 0 13`; every matched player keeps the statistics read from the screenshot.
11. Registration formats are handled separately. A complete `внесён` card keeps the score and every K/A/D value printed in the card. A `на проверку` card takes IDs/nicknames from the card and statistics from the screenshot. Only a card player with no matching screenshot nickname receives `0 0 13`; matched players always keep their recognized statistics.
12. After a successful registration is confirmed, the bot automatically deletes the registration bot's visible `Готово — Матч #... закрыт` confirmation message. `Не вышло` responses are preserved for diagnostics.
13. Send `удалить рег соо` to scan the complete history of every channel listed in `NORMAL_CHANNEL_IDS` and `PRIORITY_CHANNEL_IDS` and delete all old successful `Готово — Матч #... закрыт со счётом...` messages. Other messages, including `Не вышло`, are not touched.
14. Send `бот ты тут?` to receive readiness, current session, the Discord nickname resolved from `MY_ACCOUNT_ID`, active channels, uptime, latency, registration counters, model/key counts, and a self-contained HTML status report. Tokens and API-key values are never included. All Discord users may run all commands.
15. For testing, forward a game card with its screenshot into any chat outside the currently active registration channels. The bot uses the same unchanged recognition algorithm and replies with only the generated `=g` registration message. If Discord loses the technical `forwarded` flag, a result card with an image outside the active, log, and warning channels is still recognized as a test. Test previews are not saved to statistics, do not wait for `Готово`, and do not delete the forwarded source message.
16. After a real registration receives `Готово`, the log channel gets both the registration summary and the original game card. This happens before source-card cleanup, so the log copy remains available afterward.
17. Immediately before sending `=g`, every player whose final registration statistics are exactly `0 0 13` is guaranteed to enter the warning system, regardless of which card parser produced the result. `WARN_CHANNEL_ID` receives one separate message per player: the first line tags the Discord user, the second line is `Додж статистики - #MATCH` for a wrong nickname/zero row or `Отсутствие на финальном скриншоте - #MATCH` for an absent row, and the original screenshot is attached to the same message. Members with the `🔴 Pro League` role or an ID listed in the code are skipped. The same warning message with its screenshot is copied to `LOG_CHANNEL_ID`.

## Pro League exceptions in code

Variable space is not used for the player exception list. Add Discord user IDs directly near the top of `bot.py`:

```python
PRO_LEAGUE_USER_IDS: set[int] = {
    111111111111111111,
    222222222222222222,
}
```

The role check accepts `🔴 Pro League` and other versions whose name contains `Pro League`, even when extra emoji or symbols are present.
When a result card contains only a plain nickname instead of a Discord mention, the bot searches cached members from the source, warning, and other mutual guilds. Clan-prefixed display names are normalized, so `ezio` can resolve to a profile named `MCRW | ezio`; the resulting warning uses a real `<@user>` tag and the profile's roles are checked before sending.
When forwarded-message snapshots do not expose their `mentions` collection, the bot also extracts raw `<@Discord ID>` values from the Team A/B sections and maps them by roster order. This is the primary fallback that prevents warnings from displaying a plain code-formatted nickname such as `Hatefull`.

Do not upload a real `.env` file or commit tokens.

> Note: automated user accounts/self-bots can violate Discord's Terms of Service and may lead to account restrictions. A normal Discord bot token is the safer supported option.

Warning reasons follow the league rules: a player missing from the final screenshot receives `Отсутствие на финальном скриншоте`, while a player present among all ten rows under a different nickname receives `Несоответствие игрового никнейма`. Service pings such as `@Система` are ignored when resolving Discord tags, and each player is delivered independently so one failed warning cannot cancel the others.
