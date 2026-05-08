# Tailwind static build

Admin-CSP запрещает `'unsafe-inline'` для `style-src`, поэтому Tailwind не может
работать через Play CDN (`https://cdn.tailwindcss.com`) — он инжектит
сгенерированный CSS в runtime в `<style>` тег. Вместо этого мы используем
prebuilt CSS-bundle, собираемый локально через стэндалон-CLI (без Node.js).

## Когда пересобирать

Каждый раз, когда в `app/templates/*.html` появляется НОВЫЙ Tailwind-класс,
которого ещё не было. CSS не гонится в CI — собранный `app/static/tailwind.css`
коммитится в репо.

## Как пересобрать

```bash
pip install pytailwindcss
cd app/static
tailwindcss -i src/tailwind.input.css -o tailwind.css --minify
```

`pytailwindcss` скачает Tailwind standalone CLI (Go binary, ~25 МБ) при первом
запуске. Размер итогового `tailwind.css` ~42 КБ — tree-shake по `@source`
директиве в `src/tailwind.input.css`.

## Что будет если забыть пересобрать

Новый класс в шаблоне просто не отрендерится (CSS-правила нет → стиль
применится только если такой класс уже использовался где-то ещё или у
браузера есть default). На страницах админки ты это заметишь визуально.
