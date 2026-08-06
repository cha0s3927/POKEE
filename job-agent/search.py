"""
岗位搜索 — Serper.dev (Google) + Bing 抓取双通道

Serper.dev 走 Google 索引，中文招聘网站覆盖好，免费 2500 次/月。
Bing 抓取作为免 Key 的降级通道。

Serper API: https://google.serper.dev/search
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse

import httpx

from config import settings

logger = logging.getLogger(__name__)

SITE_DOMAINS = {
    "zhipin": "zhipin.com",
    "shixiseng": "shixiseng.com",
    "zhilian": "zhaopin.com",
    "51job": "51job.com",
    "lagou": "lagou.com",
    "nowcoder": "nowcoder.com",
    "liepin": "liepin.com",
}

PLATFORM_NAMES = {
    "zhipin": "BOSS直聘", "shixiseng": "实习僧", "zhilian": "智联招聘",
    "51job": "前程无忧", "lagou": "拉勾", "nowcoder": "牛客网", "liepin": "猎聘",
}

SERPER_URL = "https://google.serper.dev/search"
MAX_RESULTS = 20

# 代理（Bing 降级通道用）
HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or settings.http_proxy or None


def search_jobs(query: str, site: str = "all", user_id: str = "") -> dict:
    """
    搜索岗位。

    Args:
        query: 搜索关键词，如 "Java 实习 北京"
        site: 限定平台 (shixiseng/nowcoder/zhipin/.../all)
    """
    results: list[dict] = []

    if site in PLATFORM_NAMES:
        search_query = f"site:{SITE_DOMAINS[site]} {query}"
        results = _search_serper(search_query)
        # fallback: 中文关键词搜
        if not results:
            search_query2 = f"{PLATFORM_NAMES[site]} {query}"
            results = _search_serper(search_query2)
    else:
        # 全平台搜
        results = _search_serper(query)

    # Serper 失败时降级到 Bing
    if not results:
        logger.info("Serper returned no results, falling back to Bing")
        results = _search_bing_fallback(query, site)

    formatted = _format_results(results, query)
    return {"query": query, "results": formatted, "total": len(formatted)}


def _search_serper(query: str) -> list[dict]:
    """通过 Serper.dev Google 搜索"""
    if not settings.serper_api_key:
        logger.info("Serper API key not configured")
        return []

    try:
        resp = httpx.post(
            SERPER_URL,
            json={"q": query, "gl": "cn", "hl": "zh-CN", "num": MAX_RESULTS},
            headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
            proxy=HTTP_PROXY,
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("Serper returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        organic = data.get("organic", [])

        results = []
        for r in organic:
            url = r.get("link", "")
            if any(skip in url for skip in ("youtube.com", "facebook.com", "twitter.com", "instagram.com")):
                continue
            results.append({
                "title": r.get("title", ""),
                "url": url,
                "description": r.get("snippet", "") or r.get("description", ""),
            })

        logger.info("Serper: %d results for '%s'", len(results), query[:80])
        return results

    except Exception:
        logger.exception("Serper search error")
        return []


def _search_bing_fallback(query: str, site: str = "all") -> list[dict]:
    """Bing 抓取降级通道"""
    if site in PLATFORM_NAMES:
        q = f"{PLATFORM_NAMES[site]} {query}"
    else:
        q = query

    try:
        resp = httpx.get(
            "https://cn.bing.com/search",
            params={"q": q, "count": MAX_RESULTS, "setlang": "zh-CN"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            proxy=HTTP_PROXY,
            timeout=20,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return []

        results = []
        for block in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', resp.text, re.DOTALL):
            link_m = re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.+?)</a>', block, re.DOTALL)
            if not link_m:
                continue
            url = urllib.parse.unquote(link_m.group(1))
            title = re.sub(r'<[^>]+>', '', link_m.group(2)).strip()
            if any(s in url for s in ("bing.com", "microsoft.com")):
                continue
            snippet = ""
            for p in re.findall(r'<(?:p|div)[^>]*>(.+?)</(?:p|div)>', block, re.DOTALL):
                s = re.sub(r'<[^>]+>', '', p).strip()
                if len(s) > len(snippet):
                    snippet = s
            results.append({"title": title, "url": url, "description": snippet})
            if len(results) >= MAX_RESULTS:
                break
        return results
    except Exception:
        logger.exception("Bing fallback error")
        return []


def _format_results(raw: list[dict], query: str) -> list[dict]:
    formatted = []
    for r in raw:
        url = r.get("url", "")
        formatted.append({
            "title": r.get("title", ""),
            "company": _guess_company(r.get("title", "")),
            "url": url,
            "platform": _detect_platform(url),
            "description": r.get("description", "")[:500],
        })
    return formatted


def _detect_platform(url: str) -> str:
    for key, domain in SITE_DOMAINS.items():
        if domain in url:
            return key
    for key, domain in [("ncss", "ncss.cn"), ("zhilian_xiaoyuan", "xiaoyuan.zhaopin.com")]:
        if domain in url:
            return key
    return "other"


def _guess_company(title: str) -> str:
    m = re.search(r"【(.+?)】", title)
    if m:
        return m.group(1)
    m = re.search(r"(.+?)(?:招聘|诚聘|急聘|实习|·|—|-)", title)
    if m:
        return m.group(1).strip()
    parts = title.split()
    return parts[0] if parts else title[:20]
