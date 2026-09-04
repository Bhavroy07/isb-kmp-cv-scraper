# ISB CAS-KMP resume downloader

Python scripts to search the [ISB CAS-KMP](https://apps.isb.edu/CAS-KMP/) portal and download alumni CVs. You need **your own ISB login**. This does not bypass access control.

You pick **class years** and a **post-ISB company**. The scraper keeps only people whose **Post ISB Designation** or **Post ISB Function** contains `product` (for example Product Manager, Product Management). Files land in:

```text
cvs/<year>/<company>/
```

Valid `--company` names are in [`post_isb_companies.txt`](post_isb_companies.txt) (one name per line). Copy a line **exactly**, including punctuation.

---

## Clone and setup

```bash
git clone https://github.com/Bhavroy07/isb-kmp-cv-scraper.git
cd isb-kmp-cv-scraper

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Later sessions:

```bash
cd isb-kmp-cv-scraper
source .venv/bin/activate
```

---

## Log in once

The portal needs your ISB account. This opens Chromium and stores the session in `~/.isb_kmp_profile` on **your** machine (not in git).

```bash
python isb_kmp_scraper.py login
```

1. Sign in in the browser.
2. Wait until you can see the student **search form**.
3. Return to Terminal and press **Enter**.

You should see `Session saved.` If a later run says you are not logged in, run `login` again.

---

## Download CVs

```bash
python isb_kmp_scraper.py run --years 2023 2024 2025 2026 --company "Razorpay Software Private Limited"
```

- `--years` is the **Class** field (one year or several).
- `--company` is **Post ISB Company**. Quote names that contain spaces.

Examples:

```bash
python isb_kmp_scraper.py run --years 2025 --company "Flipkart India Pvt. Ltd."
python isb_kmp_scraper.py run --years 2024 2025 2026 --company "Amazon"
```

Several companies:

```bash
python isb_kmp_scraper.py run --years 2025 \
  --company "Razorpay Software Private Limited" \
  --company "Flipkart India Pvt. Ltd."
```

Match every dropdown option that contains a word (this can fire many searches):

```bash
python isb_kmp_scraper.py run --years 2025 --company "~Razorpay"
```

`--limit 5` downloads at most five CVs (good for a test).

---

## What you get

```text
cvs/
  2025/
    Razorpay_Software_Private_Limited/
      <cv>.pdf
      _manifest.json
```

`_manifest.json` records finished downloads. Re-running the same years and company skips those files.

Rows with no **View** resume link, or with no `product` in designation/function, are skipped on purpose.

**Do not commit `cvs/`.** Alumni resumes stay on your computer.

---

## Refresh the company list

If the portal adds companies:

```bash
python list_post_isb_companies.py
```

That overwrites `post_isb_companies.txt` and `post_isb_companies.json`.

---

## Project files

| File | Role |
| --- | --- |
| `isb_kmp_scraper.py` | Login, search, filter, download |
| `list_post_isb_companies.py` | Dump the Post-ISB company dropdown |
| `post_isb_companies.txt` | Names to pass to `--company` |
| `requirements.txt` | Python dependency (`playwright`) |

---

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `pass --company NAME` | Add `--company` using a line from `post_isb_companies.txt`. |
| `'…' is not valid for post_isb_company` | Name does not match the dropdown. Search the `.txt` file and paste the exact line. |
| `Not logged in` | Run `python isb_kmp_scraper.py login` again. |
| `no matching resumes` | That year/company had no product rows, or no View link. Read the `keep` / `skip` lines in the terminal. |

---

## Notes

This talks to an authenticated campus system. Use it only with credentials you are allowed to use, and keep downloaded CVs private.
