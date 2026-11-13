#!/usr/bin/env python3
"""
Viral Content Sync — Supabase edition
--------------------------------------
Scrapes one tracker's competitors via Apify (profiles for follower counts,
then recent posts), keeps the standout posts, and UPSERTS into the Supabase
`posts` table.

A post is kept when it clears a small noise floor AND passes ANY of three
tests (OR, not AND -- the old AND rule starved small and large accounts
alike):
  1. engagement rate  >= MIN_ER
  2. comments         >= MIN_COMMENTS
  3. engagements      >= OUTLIER_X x that account's OWN median engagements
     (median taken over the posts scraped this run -- a real baseline, so a
     5k-follower account is judged against itself, not against a 300k one)
On top of that, every account always contributes its TOP_N_PER_COMP best
posts, so a quiet week still fills the dashboard instead of showing zero.

Widening the filter costs NOTHING extra on Apify: the scrape already pulls
every post, the filter only decides what gets stored.

Upsert notes: existing posts get their metrics/video URL refreshed (fresh
CDN links let previously skipped videos transcribe); status / transcript /
ai_breakdown are never in the payload, so worked rows are never clobbered.
engagement_rate and viral_score are generated columns — never written.

Env (required): TRACKER ("IG", "FB", "TT" TikTok, "LI" LinkedIn,
                "XHS" 小红书 or "AI"), APIFY_TOKEN, SUPABASE_URL,
                SUPABASE_SERVICE_ROLE_KEY
Env (tuning, all optional -- set as GitHub Actions *Variables*):
     MIN_ER (0.02)  MIN_COMMENTS (15)  MIN_ENGAGE (20)  OUTLIER_X (1.5)
     TOP_N_PER_COMP (3)  MAX_PER_COMP (15)  DAYS_BACK (60)
     RESULTS_LIMIT (50)  MAX_ROWS (2500)
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TRACKER = (os.environ.get("TRACKER") or "AI").strip().upper()
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")

POLL_INTERVAL = 30
POLL_TIMEOUT = 900
PROTECTED = ("拍摄中", "已处理")


def _envf(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _envi(name, default):
    try:
        return int(float(os.environ.get(name) or default))
    except ValueError:
        return int(default)


# --- tuning knobs (GitHub Actions Variables; defaults are the loose set) ---
MIN_ER         = _envf("MIN_ER", 0.02)        # 2% engagement rate
MIN_COMMENTS   = _envi("MIN_COMMENTS", 15)    # absolute comment count
MIN_ENGAGE     = _envi("MIN_ENGAGE", 20)      # noise floor: likes+comments
OUTLIER_X      = _envf("OUTLIER_X", 1.5)      # x the account's own median
TOP_N_PER_COMP = _envi("TOP_N_PER_COMP", 3)   # always keep each account's best N
MAX_PER_COMP   = _envi("MAX_PER_COMP", 15)    # cap per account, so one big
                                              # account cannot flood the board
DAYS_BACK      = _envi("DAYS_BACK", 60)       # lookback window (free volume)
RESULTS_LIMIT  = _envi("RESULTS_LIMIT", 50)   # posts scraped per account (costs $)
MAX_ROWS       = _envi("MAX_ROWS", 2500)      # dashboard payload cap


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _request(url, method="GET", data=None, headers=None, timeout=60):
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return raw
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code} for {method} {url[:100]}")
        print(f"  Response: {exc.read().decode('utf-8', errors='replace')[:400]}")
        raise


def sb(path, method="GET", data=None, prefer=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key}  # new-style keys are not JWTs: apikey header only
    if prefer:
        headers["Prefer"] = prefer
    return _request(f"{SUPABASE_URL}/rest/v1/{path}", method, data, headers, 60)


# ---------------------------------------------------------------------------
# Competitors (self-service list in Supabase)
# ---------------------------------------------------------------------------


def fb_handle(n):
    """'https://www.facebook.com/QashierMY/' / '@QashierMY' -> 'qashiermy';
    'facebook.com/profile.php?id=123' -> '123'."""
    n = (n or "").strip()
    if "facebook.com" in n or "fb.com" in n:
        u = urllib.parse.urlparse(n if "://" in n else "https://" + n)
        parts = [p for p in u.path.split("/") if p]
        if parts and parts[0] == "profile.php":
            return (urllib.parse.parse_qs(u.query).get("id") or [""])[0]
        if parts and parts[0] in ("pg", "people"):
            parts = parts[1:]
        n = parts[0] if parts else ""
    return n.lstrip("@").strip().lower()


def fb_page_url(handle):
    return f"https://www.facebook.com/{handle}"


def tt_handle(n):
    """'https://www.tiktok.com/@qashiermy?lang=en' / '@qashiermy' -> 'qashiermy'."""
    n = (n or "").strip()
    if "tiktok.com" in n:
        u = urllib.parse.urlparse(n if "://" in n else "https://" + n)
        parts = [p for p in u.path.split("/") if p]
        n = parts[0] if parts and parts[0].startswith("@") else ""
    return n.lstrip("@").strip().lower()


def li_handle(n):
    """'https://www.linkedin.com/company/qashier/posts/' -> 'qashier'."""
    n = (n or "").strip()
    if "linkedin.com" in n:
        u = urllib.parse.urlparse(n if "://" in n else "https://" + n)
        parts = [p for p in u.path.split("/") if p]
        n = (parts[1] if len(parts) > 1 and parts[0] in ("company", "showcase", "school")
             else "")
    return n.lstrip("@").strip().lower()


def li_page_url(handle):
    return f"https://www.linkedin.com/company/{handle}/"


def xhs_handle(n):
    """'https://www.xiaohongshu.com/user/profile/67a1eb79...?xsec_token=..' -> user id."""
    n = (n or "").strip()
    if "xiaohongshu.com" in n:
        parts = [p for p in urllib.parse.urlparse(
            n if "://" in n else "https://" + n).path.split("/") if p]
        n = parts[2] if len(parts) > 2 and parts[:2] == ["user", "profile"] else ""
    return n.strip().lower()


def xhs_profile_url(handle):
    return f"https://www.xiaohongshu.com/user/profile/{handle}"


HANDLERS = {"FB": fb_handle, "TT": tt_handle, "LI": li_handle, "XHS": xhs_handle}


def fetch_competitors():
    rows = sb(f"competitors?select=username&tracker=eq.{TRACKER}&active=is.true")
    names = []
    for r in rows:
        n = (r.get("username") or "").strip()
        if TRACKER in HANDLERS:
            h = HANDLERS[TRACKER](n)
            if TRACKER == "XHS" and h and not re.fullmatch(r"[0-9a-f]{24}", h):
                print(f"  WARNING: skipping '{n[:60]}' — XHS needs the profile link "
                      "xiaohongshu.com/user/profile/<24 位 ID>, not a short xhslink")
                continue
            if h:
                names.append(h)
            continue
        if "instagram.com" in n:
            parts = [p for p in urllib.parse.urlparse(n).path.split("/") if p]
            if not parts or parts[0] in ("p", "reel", "reels", "stories", "tv"):
                print(f"  WARNING: skipping non-profile link: {n[:60]}")
                continue
            n = parts[0]
        n = n.lstrip("@").strip()
        if n:
            names.append(n)
    return names


def suggest_competitors(profile_items):
    """Rolling shortlist: replace last week's un-adopted suggestions with
    this week's most frequent unseen related profiles."""
    existing = {(r.get("username") or "").lower().lstrip("@")
                for r in sb("competitors?select=username")}
    counts = {}
    for prof in profile_items:
        for rp in (prof.get("relatedProfiles") or []):
            u = (rp.get("username") or "").strip().lower()
            if u and u not in existing:
                counts[u] = counts.get(u, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:8]
    if not top:
        print("[Suggest] no new related profiles this run")
        return
    # clear old un-adopted suggestions for this tracker
    pat = urllib.parse.quote("系统推荐*")
    sb(f"competitors?tracker=eq.{TRACKER}&active=is.false&notes=like.{pat}",
       method="DELETE", prefer="return=minimal")
    today = datetime.now(timezone.utc).date().isoformat()
    sb("competitors", method="POST", prefer="return=minimal", data=[
        {"username": u, "tracker": TRACKER, "active": False,
         "notes": f"系统推荐 {today}(相关度 {n})— 觉得好就把 active 打勾"}
        for u, n in top
    ])
    print(f"[Suggest] refreshed shortlist: {', '.join(u for u, _ in top)}")


# ---------------------------------------------------------------------------
# Apify
# ---------------------------------------------------------------------------


def run_actor(token, actor_id, actor_input, label):
    print(f"[{label}] {actor_id}...")
    result = _request(f"https://api.apify.com/v2/acts/{actor_id}/runs?token={token}",
                      "POST", actor_input, timeout=120)
    run_id = result["data"]["id"]
    print(f"  run {run_id}")
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        st = _request(f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}",
                      timeout=30)["data"]
        print(f"  {st['status']}")
        if st["status"] == "SUCCEEDED":
            items = _request(
                f"https://api.apify.com/v2/datasets/{st['defaultDatasetId']}/items"
                f"?token={token}&clean=true", timeout=120)
            return items if isinstance(items, list) else []
        if st["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run ended: {st['status']}")
    # abort so a run nobody will fetch stops billing
    try:
        _request(f"https://api.apify.com/v2/actor-runs/{run_id}/abort?token={token}",
                 "POST", timeout=30)
        print(f"  aborted {run_id} to stop billing")
    except Exception as exc:
        print(f"  WARNING: could not abort {run_id}: {exc}")
    raise TimeoutError(f"run {run_id} exceeded {POLL_TIMEOUT}s — fewer competitors "
                       f"or a higher POLL_TIMEOUT needed")


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------


def parse_date(v):
    if isinstance(v, (int, float)):
        if v > 1e11:  # milliseconds (LinkedIn)
            v = v / 1000
        return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass
    return None


def post_type(p):
    m = {"Image": "Image", "Video": "Video", "Sidecar": "Carousel", "Reel": "Reel"}
    return m.get((p.get("type") or "").strip(),
                 "Video" if p.get("isVideo") else "Image")


def ig_extract(p):
    """Apify instagram-post-scraper item -> common post fields (None = broken)."""
    if p.get("error") or not p.get("shortCode"):
        return None
    pt = post_type(p)
    tags = p.get("hashtags") or []
    return {
        "competitor": (p.get("ownerUsername") or (p.get("owner") or {}).get("username")
                       or "").lower().strip(),
        "post_id": p["shortCode"],
        "caption": p.get("caption") or "",
        "post_type": pt,
        "likes": int(p.get("likesCount") or p.get("likes") or 0),
        "comments": int(p.get("commentsCount") or p.get("comments") or 0),
        "post_date": parse_date(p.get("timestamp")),
        "post_url": f"https://www.instagram.com/p/{p['shortCode']}/",
        "thumbnail_url": p.get("displayUrl") or p.get("imageUrl") or None,
        "video_url": p.get("videoUrl") or None,
        "hashtags": ", ".join(tags) if isinstance(tags, list) else str(tags),
        "is_video": bool(p.get("isVideo") or pt in ("Video", "Reel")),
    }


def fb_extract(p):
    """Apify facebook-posts-scraper item -> common post fields (None = broken).

    FB has no direct video file link in the scrape, so for videos video_url is
    the post page itself; transcribe_videos.py fetches the audio with yt-dlp.
    """
    pid = str(p.get("postId") or p.get("id") or "").strip()
    if p.get("error") or not pid:
        return None
    url = p.get("url") or p.get("topLevelUrl") or f"https://www.facebook.com/{pid}"
    media = [m for m in (p.get("media") or []) if isinstance(m, dict)]
    m0 = media[0] if media else {}
    is_video = (bool(p.get("isVideo")) or (m0.get("__typename") or "").lower() == "video"
                or any(s in url for s in ("/videos/", "/reel/", "/watch")))
    thumb = (m0.get("thumbnail") or (m0.get("photo_image") or {}).get("uri")
             or (m0.get("image") or {}).get("uri") or None)
    text = p.get("text") or p.get("message") or ""
    return {
        # match on the page URL WE asked for, so it lines up with the competitors table
        "competitor": fb_handle(p.get("inputUrl") or p.get("facebookUrl")
                                or p.get("pageUrl") or ""),
        "post_id": "fb_" + pid,
        "caption": text,
        "post_type": "Video" if is_video else
                     ("Carousel" if len(media) > 1 else ("Image" if media else "Text")),
        "likes": int(p.get("likes") or p.get("reactionsCount") or 0),
        "comments": int(p.get("comments") or p.get("commentsCount") or 0),
        "post_date": parse_date(p.get("timestamp") or p.get("time")),
        "post_url": url,
        "thumbnail_url": thumb,
        "video_url": url if is_video else None,
        "hashtags": ", ".join(re.findall(r"#(\w+)", text)),
        "is_video": is_video,
    }


def tt_extract(p):
    """Apify tiktok-profile-scraper item -> common post fields (None = broken).
    video_url is the TikTok page; transcribe_videos.py fetches it with yt-dlp."""
    vid = str(p.get("id") or "").strip()
    if p.get("error") or not vid:
        return None
    a = p.get("authorMeta") or {}
    user = tt_handle(a.get("name") or a.get("uniqueId") or "")
    url = p.get("webVideoUrl") or f"https://www.tiktok.com/@{user}/video/{vid}"
    vm = p.get("videoMeta") or {}
    slides = bool(p.get("isSlideshow"))
    tags = [h.get("name") for h in (p.get("hashtags") or [])
            if isinstance(h, dict) and h.get("name")]
    return {
        "competitor": user,
        "post_id": "tt_" + vid,
        "caption": p.get("text") or "",
        "post_type": "Carousel" if slides else "Video",
        "likes": int(p.get("diggCount") or 0),
        "comments": int(p.get("commentCount") or 0),
        "post_date": parse_date(p.get("createTimeISO") or p.get("createTime")),
        "post_url": url,
        "thumbnail_url": vm.get("coverUrl") or vm.get("originalCoverUrl") or None,
        "video_url": None if slides else url,
        "hashtags": ", ".join(tags),
        "is_video": not slides,
    }


def _li_competitor(p):
    """Match on the company URL WE asked for first, then the post author."""
    q = p.get("query")
    cands = [q.get("targetUrl") or q.get("url") if isinstance(q, dict) else q,
             p.get("inputUrl"), p.get("targetUrl")]
    a = p.get("author") or {}
    cands += [a.get("linkedinUrl"), a.get("universalName")]
    for c in cands:
        if isinstance(c, str) and c and not c.startswith("http") and "/" not in c:
            return c.strip().lower()
        h = li_handle(c) if isinstance(c, str) else ""
        if h:
            return h
    return ""


def li_extract(p):
    """Apify harvestapi linkedin-company-posts item -> common post fields."""
    pid = str(p.get("id") or "").strip()
    if p.get("error") or not pid.isdigit():
        return None
    eng = p.get("engagement") or {}
    images = [i for i in (p.get("postImages") or []) if isinstance(i, dict)]
    video = p.get("postVideo") if isinstance(p.get("postVideo"), dict) else {}
    doc = p.get("document") if isinstance(p.get("document"), dict) else {}
    cover = ((doc.get("coverPages") or [{}])[0].get("imageUrls") or [None])[0] \
        if doc.get("coverPages") else None
    url = p.get("linkedinUrl") or f"https://www.linkedin.com/feed/update/urn:li:activity:{pid}/"
    text = p.get("content") or ""
    is_video = bool(video)
    return {
        "competitor": _li_competitor(p),
        "post_id": "li_" + pid,
        "caption": text,
        "post_type": "Video" if is_video else ("Carousel" if doc or len(images) > 1
                                               else ("Image" if images else "Text")),
        "likes": int(eng.get("likes") or 0),
        "comments": int(eng.get("comments") or 0),
        "post_date": parse_date((p.get("postedAt") or {}).get("timestamp")
                                or (p.get("postedAt") or {}).get("date")),
        "post_url": url,
        "thumbnail_url": (video.get("thumbnailUrl") or (images[0].get("url") if images else None)
                          or cover),
        # a direct file when LinkedIn gives one, else the post page for yt-dlp
        "video_url": (video.get("videoUrl") or url) if is_video else None,
        "hashtags": ", ".join(re.findall(r"#(\w+)", text)),
        "is_video": is_video,
    }


def xhs_extract(n):
    """One note from zen-studio rednote-user-profile-scraper (with _uid added)."""
    nid = str(n.get("id") or n.get("note_id") or "").strip()
    if not nid or not n.get("_uid"):
        return None
    title = (n.get("display_title") or n.get("title") or "").strip()
    desc = re.sub(r"\[话题\]#", "", n.get("desc") or "").strip()
    caption = desc if title and title in desc else "\n".join(x for x in (title, desc) if x)
    is_video = (n.get("type") or "").lower() == "video"
    tok = n.get("xsec_token")
    url = f"https://www.xiaohongshu.com/explore/{nid}" + (
        f"?xsec_token={urllib.parse.quote(tok)}&xsec_source=pc_user" if tok else "")
    imgs = [i for i in (n.get("images_list") or []) if isinstance(i, dict)]
    return {
        "competitor": n["_uid"],
        "post_id": "xhs_" + nid,
        "caption": caption,
        "post_type": "Video" if is_video else ("Carousel" if len(imgs) > 1 else "Image"),
        "likes": int(n.get("likes") or n.get("liked_count") or 0),
        "comments": int(n.get("comments_count") or 0),
        "post_date": parse_date(n.get("create_time")),
        "post_url": url,
        "thumbnail_url": (imgs[0].get("url") if imgs else None),
        "video_url": url if is_video else None,
        "hashtags": ", ".join(re.findall(r"#(\w+)", desc)),
        "is_video": is_video,
    }


def to_rows(posts, followers_map, today, extract=ig_extract):
    """Group the scrape by account, judge each account against ITSELF, and
    guarantee every account contributes its best few posts."""
    by_comp, nofol, broken = {}, 0, 0
    for p in posts:
        x = extract(p)
        if not x:
            broken += 1
            continue
        u = x["competitor"]
        fol = followers_map.get(u, 0)
        if not fol:
            nofol += 1
            continue
        likes, comments = x["likes"], x["comments"]
        by_comp.setdefault(u, []).append({
            "row": {
                **x,
                "caption": x["caption"][:5000],
                "tracker": TRACKER,
                "followers": fol,
                "last_synced": today,
            },
            "eng": likes + comments,
            "er": (likes + comments) / fol,
            "comments": comments,
            # same weighting the dashboard sorts by, so "top N" means the same thing
            "score": (likes + comments * 3) / fol * 100,
        })

    rows, below_total, rescued_total = [], 0, 0
    print(f"  {'账号':<24}{'抓到':>5}{'入选':>5}{'补位':>5}  基准/中位互动")
    for u in sorted(by_comp):
        recs = by_comp[u]
        engs = [r["eng"] for r in recs]
        med = statistics.median(engs) if len(engs) >= 3 else 0
        keep_ids, kept = set(), []
        for r in recs:
            if r["eng"] < MIN_ENGAGE:
                continue
            if (r["er"] >= MIN_ER
                    or r["comments"] >= MIN_COMMENTS
                    or (med > 0 and r["eng"] >= OUTLIER_X * med)):
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
        passed = len(kept)
        # safety net: a quiet account still contributes its best posts
        rescued = 0
        if passed < TOP_N_PER_COMP:
            for r in sorted(recs, key=lambda r: -r["score"]):
                if r["row"]["post_id"] in keep_ids:
                    continue
                keep_ids.add(r["row"]["post_id"])
                kept.append(r)
                rescued += 1
                if len(kept) >= TOP_N_PER_COMP:
                    break
        # cap: keep the best MAX_PER_COMP so one large account cannot flood
        capped = 0
        if len(kept) > MAX_PER_COMP:
            kept.sort(key=lambda r: -r["score"])
            capped = len(kept) - MAX_PER_COMP
            kept = kept[:MAX_PER_COMP]
        below_total += len(recs) - passed
        rescued_total += rescued
        rows.extend(r["row"] for r in kept)
        note = f" · 超额截掉 {capped}" if capped else ""
        print(f"  @{u:<23}{len(recs):>5}{len(kept):>5}{rescued:>5}  "
              f"中位 {int(med)} 互动 · {recs[0]['row']['followers']:,} 粉{note}")

    print(f"  ── 合计 {len(rows)} 条入库 "
          f"({len(rows) - rescued_total} 达标 + {rescued_total} 补位) | "
          f"{below_total} 条未达标 | {nofol} 条缺粉丝数 | {broken} 条抓取失败")
    if len(rows) < 20:
        print("  提示: 内容偏少。到仓库 Settings → Variables 调松阈值，例如")
        print("        MIN_ER=0.01  MIN_COMMENTS=8  OUTLIER_X=1.2  TOP_N_PER_COMP=5")
        print("        想要每个账号更多条 → 调大 MAX_PER_COMP")
        print("        或在 Supabase 的 competitors 表多加几个竞对账号。")
    return rows


# ---------------------------------------------------------------------------
# Cleanup (dashboard weight, not Supabase limits — free tier holds 500MB)
# ---------------------------------------------------------------------------


def cleanup():
    rows = sb("posts?select=post_id,status,last_synced&order=last_synced.asc.nullsfirst")
    if len(rows) <= MAX_ROWS:
        print(f"[Cleanup] {len(rows)} rows <= {MAX_ROWS}, nothing removed")
        return
    overflow = len(rows) - MAX_ROWS
    victims = [r["post_id"] for r in rows if r.get("status") not in PROTECTED][:overflow]
    if not victims:
        print(f"[Cleanup] WARNING: {overflow} over cap but all rows carry work")
        return
    for i in range(0, len(victims), 100):
        chunk = ",".join(victims[i:i+100])  # IG shortcodes are URL-safe
        sb(f"posts?post_id=in.({chunk})",
           method="DELETE", prefer="return=minimal")
    print(f"[Cleanup] removed {len(victims)} oldest unworked rows")


# ---------------------------------------------------------------------------
# Scrape (one function per platform; both return followers, posts, extractor)
# ---------------------------------------------------------------------------


def scrape_ig(token, names):
    profiles = run_actor(token, "apify~instagram-profile-scraper",
                         {"usernames": names}, "Step 1 profiles")
    followers = {}
    for pr in profiles:
        u = (pr.get("username") or "").lower().strip()
        c = pr.get("followersCount") or pr.get("followers") or 0
        if u and c:
            followers[u] = int(c)
    print(f"  followers for {len(followers)}/{len(names)}")

    try:
        suggest_competitors(profiles)
    except Exception as exc:
        print(f"[Suggest] skipped ({exc})")

    posts = run_actor(token, "apify~instagram-post-scraper",
                      {"username": names, "resultsLimit": RESULTS_LIMIT,
                       "onlyPostsNewerThan": f"{DAYS_BACK} days"}, "Step 2 posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, "
          f"<= {RESULTS_LIMIT}/account)")
    return followers, posts, ig_extract


def scrape_fb(token, names):
    start_urls = [{"url": fb_page_url(n)} for n in names]
    pages = run_actor(token, "apify~facebook-pages-scraper",
                      {"startUrls": start_urls}, "Step 1 FB pages")
    followers = {}
    for pg in pages:
        # page likes as fallback: some pages hide the follower count
        c = pg.get("followers") or pg.get("followersCount") or pg.get("likes") or 0
        for key in (pg.get("inputUrl"), pg.get("facebookUrl"), pg.get("pageUrl")):
            h = fb_handle(key or "")
            if h and c:
                followers.setdefault(h, int(c))
    print(f"  followers for {len([n for n in names if n in followers])}/{len(names)}")
    if pages and not followers:
        print(f"  DEBUG page fields: {sorted(pages[0])[:40]}")
    for n in names:
        if n not in followers:
            print(f"  WARNING: no follower count for facebook.com/{n} — its posts are "
                  "skipped. Check the page name in the competitors table.")

    posts = run_actor(token, "apify~facebook-posts-scraper",
                      {"startUrls": start_urls, "resultsLimit": RESULTS_LIMIT,
                       "onlyPostsNewerThan": f"{DAYS_BACK} days"}, "Step 2 FB posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, "
          f"<= {RESULTS_LIMIT}/page)")
    matched = {(fb_extract(p) or {}).get("competitor") for p in posts} & set(names)
    if posts and not matched:
        print(f"  DEBUG post fields: {sorted(posts[0])[:40]}")
    return followers, posts, fb_extract


def _report_followers(names, followers, where):
    print(f"  followers for {len([n for n in names if n in followers])}/{len(names)}")
    for n in names:
        if n not in followers:
            print(f"  WARNING: no follower count for {where(n)} — its posts are skipped. "
                  "Check the name in the competitors table.")


def scrape_tt(token, names):
    # one call: every video carries its author's follower count (authorMeta.fans)
    posts = run_actor(token, "clockworks~tiktok-profile-scraper",
                      {"profiles": names, "profileScrapeSections": ["videos"],
                       "profileSorting": "latest", "resultsPerPage": RESULTS_LIMIT,
                       "oldestPostDateUnified": f"{DAYS_BACK} days"}, "TikTok videos")
    followers = {}
    for p in posts:
        a = p.get("authorMeta") or {}
        u, c = tt_handle(a.get("name") or ""), a.get("fans") or 0
        if u and c:
            followers[u] = max(followers.get(u, 0), int(c))
    print(f"  scraped {len(posts)} videos (last {DAYS_BACK} days, <= {RESULTS_LIMIT}/account)")
    _report_followers(names, followers, lambda n: f"tiktok.com/@{n} (or no video in "
                      f"the last {DAYS_BACK} days)")
    if posts and not followers:
        print(f"  DEBUG video fields: {sorted(posts[0])[:40]}")
    return followers, posts, tt_extract


def scrape_li(token, names):
    urls = [li_page_url(n) for n in names]
    comps = run_actor(token, "harvestapi~linkedin-company", {"companies": urls},
                      "Step 1 LinkedIn companies")
    followers = {}
    for i, c in enumerate(comps):
        cnt = c.get("followerCount") or c.get("followers") or 0
        keys = [c.get("universalName"), c.get("linkedinUrl"), c.get("url"),
                c.get("input"), c.get("query") if isinstance(c.get("query"), str) else None]
        hs = {k.strip().lower() if "/" not in k else li_handle(k)
              for k in keys if isinstance(k, str) and k}
        for h in hs:
            if h in names and cnt:
                followers.setdefault(h, int(cnt))
    if comps and not followers:
        print(f"  DEBUG company fields: {sorted(comps[0])[:40]}")
    _report_followers(names, followers, lambda n: f"linkedin.com/company/{n}")

    since = (datetime.now(timezone.utc).date().toordinal() - DAYS_BACK)
    posts = run_actor(token, "harvestapi~linkedin-company-posts",
                      {"targetUrls": urls, "maxPosts": RESULTS_LIMIT,
                       "postedLimitDate": datetime.fromordinal(since).date().isoformat(),
                       "includeReposts": False, "includeQuotePosts": True,
                       "scrapeReactions": False, "scrapeComments": False},
                      "Step 2 LinkedIn posts")
    print(f"  scraped {len(posts)} posts (last {DAYS_BACK} days, <= {RESULTS_LIMIT}/page)")
    matched = {(li_extract(p) or {}).get("competitor") for p in posts} & set(names)
    if posts and not matched:
        print(f"  DEBUG post fields: {sorted(posts[0])[:40]}")
        print(f"  DEBUG author: {json.dumps(posts[0].get('author'), ensure_ascii=False)[:300]}")
    return followers, posts, li_extract


def scrape_xhs(token, names):
    items = run_actor(token, "zen-studio~rednote-user-profile-scraper",
                      {"profileUrls": [xhs_profile_url(n) for n in names],
                       "includeNotes": True, "maxNotesPerProfile": RESULTS_LIMIT},
                      "XHS profiles + notes")
    cutoff = datetime.now(timezone.utc).timestamp() - DAYS_BACK * 86400
    followers, posts, old = {}, [], 0
    for it in items:
        for k in ("free_preview_notice", "limit_reached", "message"):
            if it.get(k):
                print(f"  NOTE from actor ({k}): {str(it[k])[:200]}")
        prof = it.get("profile") or {}
        uid = xhs_handle(it.get("_user_id") or prof.get("userid") or it.get("_input") or "")
        fans = prof.get("fans") or next((x.get("count") for x in (prof.get("interactions") or [])
                                         if isinstance(x, dict) and x.get("type") == "fans"), 0)
        if uid and fans:
            followers[uid] = int(fans)
        if uid and prof.get("nickname"):
            print(f"  {uid} = {prof['nickname']} · {int(fans or 0):,} 粉")
        for n in it.get("notes") or []:
            ts = n.get("create_time") or 0
            ts = ts / 1000 if ts > 1e11 else ts
            if ts and ts < cutoff:
                old += 1
                continue
            posts.append({**n, "_uid": uid})
    print(f"  {len(posts)} notes in the last {DAYS_BACK} days ({old} older skipped)")
    _report_followers(names, followers, lambda n: xhs_profile_url(n))
    return followers, posts, xhs_extract


SCRAPERS = {"FB": scrape_fb, "TT": scrape_tt, "LI": scrape_li, "XHS": scrape_xhs}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    token = (os.environ.get("APIFY_TOKEN") or "").strip()
    if not token or not SUPABASE_URL or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        print("ERROR: APIFY_TOKEN / SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY required")
        sys.exit(1)

    names = fetch_competitors()
    if not names:
        print(f"No active {TRACKER} competitors in Supabase — nothing to do.")
        return
    print(f"[{TRACKER}] {len(names)} competitors: {', '.join(names[:8])}"
          + (" ..." if len(names) > 8 else ""))
    print(f"  filter: ER>={MIN_ER:.1%} OR comments>={MIN_COMMENTS} OR "
          f"engagements>={OUTLIER_X}x own median | floor {MIN_ENGAGE} | "
          f"keep {TOP_N_PER_COMP}-{MAX_PER_COMP} per account")

    if TRACKER in SCRAPERS:
        followers, posts, extract = SCRAPERS[TRACKER](token, names)
    else:
        followers, posts, extract = scrape_ig(token, names)

    today = datetime.now(timezone.utc).date().isoformat()
    rows = to_rows(posts, followers, today, extract)
    if rows:
        for i in range(0, len(rows), 100):
            sb("posts?on_conflict=post_id", method="POST", data=rows[i:i+100],
               prefer="resolution=merge-duplicates,return=minimal")
        print(f"[Upsert] {len(rows)} rows written")

        # An IG 403 usually means the CDN signature expired, not that the video
        # is gone. This sync just refreshed those URLs — clear the fail marks so
        # transcription gets another shot with the fresh links.
        mark = urllib.parse.quote("(转录失败)")
        sb(f"posts?transcript=eq.{mark}&last_synced=eq.{today}",
           method="PATCH", data={"transcript": None}, prefer="return=minimal")
        print("[Upsert] reopened previously-failed transcripts with fresh URLs")
    else:
        print("[Upsert] nothing passed the filter — check the per-account table "
              "above, then loosen the Variables or add more competitors")

    try:
        cleanup()
    except Exception as exc:
        print(f"[Cleanup] skipped ({exc})")

    print("Done.")


if __name__ == "__main__":
    main()
