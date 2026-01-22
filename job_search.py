import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timedelta
import os
import smtplib
from email.message import EmailMessage

# --- CONFIG ---
EMAIL = os.environ["EMAIL_ADDRESS"]
PASSWORD = os.environ["EMAIL_PASSWORD"]

# List of companies
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

# Keywords
KEYWORDS = [
    "software engineer","software developer","full stack","backend engineer","frontend engineer"
]

# --- FUNCTION: Scrape Google Jobs ---
def scrape_google_jobs(company, keyword):
    """
    Scrape publicly available job postings from Google Jobs
    Returns list of dicts: title, company, location, apply_link, posted_date
    """
    results = []
    search_url = f"https://www.google.com/search?q={company}+{keyword}+jobs+site:careers.google.com OR site:greenhouse.io OR site:lever.co OR site:linkedin.com/jobs&hl=en"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        resp = requests.get(search_url, headers=headers)
        if resp.status_code != 200:
            return results
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Example scraping: find links with 'jobs' in href
        for a in soup.find_all("a", href=True):
            href = a['href']
            text = a.get_text()
            if "job" in href.lower() and text:
                # Simplified: later can filter posted date & location
                results.append({
                    "Job Title": text.strip(),
                    "Company": company,
                    "Location": "US / Remote",  # Google Jobs scraping needs enhancement for exact location
                    "Apply Link": href,
                    "Posted Date": datetime.now().strftime("%Y-%m-%d")  # placeholder
                })
    except Exception as e:
        print(f"Error scraping {company}: {e}")
    
    return results

# --- MAIN JOB SCRAPER ---
all_jobs = []

for company in COMPANIES:
    for keyword in KEYWORDS:
        jobs = scrape_google_jobs(company, keyword)
        all_jobs.extend(jobs)

# Remove duplicates
df = pd.DataFrame(all_jobs)
df.drop_duplicates(subset=['Job Title','Company','Apply Link'], inplace=True)

# Save CSV
today = datetime.now().strftime("%Y-%m-%d")
csv_file = f"jobs_{today}.csv"
df.to_csv(csv_file, index=False)

# --- SEND EMAIL ---
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
