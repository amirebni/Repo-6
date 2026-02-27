
import asyncio
import aiohttp
import json
import time
import os
import base64
import urllib.parse
from datetime import datetime

# ==============================
# CONFIG
# ==============================

SOURCE_URL = "https://raw.githubusercontent.com/punez/Repo-5/main/alive.txt"

HISTORY_FILE = "history.json"

OUTPUT_ELITE = "elite_300.txt"
OUTPUT_PREMIUM = "premium_1000.txt"
OUTPUT_STABLE = "stable.txt"
OUTPUT_ALL = "all_ranked.txt"

TCP_TIMEOUT = 1.5
PROBE_COUNT = 3
CONCURRENCY = 150

MAX_LATENCY_OK = 2000
DELETE_FAIL_STREAK = 3
DELETE_MISSING_DAYS = 2
BOOST_STREAK = 5

# ==============================


def log(msg):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


# ==============================
# Download Alive List
# ==============================

async def download_alive():
    async with aiohttp.ClientSession() as session:
        async with session.get(SOURCE_URL, timeout=20) as r:
            text = await r.text()
            return [l.strip() for l in text.splitlines() if l.strip()]


# ==============================
# Parser (minimal)
# ==============================

def parse_node(link):
    try:
        if link.startswith("vmess://"):
            raw = link.replace("vmess://", "")
            raw += "=" * (-len(raw) % 4)
            decoded = base64.b64decode(raw).decode()
            data = json.loads(decoded)
            return data.get("add"), int(data.get("port"))

        u = urllib.parse.urlparse(link)
        return u.hostname, u.port or 443

    except:
        return None, None


def fingerprint(link):
    try:
        host, port = parse_node(link)
        if host and port:
            return f"{host}:{port}"
    except:
        pass
    return None


# ==============================
# TCP Probe
# ==============================

async def tcp_probe(host, port):
    try:
        start = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=TCP_TIMEOUT
        )
        latency = (time.perf_counter() - start) * 1000
        writer.close()
        await writer.wait_closed()
        return latency
    except:
        return None


# ==============================
# Stability Test
# ==============================

async def test_node(link, semaphore):

    host, port = parse_node(link)
    if not host or not port:
        return None

    latencies = []

    async with semaphore:
        for _ in range(PROBE_COUNT):
            latency = await tcp_probe(host, port)
            if latency is not None:
                latencies.append(latency)
            await asyncio.sleep(0.3)

    success_rate = len(latencies) / PROBE_COUNT

    if success_rate < 0.66:
        return {
            "status": "fail"
        }

    avg_latency = sum(latencies) / len(latencies)
    jitter = max(latencies) - min(latencies)

    if avg_latency > MAX_LATENCY_OK:
        return {
            "status": "fail"
        }

    return {
        "status": "success",
        "avg_latency": avg_latency,
        "jitter": jitter
    }


# ==============================
# History Handling
# ==============================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, "r") as f:
        return json.load(f)


def save_history(data):
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ==============================
# Score Engine
# ==============================

def compute_score(data):

    score = 0

    score += data.get("success_streak", 0) * 5

    avg_latency = data.get("avg_latency", 9999)

    if avg_latency < 300:
        score += 20
    elif avg_latency < 700:
        score += 10
    elif avg_latency < 1200:
        score += 5

    if data.get("success_streak", 0) >= BOOST_STREAK:
        score += 10

    jitter = data.get("jitter", 0)
    if jitter > 1000:
        score -= 5

    return score


# ==============================
# MAIN
# ==============================

async def main():

    log("Downloading alive list...")
    links = await download_alive()
    log(f"Alive input: {len(links)}")

    history = load_history()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    semaphore = asyncio.Semaphore(CONCURRENCY)

    tasks = {}
    for link in links[:1500]:  # deep test only first 1500
        fp = fingerprint(link)
        if fp:
            tasks[fp] = (link, asyncio.create_task(test_node(link, semaphore)))

    results = {}

    for fp, (link, task) in tasks.items():
        result = await task
        results[fp] = (link, result)

    updated_history = {}

    # update existing
    for fp, (link, result) in results.items():

        old = history.get(fp, {
            "success_streak": 0,
            "fail_streak": 0,
            "total_success": 0,
            "total_fail": 0,
            "avg_latency": 0
        })

        if result and result["status"] == "success":

            old["success_streak"] += 1
            old["fail_streak"] = 0
            old["total_success"] += 1

            if old["avg_latency"] == 0:
                old["avg_latency"] = result["avg_latency"]
            else:
                old["avg_latency"] = (
                    old["avg_latency"] * 0.7 +
                    result["avg_latency"] * 0.3
                )

            old["jitter"] = result["jitter"]

        else:
            old["fail_streak"] += 1
            old["success_streak"] = 0
            old["total_fail"] += 1

        old["last_seen"] = today
        updated_history[fp] = old

    # remove dead nodes
    final_nodes = []

    for fp, data in updated_history.items():

        if data.get("fail_streak", 0) >= DELETE_FAIL_STREAK:
            continue

        final_nodes.append(fp)

    # scoring
    ranked = sorted(
        final_nodes,
        key=lambda x: compute_score(updated_history[x]),
        reverse=True
    )

    # outputs
    with open(OUTPUT_ALL, "w") as f:
        for fp in ranked:
            link = tasks.get(fp, (None,))[0]
            if link:
                f.write(link + "\n")

    with open(OUTPUT_ELITE, "w") as f:
        for fp in ranked[:300]:
            link = tasks.get(fp, (None,))[0]
            if link:
                f.write(link + "\n")

    with open(OUTPUT_PREMIUM, "w") as f:
        for fp in ranked[:1000]:
            link = tasks.get(fp, (None,))[0]
            if link:
                f.write(link + "\n")

    with open(OUTPUT_STABLE, "w") as f:
        for fp in ranked:
            link = tasks.get(fp, (None,))[0]
            if link:
                f.write(link + "\n")

    save_history(updated_history)

    log("Done ✔")


if __name__ == "__main__":
    asyncio.run(main())
