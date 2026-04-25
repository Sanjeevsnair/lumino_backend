import requests

BASE_URL = "https://primebox.pages.dev"

class PrimeboxScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    # -------- SEARCH --------
    def search(self, query):
        search_api = f"{BASE_URL}/api/search?keyword={query}"

        try:
            res = self.session.get(search_api, timeout=10)
            json_data = res.json()
        except Exception as e:
            print(f"❌ failed to fetch search API: {e}")
            return []

        if json_data.get("code") != 0:
            print("❌ search API error")
            return []

        items = json_data.get("data", {}).get("items", [])
        results = []

        for item in items:
            title = item.get("title", "N/A")
            detail_path = item.get("detailPath", "")
            subject_type = item.get("subjectType")
            cover = item.get("cover", {})
            image = cover.get("url")

            results.append({
                "title": title,
                "link": f"{BASE_URL}/detail/{detail_path}" if detail_path else None,
                "subjectType": subject_type,
                "image": image
            })

        return results

    # -------- GET STREAMS --------
    def get_streams(self, detail_url, subject_type):
        if not detail_url:
            print("❌ invalid detail URL")
            return []

        detail_path = detail_url.split("/detail/")[-1]

        # -------- DETAIL API --------
        # We still need to call the detail API to get the proper subjectId
        detail_api = f"{BASE_URL}/api/detail?detailPath={detail_path}"

        try:
            res = self.session.get(detail_api, timeout=10)
            json_data = res.json()
        except:
            print("❌ failed to fetch detail API")
            return []

        if json_data.get("code") != 0:
            print("❌ detail API error")
            return []

        data = json_data.get("data", {})

        subject_id = (
            data.get("id")
            or data.get("subjectId")
            or data.get("subject_id")
            or data.get("subject", {}).get("subjectId")
        )

        if not subject_id:
            print("❌ subjectId not found")
            return []

        # -------- MOVIE --------
        if subject_type == 1:
            print("\n🎬 movie detected → fetching streams...\n")
            se, ep = 0, 0

        # -------- SERIES --------
        elif subject_type == 2:
            print("\n📺 series detected\n")
            
            # ✅ Directly ask the user for Season and Episode numbers
            try:
                se = int(input("Enter season number (e.g., 1): "))
                ep = int(input("Enter episode number (e.g., 1): "))
            except ValueError:
                print("❌ Invalid input. Please enter numbers only.")
                return []
            
            print("\nfetching streams...\n")
            
        else:
            print("❌ unknown subject type")
            return []

        # -------- PLAY API --------
        play_api = (
            f"{BASE_URL}/api/play?"
            f"subjectId={subject_id}&se={se}&ep={ep}&detailPath={detail_path}"
        )

        try:
            res = self.session.get(play_api, timeout=10)
            play_json = res.json()
        except:
            print("❌ play API request failed")
            return []

        if play_json.get("code") != 0:
            print("❌ play API error")
            return []

        streams = play_json.get("data", {}).get("streams", [])

        return streams


# ===== MAIN CLI =====
if __name__ == "__main__":
    scraper = PrimeboxScraper()

    try:
        while True:
            query = input("\nsearch movie/series (or exit): ").strip()

            if query.lower() == "exit":
                break

            results = scraper.search(query)

            if not results:
                print("\n❌ no results found")
                continue

            print(f"\nfound {len(results)} results:\n")

            for i, r in enumerate(results, 1):
                type_label = "[Movie]" if r['subjectType'] == 1 else "[Series]" if r['subjectType'] == 2 else ""
                print(f"{i}. {r['title']} {type_label}")

            try:
                choice = int(input("\nselect number: ")) - 1
                selected = results[choice]
            except:
                print("invalid choice")
                continue

            streams = scraper.get_streams(selected["link"], selected.get("subjectType"))

            if not streams:
                print("❌ no streams found")
                continue

            print("\n✅ Streams found:")
            for s in streams:
                reso = s.get("resolution", "unknown")
                url = s.get("url", "")
                print(f"{reso}p → {url}")
                
    except KeyboardInterrupt:
        print("\nExited.")