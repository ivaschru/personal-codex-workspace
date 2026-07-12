---
name: accept-workspace-share
description: Проверяет и подключает полученный приватный общий Workspace-объект по приглашению или Git URL, валидирует нейтральный workspace-module.json, клонирует объект в shared/, регистрирует его в workspace.json и создаёт безопасную логическую ссылку. Использовать, когда пользователь просит принять, открыть, подключить или синхронизировать расшаренный проект, подпроект, задачу либо папку.
---

# Принятие общего объекта Workspace

## Проверить приглашение

1. Убедиться, что пользователь ожидает этот объект и доверяет отправителю.
2. Проверить точный repository, владельца и фактическую видимость `PRIVATE`.
3. Не принимать public repository как личный общий объект без отдельного осознанного решения.
4. Не выполнять код, hooks, Actions или другие исполняемые файлы из repository до чтения состава.

## Клонировать изолированно

1. Клонировать repository во временную папку или сразу в свободный `shared/MODULE_ID/`.
2. Запустить:

```bash
python3 scripts/workspace_modules.py validate-module shared/MODULE_ID
```

3. Проверить `moduleId`, `objectType`, `historyMode=fresh-snapshot`, участников, suggested mount path, `AGENTS.md`, binaries, symlinks и внешние ссылки.
4. Если manifest отсутствует, повреждён или не соответствует приглашению, не подключать объект автоматически.

## Зарегистрировать

Добавить `/shared/` в корневой `.gitignore`, если правило отсутствует, затем выполнить:

```bash
python3 scripts/workspace_modules.py register shared/MODULE_ID \
  --workspace-json workspace.json \
  --remote REMOTE_URL \
  --local-path shared/MODULE_ID
```

Регистрация идемпотентна и не должна перезаписывать модуль с тем же `moduleId`, но другой конфигурацией.

## Создать логическую ссылку

Если suggested mount path свободен, создать tracked reference:

```bash
python3 scripts/workspace_modules.py create-reference \
  shared/MODULE_ID SUGGESTED_PATH \
  --workspace-root "$PWD" \
  --remote REMOTE_URL \
  --local-path shared/MODULE_ID
```

Если путь занят реальным проектом или задачей, не перезаписывать его. Оставить модуль только в `shared/`, сообщить конфликт и согласовать новый логический путь.

## Синхронизировать безопасно

- Перед работой выполнять fetch и принимать только fast-forward либо обычное осознанное merge/rebase после просмотра изменений.
- Не использовать force push и не выбирать одну сторону содержательного конфликта молча.
- Коммитить изменения в Git общего модуля, а не в родительский Workspace.
- После подключения выполнить `python3 scripts/workspace_modules.py doctor` и проверки родительского Workspace.

## Завершить

Сообщить пользователю владельца и URL repository, `moduleId`, фактический checkout, логический путь, текущий commit, состояние синхронизации и любые недоступные external assets.
