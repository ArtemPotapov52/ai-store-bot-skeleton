# Storefront artwork slots

Put generated artwork in this directory. The bot detects `.jpg`, `.jpeg`,
`.png` and `.webp` automatically. Recommended format: **1600×900, 16:9, JPG,
under 5 MB**, with a dark background and enough safe space for Telegram crop.

Recognized storefront files:

1. `main-menu.png` — brand hero and the words “Главное меню”.
2. `catalog.png` — all-products hero and the word “Товары”.
3. `profile.png` — account/profile hero and the word “Профиль”.
4. `balance.png` — balance top-up hero and the word “Баланс”.
5. `legal.png` — rules and agreements hero.
6. `orders.jpg` — optional purchase-history hero and the word “Мои заказы”.

The current My Store category images are in `categories/chatgpt.png`,
`categories/gemini.png`, `categories/claude.png` and `categories/grok.png`.

Category artwork is stored on the category row as `image_ref`; product artwork
is stored on the product row. A ref may be:

- a filename below this directory, for example `categories/chatgpt.jpg`;
- an HTTPS image URL;
- an existing Telegram `file_id`.

Local refs are sandboxed to this directory. Absolute paths and `../` traversal
are rejected.
