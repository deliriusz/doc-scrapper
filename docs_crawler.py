import os
import sys
import asyncio
import hashlib
import re
import json
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.formatters import TextFormatter
import requests
from xml.etree import ElementTree
from typing import List
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

async def process_and_store_document(url: str, markdown: str, output_dir: str):
    """ Create output file name from url and save it. """
    # whole path max length in Windows/Linux/Mac seems to be 256, 120 is a good buffer,
    # giving informative names. MD5 hash is 16 bytes (~32 characters).
    max_file_name_len = 120

    possible_doc_name_from_url = re.sub(r'https?://(www\.)?', '', url)
    possible_doc_name_from_url = re.sub(r'[^a-zA-Z0-9-]', '_', possible_doc_name_from_url)

    doc_name = ""

    if len(possible_doc_name_from_url) > max_file_name_len:
        doc_name = possible_doc_name_from_url[:max_file_name_len] + "_" + hashlib.md5(url.encode()).hexdigest()
    else:
        doc_name = possible_doc_name_from_url

    output_path = output_dir + '/' + doc_name + ".md"

    # normalize any //
    output_path = re.sub(r'/+', '/', output_path)

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(output_path, 'w') as md_file:
        md_file.write(markdown)


async def get_unique_urls_from_config(config_file: str) -> List[str]:
    """ Bundle urls from single pages, sitemap files and recursive crawling pages. """
    with open(config_file) as json_data:
        conf = json.load(json_data)

        output_urls = set()

        output_urls.update(conf["single_page"])
        output_urls.update(conf["youtube"])

        for sitemap_url in conf["sitemap"]:
            output_urls.update(get_urls_from_sitemap(sitemap_url))

        # TODO: add scraping options, possibly change return type to list of objects
        for scrap_url in conf["scrap"]:
            output_urls.add(scrap_url["url"])

        # YT don't need crawling, just yt_dlp to get transcript
        return list(output_urls)

def get_yt_transcript(url: str) -> str:
    # Heavily inspired by https://github.com/AlteredAdmin/YouTube-Transcript-Downloader
    pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    video_id = match.group(1) if match else None

    if video_id is None:
        print(f"No video id found in url {url}")
    else:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = transcript_list.find_generated_transcript(['en'])

            formatter = TextFormatter()
            transcript_text = formatter.format_transcript(transcript.fetch())

            # Remove timecodes and speaker names
            transcript_text = re.sub(r'\[\d+:\d+:\d+\]', '', transcript_text)
            transcript_text = re.sub(r'<\w+>', '', transcript_text)

            print(f"Successfully crawled: {url}")

            return transcript_text
        except Exception as e:
            print(f"Error downloading transcript: {e}")
            return ""

async def crawl_parallel(urls: List[str], output_dir: str, crawler: AsyncWebCrawler, max_concurrent: int = 5):
    """Crawl multiple URLs in parallel with a concurrency limit."""
    crawl_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS)

    try:
        # Create a semaphore to limit concurrency
        semaphore = asyncio.Semaphore(max_concurrent)

        async def process_url(url: str):
            async with semaphore:
                if "youtube.com" in url or "youtu.be" in url:
                    result = get_yt_transcript(url)
                    await process_and_store_document(url, result, output_dir)
                else:
                    result = await crawler.arun(
                        url=url,
                        config=crawl_config,
                        session_id="session1"
                    )
                    if result.success:
                        print(f"Successfully crawled: {url}")
                        # await process_and_store_document(url, result.markdown_v2.raw_markdown)
                        await process_and_store_document(url, result.markdown, output_dir)
                    else:
                        print(f"Failed: {url} - Error: {result.error_message}")

        # Process all URLs in parallel with limited concurrency
        await asyncio.gather(*[process_url(url) for url in urls])
    finally:
        await crawler.close()

def get_urls_from_sitemap(url: str) -> List[str]:
    """Get URLs from docs sitemap."""
    try:
        response = requests.get(url)
        response.raise_for_status()

        # Parse the XML
        root = ElementTree.fromstring(response.content)

        # Extract all URLs from the sitemap
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        urls = [loc.text for loc in root.findall('.//ns:loc', namespace)]

        return urls
    except Exception as e:
        print(f"Error fetching sitemap: {e}")
        return []


async def main(config_file: str, output_dir: str):
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        # performance improvements
        extra_args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
    )

    # Create the crawler instance
    crawler = AsyncWebCrawler(config=browser_config)
    await crawler.start()

    urls = await get_unique_urls_from_config(config_file)

    await crawl_parallel(urls, output_dir, crawler, 10)

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage {sys.argv[0]} <json-config-file-path> <output-dir>")
        exit(1)

    config_file = sys.argv[1]
    output_dir = sys.argv[2]
    asyncio.run(main(config_file, output_dir))