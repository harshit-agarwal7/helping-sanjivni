# Bhulekh Odisha — Bulk Khatiyan (RoR) PDF Downloader

Bulk download Record of Rights (RoR/Khatiyan) PDFs from [bhulekh.ori.nic.in](https://bhulekh.ori.nic.in) via a simple web interface.

![Demo](/real-estate-dd/searching-bhulekh/bhulekh_bulk_download.gif)

## How It Works

1. Enter a **District**, **Tahsil**, and **Village**.
2. Provide a comma-separated list of **Khatiyan numbers**.
3. Hit **Start** — the app launches a headless browser, navigates the portal for each Khatiyan, and saves the RoR as a PDF.
4. Download the generated PDFs from the results table.

Progress is streamed in real time via Server-Sent Events.

## Setup

```bash
# Create a virtualenv and install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install the Playwright Chromium browser
playwright install chromium
```

## Usage

```bash
python app.py              # starts the web UI on http://localhost:2173
python app.py --port 8080  # use a custom port
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PLAYWRIGHT_HEADLESS` | `true` | Set to `false` to watch the browser while it works |
| `PLAYWRIGHT_SLOW_MO_MS` | `0` | Add a delay (ms) between Playwright actions for debugging |
| `PLAYWRIGHT_DEVTOOLS` | `false` | Open Chrome DevTools automatically (headed mode only) |

### Discovery Mode

Inspect the Bhulekh portal's form elements (useful for debugging selector changes):

```bash
python app.py --discover           # headed
python app.py --discover-headless  # headless
```

## Project Structure

```
searching-bhulekh/
├── app.py              # Flask server + Playwright scraping logic
├── templates/
│   └── index.html      # Web UI
├── requirements.txt
└── output/             # Downloaded PDFs (created at runtime)
```
