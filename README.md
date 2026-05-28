# 🚀 Lead Automation Engine

A production-ready Python automation system that reads leads from Google Sheets, rotates proxies & device fingerprints per row, fills web forms via Playwright, and writes results back to the sheet.

---

## 📁 Project Structure

```
lead-automation/
├── main.py                 # Entry point — orchestrates the full pipeline
├── config.yaml             # All configurable settings (selectors, delays, etc.)
├── .env.example            # Environment variable template
├── .env                    # Your actual env vars (git-ignored)
├── devices_pool.py         # 17 real Android device fingerprints
├── proxies.txt             # One proxy per line (optional)
├── credentials/
│   └── service_account.json  # Google Service Account key
├── utils/
│   ├── __init__.py
│   ├── sheet_handler.py    # Google Sheets read/write via gspread
│   ├── proxy_manager.py    # Proxy rotation (file / env / rotating gateway)
│   ├── device_manager.py   # Device fingerprint builder
│   └── stealth.py          # Anti-detection JS patches + human-like helpers
├── core/
│   ├── __init__.py
│   └── form_filler.py      # Playwright form automation engine
├── logs/                   # Structured log files
├── screenshots/            # Failure/success screenshots
└── requirements.txt
```

---

## ⚡ Quick Start

### 1. Clone & create virtual environment

```bash
cd lead-automation
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your Google Sheet URL, proxy settings, etc.
```

### 4. Set up Google Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Google Sheets API** & **Google Drive API**
3. Create a **Service Account** → Download the JSON key
4. Save it as `credentials/service_account.json`
5. **Share your Google Sheet** with the service account email (Editor access)

### 5. Prepare your Google Sheet

Your sheet should have these columns (names are configurable in `config.yaml`):

| Column | Description |
|--------|-------------|
| `First_Name` | Lead's first name |
| `Last_Name` | Lead's last name |
| `Email` | Lead's email |
| `Phone` | Lead's phone number |
| `Address` | Street address |
| `City` | City |
| `State` | State (abbreviation or full) |
| `Zip_Code` | ZIP / postal code |
| `Message` | Optional message field |
| `Status` | Set to **Pending** for new rows |
| `Notes` | Auto-filled with result details |
| `Proxy_Used` | Auto-filled with proxy address |
| `Last_Attempt` | Auto-filled with timestamp |
| `Retry_Count` | Auto-filled with retry counter |
| `Submission_ID` | Auto-filled with unique ID |
| `Device_Model` | Optional: e.g. "Pixel 8", "Galaxy S24" |
| `Android_Version` | Optional: e.g. "14", "15" |
| `Orientation` | Optional: "portrait", "landscape", "random" |
| `Use_Custom_Device` | "yes" to use the device columns above; else random |

### 6. Add proxies (optional)

Create a `proxies.txt` file:

```
http://user:pass@proxy1.example.com:8080
socks5://user:pass@proxy2.example.com:1080
http://proxy3.example.com:3128
```

Or set `PROXY_SOURCE=rotating` in `.env` for services like Bright Data, Smartproxy, or IPRoyal.

### 7. Configure form selectors

Edit `config.yaml` → `target.form_fields` to map your sheet columns to CSS selectors on the target form:

```yaml
target:
  url: "https://example.com/apply"
  form_fields:
    First_Name: "input[name='first_name']"
    Email: "input[name='email']"
    # ... add your selectors
  submit_button: "button[type='submit']"
  success_indicator: ".thank-you-message"
```

### 8. Run

```bash
python main.py
```

---

## 🛡️ Anti-Detection Features

- **Stealth JS injection** — Hides `navigator.webdriver`, spoofs WebGL, canvas, plugins
- **Real device fingerprints** — 17 devices (Pixel, Galaxy, OnePlus, Xiaomi, etc.)
- **Human-like typing** — Variable inter-key delays with random micro-pauses
- **Mouse movement** — Random cursor movement between form fields
- **Random scrolling** — Natural scroll behaviour before form filling
- **Per-row rotation** — Fresh proxy + fingerprint for every single row
- **Viewport jitter** — Randomised dimensions within realistic ranges
- **Locale/timezone/color-scheme** — Randomised per context

---

## ⚙️ Configuration Reference

### `.env` variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `credentials/service_account.json` | Path to SA key |
| `GOOGLE_SHEET_URL` | — | Sheet URL or ID |
| `GOOGLE_SHEET_WORKSHEET` | `Sheet1` | Tab name |
| `PROXY_SOURCE` | `file` | `file`, `env`, or `rotating` |
| `PROXY_LIST` | — | Comma-separated proxies (if source=env) |
| `ROTATING_PROXY_URL` | — | Single rotating endpoint |
| `HEADLESS` | `true` | Run browser headless |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR |

### `config.yaml` sections

- **`target`** — URL, form selectors, submit button, success indicator
- **`retry`** — max_retries, backoff_base, backoff_max
- **`delays`** — typing speed, action pauses, page load wait
- **`device_defaults`** — viewport ranges, random locale/timezone toggles
- **`screenshots`** — on_failure, on_success, directory
- **`sheet_columns`** — map internal names to your actual column headers

---

## 🔄 Status Flow

```
Pending → In Progress → Success
                      → Failed  (after max retries exhausted)
                      → Retry   (intermediate, will be retried)
```

---

## 📝 License

This project is for educational and authorized use only. Always ensure you have permission to automate form submissions on any target website.
