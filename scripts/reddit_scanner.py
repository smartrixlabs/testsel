"""
RedLead - Reddit Scanner (Public Feeds API Version)
===================================================
Runs every ~15 minutes via staggered cron across GitHub Actions (:00), GitLab CI (:15), Render (:30).
- Uses Reddit's public .json feed API (no OAuth required)
- Reads all ACTIVE projects from Firestore
- Scans Reddit feeds (search + subreddit /new feeds) for each project's keywords
- Deduplicates posts, scores intent (0-100 preScore), stores HIGH/MEDIUM to Firestore
"""

import os
import json
import time
import logging
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("RedLeadScanner")

# ── Immediate Discard Signals ────────────────────────────────────────────────

SPAM_SIGNALS = [
    # Self-promotion / content farming
    "check out my", "shameless plug", "i built", "i made",
    "i created", "just launched", "launching", "my product",
    "my tool", "my app", "my startup", "side project",
    "show hn", "showoff",
    # Marketing / sales
    "promo", "discount", "sale", "coupon", "use code",
    "affiliate", "referral link", "sponsor", "sponsored",
    "limited time", "sign up now",
    # Engagement bait
    "upvote if", "like and subscribe", "follow me",
    "check my profile", "link in bio",
]

# ── Weighted Intent Scoring ───────────────────────────────────────────────────
# Weights 2-12 → raw accumulates → normalised to 0-100 via MAX_POSSIBLE=80

INTENT_BUCKETS: dict = {
    # High-buy intent
    12: [
        "ready to buy", "ready to pay", "need this asap", "need this urgently",
        "purchase today", "buying today", "will pay for", "paying for",
    ],
    11: [
        "willing to pay", "budget is", "budget for", "paid option ok",
        "what should i buy", "which one should i buy", "what should we buy",
        "need recommendations", "need recommendation", "can someone recommend",
    ],
    10: [
        "looking for", "can anyone recommend", "any recommendations",
        "recommend me", "best tool", "best software", "best app",
        "need a tool", "need software", "need an app", "help me find",
        "what do you recommend", "what's the best", "whats the best",
        "which tool", "which platform", "which service", "help me choose",
    ],
    # Switch / migrate intent
    9: [
        "alternatives to", "alternative to", "looking for alternative",
        "switch from", "switching from", "moving from", "migrating from",
        "replacing", "replacement for", "ditch", "moving away from",
        "leaving", "canceling", "cancelling", "sunsetting",
    ],
    8: [
        "better than", "instead of", "cheaper than", "faster than",
        "open source alternative", "free alternative", "what replaced",
        "competitor to", "similar to", "equivalent to",
    ],
    # Research / comparison intent
    7: [
        "comparison", "vs", "versus", "compared to", "how does it compare",
        "pros and cons", "tradeoffs", "feature comparison", "which is better",
        "is there a tool", "is there software", "is there an app",
        "what software", "what service", "what platform", "what app",
        "what stack", "what are people using",
    ],
    6: [
        "best way to", "how to choose", "what do you use", "what are you using",
        "any good", "good tool for", "tools for", "software for", "app for",
        "any suggestions", "suggestions for", "recommendations for",
        "any alternatives", "looking into", "evaluating", "shortlist",
    ],
    # Pain / friction intent
    5: [
        "frustrated with", "fed up with", "tired of", "hate using", "sick of",
        "annoyed with", "struggling with", "cannot figure out", "can't figure out",
        "blocked by", "pain point", "dealbreaker", "missing feature",
        "waste of time", "takes too long", "manual process", "too manual",
        "if only there was", "wish there was", "need a better way",
    ],
    4: [
        "problem with", "issue with", "doesn't work", "stopped working",
        "broken", "slow", "buggy", "unreliable", "keeps failing",
        "is it worth", "should i use", "worth switching", "worth migrating",
    ],
    # Discovery / light exploration
    3: [
        "does anyone", "any experience", "anyone tried", "has anyone used",
        "how do you handle", "workflow for", "what's your experience",
        "what has worked for you", "any success with", "who uses",
    ],
    2: [
        "thoughts on", "opinions on", "worth it", "review", "reviews",
        "tips for", "best practices", "how do i", "getting started with",
        "new to", "just discovered", "heard of", "considering",
    ],
}

# Regex aliases — capture Reddit shorthand and typos that plain substring misses
INTENT_REGEX_ALIASES: dict = {
    10: [r"\breco(?:mmend|mmendation|s)?\b", r"\brecs\b", r"\brecc?o(?:s)?\b"],
    9:  [r"\bswitch(?:ing)?\b", r"\bmigrat(?:e|ing|ion)\b"],
    7:  [r"\b(vs\.?|versus)\b", r"\bcompare(?:d)?\b"],
    5:  [r"\bfrustrat(?:ed|ing)\b", r"\bpain\s*point\b"],
}

# Negative signals that reduce score when the post is not buyer-intent
NEGATIVE_INTENT_WEIGHTS: dict = {
    # Career / jobs
    "hiring": -8, "job opening": -8, "resume": -6, "interview prep": -6,
    # Student / homework
    "homework": -7, "assignment": -7, "course project": -6, "student project": -6,
    # Showcase / meme / content farming
    "rate my": -6, "roast my": -6, "look what i built": -8,
    "showcase": -5, "template dump": -5,
    # Pure troubleshooting (not buying)
    "stack trace": -4, "exception": -4, "error code": -4,
}

import re

def score_post(title: str, body: str, keywords: list) -> dict:
    """
    Returns { preScore: int 0-100, intent: HIGH|MEDIUM|LOW, matchedKeywords: list }
    Layers:
      1. Immediate discard: SPAM_SIGNALS + NSFW
      2. Keyword gate: must contain at least one campaign keyword
      3. Positive intent accumulation (INTENT_BUCKETS + INTENT_REGEX_ALIASES)
      4. Negative intent adjustment (NEGATIVE_INTENT_WEIGHTS)
      5. Title boost: signals found in title are counted twice
      6. Normalise to 0-100 and map to HIGH / MEDIUM / LOW
    """
    text = (title + " " + (body or "")).lower()
    title_lower = title.lower()

    # Layer 1 — immediate discard
    for sig in SPAM_SIGNALS:
        if sig in text:
            return {"preScore": 0, "intent": "LOW", "matchedKeywords": []}
    if "nsfw" in text:
        return {"preScore": 0, "intent": "LOW", "matchedKeywords": []}

    # Layer 2 — keyword gate (whole-word match)
    matched_kws = [
        kw for kw in keywords
        if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text)
    ]
    if not matched_kws:
        return {"preScore": 0, "intent": "LOW", "matchedKeywords": []}

    # Layer 3 — accumulate positive phrase scores
    raw = 0
    for weight, phrases in INTENT_BUCKETS.items():
        for phrase in phrases:
            if phrase in text:
                raw += weight

    # Regex aliases (Reddit shorthand / typos)
    for weight, patterns in INTENT_REGEX_ALIASES.items():
        for pat in patterns:
            if re.search(pat, text):
                raw += weight
                break  # count each alias bucket once

    # Layer 4 — negative intent penalties
    for signal, penalty in NEGATIVE_INTENT_WEIGHTS.items():
        if signal in text:
            raw += penalty

    # Layer 5 — title boost: signals in title are counted twice total
    for weight, phrases in INTENT_BUCKETS.items():
        for phrase in phrases:
            if phrase in title_lower:
                raw += weight

    # Layer 6 — normalise to 0-100
    MAX_POSSIBLE = 80
    pre_score = min(max(int((raw / MAX_POSSIBLE) * 100), 0), 100)

    if pre_score >= 45:
        intent = "HIGH"
    elif pre_score >= 18:
        intent = "MEDIUM"
    else:
        intent = "LOW"

    return {"preScore": pre_score, "intent": intent, "matchedKeywords": matched_kws}


def quality_bonus(score: int | None, comment_count: int | None, source: str) -> int:
    """Layer 6 quality bonus — only applied when engagement metrics are real (JSON-enriched).
    RSS feed values are placeholders; do not apply the bonus for RSS-sourced posts.
    """
    if source != "json_enriched":
        return 0
    bonus = 0
    score = score or 0
    comment_count = comment_count or 0
    if score >= 10:          bonus += 5
    if score >= 50:          bonus += 5
    if comment_count >= 3:   bonus += 5
    if comment_count >= 10:  bonus += 5
    return bonus


def find_matched_keywords(title: str, body: str, keywords: list) -> list:
    """Pre-filter helper — used in scan_subreddit_feeds to skip irrelevant posts early."""
    text = (title + " " + (body or "")).lower()
    return [kw for kw in keywords if re.search(r'\b' + re.escape(kw.lower()) + r'\b', text)]

def post_age_hours(created_utc: float) -> float:
    now = datetime.now(timezone.utc).timestamp()
    return (now - created_utc) / 3600

# ── Firebase Init ─────────────────────────────────────────────────────────────

def init_firebase():
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT env var not set")
    sa_dict = json.loads(sa_json)
    if not firebase_admin._apps:
        cred = credentials.Certificate(sa_dict)
        firebase_admin.initialize_app(cred)
    return firestore.client()


# ── Plan Config ───────────────────────────────────────────────────────────────

# Fallback plan limits used when Firestore `config/plans` is unreachable.
_DEFAULT_PLAN_LIMITS = {
    "free":    {"leadsPerDay": 20},
    "premium": {"leadsPerDay": 50},
    "pro":     {"leadsPerDay": 80},
}

def load_plan_limits(db) -> dict:
    """Read plan limits from Firestore `config/plans` (single source of truth).
    Falls back to _DEFAULT_PLAN_LIMITS if the document doesn't exist yet.
    Returns a dict keyed by plan name with at least a `leadsPerDay` key.
    """
    try:
        snap = db.collection("config").document("plans").get()
        if snap.exists:
            data = snap.to_dict() or {}
            # Normalise: ensure every plan has leadsPerDay
            result = {}
            for plan, cfg in data.items():
                if isinstance(cfg, dict) and "leadsPerDay" in cfg:
                    result[plan] = cfg
            if result:
                log.info(f"Loaded plan limits from Firestore: { {k: v['leadsPerDay'] for k, v in result.items()} }")
                return result
    except Exception as e:
        log.warning(f"Could not load plan limits from Firestore: {e} — using defaults")
    return _DEFAULT_PLAN_LIMITS


def get_project_store_limit(db, proj: dict, plan_limits: dict) -> int:
    """Resolve the per-scan-run write cap for a project based on its owner's plan.
    Falls back to the free limit if userId is missing or user doc is absent.
    """
    uid = proj.get("userId")
    plan = "free"
    if uid:
        try:
            user_snap = db.collection("users").document(uid).get()
            if user_snap.exists:
                plan = (user_snap.to_dict() or {}).get("plan", "free")
        except Exception as e:
            log.warning(f"Could not fetch user plan for uid={uid}: {e}")
    cfg = plan_limits.get(plan) or plan_limits.get("free") or {"leadsPerDay": 20}
    return cfg["leadsPerDay"]

# ── Post Deduplication ────────────────────────────────────────────────────────

def get_existing_post_ids(db, project_id: str) -> set:
    """Fetch all existing post IDs for a project to avoid duplicates."""
    docs = db.collection("posts").where("projectId", "==", project_id).stream()
    return {d.id for d in docs}

# ── Scan Reddit Public Feeds (.json) ──────────────────────────────────────────

import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

def fetch_reddit_json(url: str, params: dict = None) -> list:
    """Fetch and parse Reddit RSS (Atom) feeds to bypass strict JSON datacenter blocks."""
    try:
        if params:
            query_string = urllib.parse.urlencode(params)
            target_url = f"{url}?{query_string}"
        else:
            target_url = url
            
        # Swap JSON extension to RSS, which is significantly less restricted for scrapers
        target_url = target_url.replace(".json", ".rss")
        
        curl_cmd = [
            "curl", "-s", target_url,
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        ]
        
        result = subprocess.check_output(curl_cmd, timeout=20).decode('utf-8')
        
        if "<feed" not in result:
            log.warning(f"Failed to decode XML from {target_url} (Likely blocked by WAF)")
            return []
            
        root = ET.fromstring(result)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        
        posts = []
        for entry in root.findall("atom:entry", ns):
            title = entry.find("atom:title", ns).text or ""
            content = entry.find("atom:content", ns)
            body = content.text if content is not None else ""
            
            author_el = entry.find("atom:author/atom:name", ns)
            author = author_el.text if author_el is not None else "[deleted]"
            
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib["href"] if link_el is not None else ""
            
            id_str = entry.find("atom:id", ns).text or ""
            # e.g. "t3_1c6xyz"
            post_id = id_str.split("_")[-1] if "_" in id_str else id_str
            
            updated_str = entry.find("atom:updated", ns).text or ""
            try:
                dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                created_utc = dt.timestamp()
            except:
                created_utc = datetime.now(timezone.utc).timestamp()

            # NSFW / Relevancy Filtering
            # We rely on classify_intent's whole-word keyword check to discard irrelevant posts,
            # but we can do a quick check for explicit NSFW tags in the content here.
            content_lower = (title + " " + body).lower()
            if "nsfw" in content_lower:
                continue

            posts.append({
                "data": {
                    "id": post_id,
                    "title": title,
                    "selftext": body,
                    "author": author,
                    "subreddit": link.split("/r/")[1].split("/")[0] if "/r/" in link else "unknown",
                    "permalink": link.replace("https://www.reddit.com", ""),
                    "score": 1,
                    "num_comments": 0,
                    "created_utc": created_utc
                }
            })
            
        return posts
    except Exception as e:
        log.warning(f"Failed to fetch {target_url}: {e}")
        return []

def scan_keyword_feeds(keywords: list, max_per_kw: int = 25, age_limit_hours: int = 72) -> list:
    posts = []
    for keyword in keywords:
        log.info(f"  Searching Reddit RSS feed: '{keyword}'")
        url = "https://www.reddit.com/search.json"
        params = {"q": keyword, "sort": "new", "t": "day", "limit": max_per_kw}
        
        children = fetch_reddit_json(url, params)
        for child in children:
            post = child["data"]
            if post_age_hours(post["created_utc"]) > age_limit_hours:
                continue
            posts.append({
                "reddit_id": post["id"],
                "title": post.get("title", ""),
                "body": post.get("selftext", ""),
                "author": post.get("author", "[deleted]"),
                "subreddit": post.get("subreddit", "unknown"),
                "url": f"https://www.reddit.com{post.get('permalink', '')}",
                "score": post.get("score", 0),
                "comment_count": post.get("num_comments", 0),
                "created_utc": post["created_utc"],
                "matched_keyword": keyword,
            })
        time.sleep(1.5)  # Respect rate limits
    return posts

def scan_subreddit_feeds(subreddits: list, keywords: list, age_limit_hours: int = 72) -> list:
    posts = []
    for sr in subreddits:
        log.info(f"  Scanning subreddit JSON feed: r/{sr}")
        url = f"https://www.reddit.com/r/{sr}/new.json"
        params = {"limit": 50}
        
        children = fetch_reddit_json(url, params)
        for child in children:
            post = child["data"]
            if post_age_hours(post["created_utc"]) > age_limit_hours:
                continue
            matched = find_matched_keywords(post.get("title", ""), post.get("selftext", ""), keywords)
            if not matched and keywords:
                continue
                
            posts.append({
                "reddit_id": post["id"],
                "title": post.get("title", ""),
                "body": post.get("selftext", ""),
                "author": post.get("author", "[deleted]"),
                "subreddit": post.get("subreddit", "unknown"),
                "url": f"https://www.reddit.com{post.get('permalink', '')}",
                "score": post.get("score", 0),
                "comment_count": post.get("num_comments", 0),
                "created_utc": post["created_utc"],
                "matched_keyword": matched[0] if matched else None,
            })
        time.sleep(1.5)
    return posts

# ── Process & Store Posts ─────────────────────────────────────────────────────

def process_and_store(db, raw_posts: list, project_id: str, user_id: str, all_keywords: list, existing_ids: set, store_limit: int = 20) -> int:
    """Deduplicate, score, sort by preScore descending, and store top-N to Firestore.
    store_limit: max new posts to write per scan run — derived from the project owner's plan.
    Sorting ensures the highest-intent leads are stored when the limit is hit.
    Uses reddit_id as the Firestore document ID to guarantee idempotency.
    Existing documents are NEVER overwritten (preserves user-set status).
    """
    seen_in_batch = set()
    stored = 0

    # Pre-score every candidate so we can sort before writing
    scored_candidates = []
    for raw in raw_posts:
        rid = raw["reddit_id"]
        post_id = f"reddit_{rid}"
        if post_id in existing_ids or rid in seen_in_batch:
            continue
        seen_in_batch.add(rid)

        scored = score_post(raw["title"], raw["body"], all_keywords)
        pre_score = scored["preScore"]
        matched_keywords = scored["matchedKeywords"]
        
        bonus = quality_bonus(raw.get("score"), raw.get("comment_count"), raw.get("source", "rss"))
        pre_score = min(100, pre_score + bonus)
        
        if pre_score >= 45:
            intent = "HIGH"
        elif pre_score >= 18:
            intent = "MEDIUM"
        else:
            continue  # discard LOW before sorting

        scored_candidates.append({
            "post_id": post_id,
            "pre_score": pre_score,
            "intent": intent,
            "matched_kws": matched_keywords,
            "raw": raw
        })

    # Sort best leads first — ensures the store_limit cap keeps highest-intent posts
    scored_candidates.sort(key=lambda x: x["pre_score"], reverse=True)
    log.info(f"   Scored candidates (HIGH/MEDIUM): {len(scored_candidates)}, plan cap: {store_limit}")

    for cand in scored_candidates:
        if stored >= store_limit:
            log.info(f"   Plan cap reached ({store_limit}) — skipping remaining {len(scored_candidates) - stored} candidates")
            break

        raw = cand["raw"]
        intent = cand["intent"]
        matched_kws = cand["matched_kws"]
        pre_score = cand["pre_score"]

        doc = {
            "projectId": project_id,
            "userId": user_id,
            "title": raw["title"],
            "body": raw["body"][:2000],
            "author": raw["author"],
            "subreddit": raw["subreddit"],
            "url": raw["url"],
            "score": raw["score"],
            "commentCount": raw["comment_count"],
            "intent": intent,
            "preScore": pre_score,
            "matchedKeywords": matched_kws,
            "status": "pending",   # default — user can change to completed / ignored
            "aiScoreStatus": "not_requested",
            "createdAt": firestore.SERVER_TIMESTAMP,
            "scannedAt": firestore.SERVER_TIMESTAMP,
            "redditCreatedUtc": raw["created_utc"],
        }

        # Dedup is enforced above — only genuinely new IDs reach this point.
        db.collection("posts").document(cand["post_id"]).set(doc)
        existing_ids.add(cand["post_id"])
        stored += 1
        log.info(f"    Stored [{intent}] score={pre_score}: {raw['title'][:70]}")

    return stored


# ── Update Project Stats ───────────────────────────────────────────────────────────

def update_project_stats(db, project_id: str, newly_stored: int = 0):
    """Increment totalPosts counter and stamp lastScannedAt.
    Uses Increment to avoid reading all posts on every scan run.
    """
    update_data = {"lastScannedAt": firestore.SERVER_TIMESTAMP}
    if newly_stored > 0:
        update_data["totalPosts"] = firestore.Increment(newly_stored)
    db.collection("campaigns").document(project_id).update(update_data)

# ── Main ──────────────────────────────────────────────────────────────────────

def cleanup_old_posts(db):
    """Deletes posts older than 2 days to stay within Firestore free tier (1GB).
    Uses the 'scannedAt' SERVER_TIMESTAMP field that exists on every stored post.
    """
    try:
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0) 
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=2)
        
        # Batch delete up to 400 at a time (Firestore batch limit = 500)
        old_posts = db.collection("posts").where("scannedAt", "<", cutoff).limit(400).get()
        
        if len(old_posts) > 0:
            log.info(f"🧹 Cleaning up {len(old_posts)} old posts (older than 2 days)...")
            batch = db.batch()
            for doc in old_posts:
                batch.delete(doc.reference)
            batch.commit()
            log.info(f"🧹 Deleted {len(old_posts)} old posts.")
        else:
            log.info("🧹 No old posts to clean up.")
    except Exception as e:
        log.error(f"⚠️ Cleanup failed: {e}")

def main():
    log.info("=" * 60)
    log.info("🚀 RedLead Scanner (Public Feeds API) starting…")
    log.info("=" * 60)

    db = init_firebase()
    
    # 1. Self-cleaning
    cleanup_old_posts(db)
    
    # 2. Load plan limits once
    plan_limits = load_plan_limits(db)
    
    # 3. Process active campaigns
    active_campaigns = list(db.collection("campaigns").where("status", "==", "active").stream())
    log.info(f"Found {len(active_campaigns)} active campaign(s)")

    for camp_doc in active_campaigns:
        camp = camp_doc.to_dict()
        project_id = camp_doc.id
        user_id = camp.get("userId", "")
        name = camp.get("name", project_id)
        keywords = camp.get("keywords", [])
        subreddits = camp.get("subreddits", [])

        log.info(f"\n── Campaign: {name} ({project_id})")
        
        if not keywords and not subreddits:
            log.info("   No keywords or subreddits — skipping")
            continue

        existing_ids = get_existing_post_ids(db, project_id)
        log.info(f"   Existing posts tracked: {len(existing_ids)}")

        all_raw = []
        if keywords:
            all_raw.extend(scan_keyword_feeds(keywords, max_per_kw=25))

        if subreddits:
            all_raw.extend(scan_subreddit_feeds(subreddits, keywords))

        log.info(f"   Raw posts fetched: {len(all_raw)}")

        store_limit = get_project_store_limit(db, camp, plan_limits)
        stored = process_and_store(db, all_raw, project_id, user_id, keywords, existing_ids, store_limit=store_limit)
        
        update_project_stats(db, project_id, stored)

    log.info("\nScan complete ✓")

if __name__ == "__main__":
    main()

