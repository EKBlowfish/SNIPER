# ZX Spectrum Watcher

Scrapes **Marktplaats, eBay, Kleinanzeigen and Vinted** for listings (default
search: *zx spectrum*), tracks their price history, and flags bargains. It can
focus the view on *actual ZX Spectrum computers* and filter out the games,
power supplies and spare parts. Available as both a web app and a desktop app,
sharing one scraping/storage core.

## Run it

```bash
pip install -r requirements.txt

python app.py        # web UI  →  http://127.0.0.1:5000
python desktop.py    # Tkinter desktop UI
```

Both read and write the same `ads.sqlite3` database in the project root, so run
one at a time.

## Project layout

```
app.py              # entry point → web UI
desktop.py          # entry point → desktop UI
sniper/             # application package
├─ config.py        # all tunables (+ environment overrides)
├─ models.py        # the Item dataclass
├─ store.py         # thread-safe SQLite persistence
├─ net.py           # HTTP session / polite fetching
├─ scraper.py       # search, parsing, and the background fetch worker
├─ settings.py      # user settings persisted to settings.json
├─ web/             # Flask app + static front-end (HTML/CSS/JS)
└─ desktop/         # Tkinter app
tools/
└─ bids_analysis.py # CLI to plot an item's price history (matplotlib)
```

## Web UI features

- **Dark mode** — toggle in the toolbar (🌙 / ☀️), remembered across restarts.
- **Filtering** — by text, source (Marktplaats / eBay), min/max price, and a
  "Deals only" toggle.
- **Live updates** — results stream in over Server-Sent Events as they're found.
- **New-deal notifications** — opt-in toasts (and an optional sound / browser
  notification) when a fresh deal appears or a tracked price drops.
- **Persisted settings** — search term, theme, filters, sort order and
  notification preference survive a restart (stored in `settings.json`).
- **CSV export** of the current listings.

## Configuration

Everything in `sniper/config.py` can be overridden with environment variables,
e.g. `SEARCH_ITEM`, `DEAL_THRESHOLD_EUR`, `AUTO_FETCH_MINUTES`,
`AUTO_FETCH_ENABLED`, and the `FX_*_EUR` currency rates.
