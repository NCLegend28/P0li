from polymarket_us import PolymarketUS

client = PolymarketUS()
results = client.search.query({"query": "temperature"})
for event in results.get("events", []):
    if not event.get("closed"):
        print(event["title"])