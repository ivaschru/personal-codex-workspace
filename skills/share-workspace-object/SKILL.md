---
name: share-workspace-object
description: Безопасно превращает существующий проект, подпроект, задачу, процедуру или папку Workspace в отдельный приватный совместный Git-репозиторий с fresh-snapshot историей, приглашает точно определённого получателя и заменяет исходный объект логической ссылкой. Использовать, когда пользователь просит поделиться, сделать общим, передать для совместной работы или синхронизировать выбранный объект с другим пользователем.
---

# Передача объекта Workspace

## Зафиксировать намерение

1. Определить точный корень объекта из контекста или пути пользователя.
2. Определить получателя по точному GitHub login или подтверждённой ссылке на профиль. Совпадения имени недостаточно.
3. Использовать нейтральное имя `{workspace}-{object-type}-{slug}`. Разрешать уточнённый slug пользователя, например `lichnoe-project-family-health`.
4. Использовать только `historyMode=fresh-snapshot`: старая история остаётся в личном Workspace, совместная начинается с импортного commit.

Явная команда «поделись X с Y» разрешает создать private repository, отправить приглашение названному получателю и локально подключить общий объект. Она не разрешает публичную видимость, передачу всего родительского Workspace, угадывание аккаунта или публикацию секретов.

## Проверить источник

1. Проверить видимость родительского Workspace и чистоту выбранного объекта.
2. Запустить:

```bash
python3 scripts/workspace_modules.py inspect SOURCE \
  --workspace-root "$PWD" \
  --module-slug SLUG
```

3. Разобрать все `BLOCK` и `WARN`. Не обходить блокеры.
4. Для external references решить каждую зависимость отдельно: перенести разрешённый файл, оставить явную недоступную ссылку или исключить связь.
5. Для `local-assets`, `local-private`, `.local`, секретов и symlink остановиться, пока данные не классифицированы и не сохранены безопасно.
6. Материализовать применимые наследуемые правила в корневом `AGENTS.md` объекта. Не копировать персональные правила родителя механически.
7. Ещё раз выполнить inspect; продолжать только при нуле блокеров и понятных предупреждениях.

## Подготовить fresh snapshot

Подготовить staging вне исходного объекта:

```bash
python3 scripts/workspace_modules.py prepare SOURCE STAGING \
  --workspace-root "$PWD" \
  --module-slug SLUG \
  --owner OWNER_LOGIN \
  --participant RECIPIENT_LOGIN \
  --suggested-mount-path SOURCE_RELATIVE_PATH
```

Проверить `workspace-module.json`, diff состава, отсутствие приватных путей, секретов, неожиданных binaries и внешних ссылок. Не добавлять parent Git history.

## Создать private repository

1. Инициализировать staging как новый Git repository с веткой `main` и одним импортным commit.
2. Создать GitHub repository только как private и не как fork:

```bash
gh repo create OWNER/REPOSITORY --private --source STAGING --remote origin --push
```

3. Проверить через GitHub API или CLI: точные `nameWithOwner`, `visibility=PRIVATE`, `isFork=false`, default branch и remote.
4. Отправить приглашение только подтверждённому login с минимально достаточным write-доступом.
5. Проверить, что API вернул именно ожидаемого пользователя и repository. Не считать похожий профиль подтверждением личности.

## Подключить один канонический экземпляр

1. Клонировать новый repository в `shared/MODULE_ID/`.
2. Проверить module manifest и чистоту checkout.
3. Добавить `/shared/` в корневой `.gitignore`, если правило ещё отсутствует.
4. Запустить безопасную замену исходника:

```bash
python3 scripts/workspace_modules.py replace-source SOURCE shared/MODULE_ID \
  --workspace-root "$PWD" \
  --workspace-json workspace.json \
  --remote REMOTE_URL \
  --local-path shared/MODULE_ID
```

Команда сравнивает все bytes и прекращает работу при любом расхождении. После успеха в старом пути остаётся tracked `workspace-reference.json`, а канонический checkout находится в `shared/`.

## Завершить

- Закоммитить только reference, `workspace.json`, `.gitignore` и связанные переносимые изменения родительского Workspace.
- Запустить `python3 scripts/workspace_modules.py doctor`.
- Сообщить URL private repository, точного приглашённого пользователя, исходный и локальный пути, исключённые assets и статус приглашения.
- Напомнить: отзыв доступа прекращает будущий доступ, но не удаляет уже сделанные получателем clones.
