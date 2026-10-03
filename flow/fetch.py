from __future__ import annotations

import requests
import json
import os
from typing import List, Optional
from prefect import flow, task
import sys


JINA_API_KEY = os.environ.get('JINA_API_KEY');

#  returns json
#  might track the token usage.  this key has 500 req per min, or 100k tokens per minute
#
@task(retries=3, retry_delay_seconds=2)
def fetch_html(url: str) -> str:
    endpoint = "https://r.jina.ai/"
    print(f"Authorization Bearer {JINA_API_KEY}");
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {JINA_API_KEY}",  # Replace with your actual JINA_API_KEY
        "Content-Type": "application/json",
        "X-Engine": "browser",
        "X-Retain-Images": "none",
        "X-Return-Format": "markdown",
        "X-With-Links-Summary": "true"
    }
    data = {
        "url": url
    }

    print(f"Fetching {url} …")
    response = requests.post(endpoint, headers=headers, data=json.dumps(data))
    response.raise_for_status()

    return(response.text)

@task
def parse(html: str) -> str:
    return html

@flow(log_prints=True)
def scrape(urls: Optional[List[str]] = None) -> None:
    """Scrape and print article content from URLs.

    A regular Python function that composes our tasks together.
    Prefect adds logging and dependency management automatically."""

    if urls:
        for url in urls:
            content = fetch_html(url)
    #        print(content["data"]["title"])
    #        print(content["data"]["description"])
    #        print(content["data"]["content"])
    #        print(content["data"]["links"])
            print(content if content else "No article content found.")

if __name__ == "__main__":
    urls = sys.argv[1:]
    scrape(urls=urls)
