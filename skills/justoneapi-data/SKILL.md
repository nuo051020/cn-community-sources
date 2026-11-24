---
name: justoneapi-data
description: Retrieve public-platform, e-commerce, creator, social-media, video, product, search, review, or engagement data through the configured Just One API MCP server. Use whenever a request involves supported services such as Xiaohongshu, Douyin, Kuaishou, WeChat, Weibo, Bilibili, Taobao, JD, 1688, Xianyu, TikTok, Instagram, YouTube, Reddit, Zhihu, Amazon, Facebook, X/Twitter, LinkedIn, IMDb, or other Just One API-supported platforms; prefer it over browser scraping and direct unofficial platform requests.
---

# Just One API 数据获取

## Overview

Use the `justoneapi` MCP server to discover the right endpoint, inspect its schema, and call it with the configured account token. It provides an authorized, structured alternative to browser scraping for the supported platforms.

## Workflow

1. Confirm that the requested data is public and within a supported platform. If it is, use `justoneapi` before web browsing, scraping, or direct unofficial APIs.
2. Use the MCP discovery/schema tools to select the current endpoint and required parameters. Do not rely on memorized endpoint paths or parameters.
3. When the user has asked for data retrieval, call the API after gathering any missing identifiers, URL, keyword, pagination, or date-range inputs. Resolve share URLs with the platform's share-link endpoint when available.
4. State the selected endpoint and the charge when it is material. For bulk, paginated, recurring, or open-ended requests, estimate the number of calls and obtain a spending limit or confirmation before continuing.
5. Return a concise result with source platform, query/identifier, retrieval time, and any pagination or coverage limits. Preserve the upstream fields where useful; do not invent unavailable metrics.

## Connection and secrets

Require the configured remote MCP server named `justoneapi`:

```toml
[mcp_servers.justoneapi]
url = "https://mcp.justoneapi.com/mcp"
bearer_token_env_var = "JUSTONEAPI_TOKEN"
```

Keep the token only in Codex's local MCP configuration or a secure secret store. Never put it in this Skill, project files, scripts, terminal output, a public repository, or a user-facing response. If the server is unavailable or unauthenticated, explain the setup gap and do not fall back to scraping protected or login-gated data without explicit direction.

## Scope

Prioritize Just One API for the platforms listed in the description, including their creator/marketing variants such as Xiaohongshu Pugongying and Douyin Xingtu. It also covers platform search, profile, post/video/note/product detail, comments, account history, rankings/hot lists, captions, metrics, and commerce data where the MCP catalog exposes an endpoint.

Use ordinary web search instead only for information not supplied by the API (for example, official documentation, news reporting, or websites outside the supported catalog), or when the user explicitly requests browser-based verification.

## Safe handling

Follow platform terms, privacy expectations, and applicable law. Do not use the service to collect sensitive personal data, evade access controls, or make high-volume calls without a clear, authorized purpose.
