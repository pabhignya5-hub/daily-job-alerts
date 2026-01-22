#!/usr/bin/env python3
"""
job_alerts.py

One-shot script (no internal scheduler). It:
- Loads a curated list of companies from companies.json (you provide exact career slugs or URLs).
- Scrapes Lever (via Lever API) and Greenhouse (HTML) and generic URLs where possible.
- Filters for jobs posted/updated in the last 24 hours.
- Produces a CSV and emails the list (plain text + attachment).

Usage:
- Create a companies.json next to this script. Format examples are below.
- Set environment variables: EMAIL_ADDRESS, EMAIL_PASSWORD, RECIPIENT (optional).
- Run: python job_alerts.py

Sample companies.json (required; place next to script):
{
  "Stripe": { "platform": "lever", "value": "stripe" },
  "Coinbase": { "platform": "lever", "value": "coinbase" },
  "ExampleCoGreenhouse": { "platform": "greenhouse", "value": "exampleco" },
  "CustomDirect": { "platform": "url", "value": "https://example.com/careers" }
}

Notes:
- This intentionally avoids any search APIs. The file is curated: you control which companies are checked.
- Only jobs with a detectable posted/updated datetime within the last 24 hours are included.
"""

import os
import sys
import json
import csv
import io
import time
import requests
from datetime import datetime, timedelta
from email.message import EmailMessage
import smtplib
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import dateparser

# CONFIG
EMAIL = os.environ.get("EMAIL_ADDRESS")
PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT = os.environ.get("EMAIL_ADDRESS", EMAIL)

if not EMAIL or not PASSWORD:
    sys.exit("Missing EMAIL_ADDRESS or EMAIL_PASSWORD environment variables. Set them before running.")

COMPANIES_FILE = os.environ.get("COMPANIES_FILE", "companies.json")
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; JobAlerts/1.0)"}
TIME_WINDOW = timedelta(days=1)  # last 24 hours

# ---------------- Utilities ----------------
def parse_date(text):
    if not text:
        return None
    try:
        dt = dateparser.parse(text, settings={"RETURN_AS_TIMEZONE_AWARE": False})
        return dt
    except Exception:
        return None

def is_recent(dt):
    if not dt:
        return False
    return (datetime.now() - dt) <= TIME_WINDOW

def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

# ---------------- Lever (reliable) ----------------
def scrape_lever(slug, display_name=None):
    """
    Uses Lever's public postings API: https://api.lever.co/v0/postings/{company}?mode=json
    Returns list of dicts with Job Title, Company, Location, Apply Link, Posted Date (datetime)
    """
    jobs = []
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(api, headers=USER_AGENT, timeout=12)
        if r.status_code != 200:
            return jobs
        data = r.json()
        for p in data:
            # Fields vary; attempt common keys
            title = p.get("text") or p.get("title") or p.get("position") or ""
            apply_link = p.get("hostedUrl") or p.get("applyUrl") or p.get("url") or ""
            # Lever postings often have 'categories' with 'location'
            categories = p.get("categories") or {}
            location = categories.get("location") or categories.get("office") or "Remote"
            # date fields: 'createdAt', 'date', 'postedAt'
            raw_date = p.get("createdAt") or p.get("date") or p.get("postedAt") or p.get("updatedAt")
            posted_dt = parse_date(raw_date) or None
            if posted_dt and is_recent(posted_dt):
                jobs.append({
                    "Job Title": title.strip(),
                    "Company": display_name or slug,
                    "Location": location.strip() if location else "Remote",
                    "Apply Link": apply_link or f"https://jobs.lever.co/{slug}",
                    "Posted Date": posted_dt.isoformat()
                })
    except Exception:
        pass
    return jobs

# ---------------- Greenhouse (HTML) ----------------
def scrape_greenhouse(slug, display_name=None):
    """
    Scrape Board page and each job page for date. Requires the job page to have a <time datetime="..."> or 'posted' text.
    """
    jobs = []
    board_url = f"https://boards.greenhouse.io/{slug}"
    try:
        r = requests.get(board_url, headers=USER_AGENT, timeout=12)
        if r.status_code != 200:
            return jobs
        soup = BeautifulSoup(r.text, "html.parser")
        openings = soup.find_all("div", class_="opening")
        for o in openings:
            a = o.find("a")
            if not a:
                continue
            title = a.get_text(strip=True)
            href = a.get("href")
            if not href:
                continue
            job_url = urljoin(board_url, href)
            # Fetch job page to find a time tag or 'posted' text
            try:
                jr = requests.get(job_url, headers=USER_AGENT, timeout=12)
                if jr.status_code != 200:
                    continue
                jsoup = BeautifulSoup(jr.text, "html.parser")
                # Look for <time datetime="...">
                time_tag = jsoup.find("time")
                dt = None
                if time_tag and time_tag.get("datetime"):
                    dt = parse_date(time_tag["datetime"])
                if not dt:
                    # Look for text like "Posted" or "Posted on"
                    txt = jsoup.get_text(" ", strip=True)
                    import re
                    m = re.search(r"Posted(?: on)?[:\s]+([A-Za-z0-9, \-:/]+)", txt, re.IGNORECASE)
                    if m:
                        dt = parse_date(m.group(1))
                if dt and is_recent(dt):
                    # Location may be present in job page
                    loc_tag = jsoup.find("span", class_="location") or jsoup.find("span", class_="posting-location")
                    loc = loc_tag.get_text(strip=True) if loc_tag else "Remote"
                    jobs.append({
                        "Job Title": title,
                        "Company": display_name or slug,
                        "Location": loc,
                        "Apply Link": job_url,
                        "Posted Date": dt.isoformat()
                    })
            except Exception:
                continue
    except Exception:
        pass
    return jobs

# ---------------- Generic URL-based scraping (best-effort) ----------------
def scrape_generic(url, display_name=None):
    """
    Best-effort: fetch page, find job links, and inspect job pages for a datetime.
    Only returns roles with an explicit date within last 24 hours.
    """
    jobs = []
    try:
        r = requests.get(url, headers=USER_AGENT, timeout=12)
        if r.status_code != 200:
            return jobs
        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.find_all("a", href=True)
        seen_links = set()
        for a in anchors:
            href = a["href"]
            if any(k in href.lower() for k in ("/job", "/jobs/", "/careers/", "/apply")):
                job_url = urljoin(url, href)
                if job_url in seen_links:
                    continue
                seen_links.add(job_url)
                # fetch job page and try to find a date
                try:
                    jr = requests.get(job_url, headers=USER_AGENT, timeout=12)
                    if jr.status_code != 200:
                        continue
                    jsoup = BeautifulSoup(jr.text, "html.parser")
                    # look for time tag or meta property
                    dt = None
                    time_tag = jsoup.find("time")
                    if time_tag and time_tag.get("datetime"):
                        dt = parse_date(time_tag["datetime"])
                    if not dt:
                        meta_dt = jsoup.find("meta", {"property": "article:published_time"}) or jsoup.find("meta", {"name": "date"})
                        if meta_dt and meta_dt.get("content"):
                            dt = parse_date(meta_dt["content"])
                    if not dt:
                        txt = jsoup.get_text(" ", strip=True)
                        import re
                        m = re.search(r"Posted(?: on)?[:\s]+([A-Za-z0-9, \-:/]+)", txt, re.IGNORECASE)
                        if m:
                            dt = parse_date(m.group(1))
                    if dt and is_recent(dt):
                        title = jsoup.title.string.strip() if jsoup.title and jsoup.title.string else (a.get_text(strip=True) or "Job")
                        # try to find location
                        loc = "Remote"
                        loc_tag = jsoup.find(lambda t: t.name in ("span", "p", "div") and "location" in (t.get("class") or []) )
                        if loc_tag:
                            loc = loc_tag.get_text(strip=True)
                        jobs.append({
                            "Job Title": title,
                            "Company": display_name or domain_of(url),
                            "Location": loc,
                            "Apply Link": job_url,
                            "Posted Date": dt.isoformat()
                        })
                except Exception:
                    continue
    except Exception:
        pass
    return jobs

# ---------------- Main collector ----------------
def collect_jobs(companies_map):
    all_jobs = []
    seen = set()
    for display_name, entry in companies_map.items():
        platform = entry.get("platform")
        value = entry.get("value")
        if not platform or not value:
            continue
        try:
            if platform == "lever":
                jobs = scrape_lever(value, display_name)
            elif platform == "greenhouse":
                jobs = scrape_greenhouse(value, display_name)
            elif platform == "url":
                jobs = scrape_generic(value, display_name)
            else:
                jobs = []
        except Exception:
            jobs = []
        for j in jobs:
            key = (j["Job Title"], j["Company"], j["Apply Link"])
            if key in seen:
                continue
            seen.add(key)
            all_jobs.append(j)
    return all_jobs

# ---------------- Email ----------------
def send_email(jobs):
    today = datetime.now().strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["Subject"] = f"Daily Curated Jobs ({today})"
    msg["From"] = EMAIL
    msg["To"] = RECIPIENT

    if not jobs:
        body = "No new curated jobs found in the last 24 hours."
        msg.set_content(body)
    else:
        lines = []
        for j in jobs:
            lines.append(f"{j['Job Title']} | {j['Company']} | {j['Location']} | {j['Apply Link']}")
        body = "Curated jobs posted in the last 24 hours:\n\n" + "\n".join(lines)
        msg.set_content(body)

        # CSV attachment
        csv_buf = io.StringIO()
        writer = csv.DictWriter(csv_buf, fieldnames=["Job Title", "Company", "Location", "Apply Link", "Posted Date"])
        writer.writeheader()
        for j in jobs:
            writer.writerow(j)
        msg.add_attachment(csv_buf.getvalue().encode("utf-8"), maintype="text", subtype="csv", filename=f"jobs_{today}.csv")

    # send
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL, PASSWORD)
        s.send_message(msg)

    print(f"Email sent to {RECIPIENT} with {len(jobs)} jobs.")

# ---------------- Entrypoint ----------------
def main():
    # Load companies.json (curated list)
    if not os.path.exists(COMPANIES_FILE):
        sample = {
            "Stripe": {"platform": "lever", "value": "stripe"},
            "Coinbase": {"platform": "lever", "value": "coinbase"},
            "YourGreenhouseCo": {"platform": "greenhouse", "value": "yourcompanyslug"},
            "YourCareersPage": {"platform": "url", "value": "https://yourcompany.com/careers"}
        }
        with open(COMPANIES_FILE, "w", encoding="utf-8") as f:
            json.dump(sample, f, indent=2)
        sys.exit(f"No {COMPANIES_FILE} found. A sample was created. Edit it with your curated companies and re-run.")

    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        companies_map = json.load(f)

    print(f"[{datetime.now().isoformat()}] Collecting jobs for {len(companies_map)} curated companies...")
    jobs = collect_jobs(companies_map)
    print(f"Found {len(jobs)} recent jobs. Sending email...")
    send_email(jobs)

if __name__ == "__main__":
    main()
