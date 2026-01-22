import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timedelta
import dateparser
import os
import smtplib
from email.message import EmailMessage
import time

# ---------------- CONFIG -----------------
EMAIL = os.environ["EMAIL_ADDRESS"]
PASSWORD = os.environ["EMAIL_PASSWORD"]

# Keywords and Companies
KEYWORDS = ["software engineer", "software developer", "full stack", "backend engineer", "frontend engineer"]

COMPANIES = [
    "paypal","Strategy Inc","nyiso","Solarity","15Five","Voxel","Pearson’s",
    "Major League Baseball","Conduit","Inspira Financial","NewsBreak","Figure",
    "Janus Henderson","Relanto","Euna","cfs","WealthCounsel","Truveta","Genentech",
    "NOV Digital Services","Radiant","GPTZero","Bain & Company!","Pure","Homebase",
    "Yardi","STERIS","genesys system integrator","cisco","Checkr","Berkshire Hathaway",
    "BlackRock","dmainc","blueyonder","TrueMeter","Twist Bioscience","Intuit careers",
    "Flextrade","EnergyHub","thetradedesk","block","pwc","sun","Acorns","PlayStation",
    "GumGum","Expedia Group","perk","Docusign","CoreWeave","snap","Trade Desk",
    "Arrowstreet","OPENLANE","OnCorps AI","Lightspark","asurion","Aritzia","oracle",
    "SLATE","Microsoft","Rippling","Axon","lululemon","Blue Origin","Google","Meta",
    "Apple","Salesforce","Stripe","Coinbase","Intuit","Snowflake","Databricks","Remitly",
    "Zillow","Redfin","OfferUp","Outreach.io","Highspot","Tanium","Nintendo of America",
    "Adobe","SAP","NVIDIA","DocuSign","Unity Technologies","Epic Games","Electronic Arts",
    "Workday","Snap Inc.","Twitter / X","SAP Concur","Wayfair"
]

# ---------------- UTILITIES -----------------
def location_us_or_remote(location: str):
    loc = location.lower()
    return "united states" in loc or "remote" in loc

def posted_last_24h(posted_date):
    now = datetime.now()
    return (now - posted_date).total_seconds() <= 86400  # 24h

def parse_posted_date(text):
    try:
        return dateparser.parse(text)
    except:
        return datetime.now()  # fallback

# ---------------- SCRAPERS -----------------

# Lever jobs
def scrape_lever(company):
    jobs = []
    url = f"https://jobs.lever.co/{company}"
    try:
        resp = requests.get(url)
        if resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        postings = soup.find_all("div", class_="posting")
        for post in postings:
            title = post.find("h5").text.strip()
            location_tag = post.find("span", class_="sort-by-location")
            location = location_tag.text.strip() if location_tag else "Remote"
            apply_link = post.find("a")["href"]
            time_tag = post.find("time")
            posted_date = parse_posted_date(time_tag["datetime"] if time_tag else "")
            if location_us_or_remote(location) and posted_last_24h(posted_date):
                jobs.append({
                    "Job Title": title,
                    "Company": company,
                    "Location": location,
                    "Apply Link": apply_link,
                    "Posted Date": posted_date.strftime("%Y-%m-%d")
                })
    except Exception as e:
        print(f"[Lever] Error scraping {company}: {e}")
    return jobs

# Greenhouse jobs
def scrape_greenhouse(company):
    jobs = []
    url = f"https://boards.greenhouse.io/{company}"
    try:
        resp = requests.get(url)
        if resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        openings = soup.find_all("div", class_="opening")
        for post in openings:
            title = post.find("a").text.strip()
            link = post.find("a")["href"]
            location_tag = post.find("span", class_="location")
            location = location_tag.text.strip() if location_tag else "Remote"
            # Greenhouse does not provide exact posted date publicly
            posted_date = datetime.now()  # assume today
            if location_us_or_remote(location):
                jobs.append({
                    "Job Title": title,
                    "Company": company,
                    "Location": location,
                    "Apply Link": link,
                    "Posted Date": posted_date.strftime("%Y-%m-%d")
                })
    except Exception as e:
        print(f"[Greenhouse] Error scraping {company}: {e}")
    return jobs

# Workday jobs (simplified example)
def scrape_workday(company):
    jobs = []
    # Workday URLs vary widely; placeholder
    return jobs

# ---------------- GOOGLE JOB SEARCH -----------------
def scrape_google_jobs(company, keyword):
    jobs = []
    search_url = f"https://www.google.com/search?q={company}+{keyword}+jobs+site:lever.co OR site:greenhouse.io OR site:angel.co OR site:stackoverflow.com/jobs OR site:indeed.com&hl=en"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(search_url, headers=headers)
        if resp.status_code != 200:
            return jobs
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a['href']
            text = a.get_text()
            if "job" in href.lower() and text:
                jobs.append({
                    "Job Title": text.strip(),
                    "Company": company,
                    "Location": "US / Remote",  # approximate
                    "Apply Link": href,
                    "Posted Date": datetime.now().strftime("%Y-%m-%d")
                })
    except Exception as e:
        print(f"[Google Jobs] Error for {company} {keyword}: {e}")
    return jobs

# ---------------- MAIN -----------------
all_jobs = []

for company in COMPANIES:
    all_jobs.extend(scrape_lever(company))
    all_jobs.extend(scrape_greenhouse(company))
    # Uncomment if Workday URLs are known
    # all_jobs.extend(scrape_workday(company))
    for keyword in KEYWORDS:
        all_jobs.extend(scrape_google_jobs(company, keyword))
    time.sleep(1)  # polite scraping

# Deduplicate
df = pd.DataFrame(all_jobs)
df.drop_duplicates(subset=['Job Title','Company','Apply Link'], inplace=True)

# Save CSV
today = datetime.now().strftime("%Y-%m-%d")
csv_file = f"jobs_{today}.csv"
df.to_csv(csv_file, index=False)

# ---------------- EMAIL -----------------
msg = EmailMessage()
msg['Subject'] = f"Daily Software Jobs ({today})"
msg['From'] = EMAIL
msg['To'] = EMAIL
msg.set_content("Please find attached the latest software engineer jobs (U.S. + Remote).")

with open(csv_file, "rb") as f:
    msg.add_attachment(f.read(), maintype="application", subtype="csv", filename=csv_file)

with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
    server.login(EMAIL, PASSWORD)
    server.send_message(msg)

print(f"Email sent with {len(df)} jobs!")
