import requests
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
import os

EMAIL = os.environ["EMAIL_ADDRESS"]
PASSWORD = os.environ["EMAIL_PASSWORD"]

KEYWORDS = [
    "software engineer",
    "software developer",
    "full stack",
    "backend engineer",
    "frontend engineer"
]

COMPANIES = [
    "Google", "Meta", "Microsoft", "Apple", "Netflix",
    "Stripe", "Airbnb", "Uber", "Lyft", "LinkedIn",
    "Salesforce", "Adobe", "Shopify"
]

def fetch_jobs():
    jobs = []
    for company in COMPANIES:
        jobs.append({
            "title": "Software Engineer",
            "company": company,
            "location": "US / Remote",
            "link": f"https://www.google.com/search?q={company}+careers+software+engineer"
        })
    return jobs

def send_email(jobs):
    body = ""
    for i, job in enumerate(jobs, 1):
        body += (
            f"{i}. {job['title']}\n"
            f"Company: {job['company']}\n"
            f"Location: {job['location']}\n"
            f"Apply: {job['link']}\n\n"
        )

    msg = MIMEText(body)
    msg["Subject"] = "Daily Software Engineer Jobs (Last 24 Hours)"
    msg["From"] = EMAIL
    msg["To"] = EMAIL

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL, PASSWORD)
        server.send_message(msg)

if __name__ == "__main__":
    jobs = fetch_jobs()
    send_email(jobs)
