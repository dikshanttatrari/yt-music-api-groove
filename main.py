import os
import time
from fastapi import FastAPI, Query, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from ytmusicapi import YTMusic
import yt_dlp
import requests
from typing import Optional
import os
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()

def setup_cookies():
    """Create youtube_cookies.txt from environment variable"""
    cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt")
    
    if not os.path.exists(cookie_path):
        cookies_str = os.getenv("YT_COOKIES")
        if cookies_str:
            try:
                with open(cookie_path, "w") as f:
                    f.write(cookies_str)
                print("✅ Created youtube_cookies.txt from YT_COOKIES env variable")
            except Exception as e:
                print(f"⚠️ Failed to create cookies file: {e}")

def init_ytmusic():
    """Try OAuth first, fall back to guest mode"""
    oauth_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth.json")
    
    # If oauth.json doesn't exist, try to create it from environment variable
    if not os.path.exists(oauth_path):
        oauth_json_str = os.getenv("OAUTH_JSON")
        if oauth_json_str:
            try:
                # Validate it's proper JSON
                import json
                json.loads(oauth_json_str)
                
                with open(oauth_path, "w") as f:
                    f.write(oauth_json_str)
                print("✅ Created oauth.json from OAUTH_JSON env variable")
            except Exception as e:
                print(f"⚠️ Failed to create oauth.json from env: {e}")
    
    if os.path.exists(oauth_path):
        try:
            from ytmusicapi import OAuthCredentials
            
            client_id = os.getenv("YT_CLIENT_ID")
            client_secret = os.getenv("YT_CLIENT_SECRET")
            
            if not client_id or not client_secret:
                print("⚠️ YT_CLIENT_ID or YT_CLIENT_SECRET not set!")
                raise ValueError("Missing OAuth credentials")
            
            yt = YTMusic(
                oauth_path,
                location="IN",
                language="en",
                oauth_credentials=OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret,
                )
            )
            yt.get_search_suggestions("test")
            print("✅ YTMusic initialized with OAuth")
            return yt
        except Exception as e:
            print(f"⚠️ OAuth init failed: {e}")
            print("⚠️ Falling back to guest mode")
    
    print("ℹ️ Using guest mode (no OAuth)")
    return YTMusic(location="IN", language="en")

setup_cookies()
yt = init_ytmusic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Range",
        "Accept-Ranges",
        "Content-Length",
        "Content-Type"
    ],
)

stream_cache = {}
CACHE_TTL = 18000

def get_cached_stream(video_id: str) -> Optional[dict]:
    if video_id in stream_cache:
        info, timestamp = stream_cache[video_id]
        if time.time() - timestamp < CACHE_TTL:
            return info
        del stream_cache[video_id]
    return None

def set_cached_stream(video_id: str, info: dict):
    stream_cache[video_id] = (info, time.time())

def clear_cached_stream(video_id: str):
    if video_id in stream_cache:
        del stream_cache[video_id]


def extract_stream_url(video_id: str) -> dict:
    """Extract stream URL with OAuth token + multiple client fallbacks"""
    
    cached = get_cached_stream(video_id)
    if cached:
        print(f"✅ Cache HIT: {video_id}")
        return cached

    # Read OAuth access token to pass to yt-dlp
    oauth_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth.json")
    access_token = None
    if os.path.exists(oauth_path):
        try:
            import json
            with open(oauth_path) as f:
                oauth_data = json.load(f)
                access_token = oauth_data.get("access_token")
                if access_token:
                    print(f"🔑 Using OAuth access token for yt-dlp")
        except Exception as e:
            print(f"⚠️ Couldn't read oauth token: {e}")

    clients_to_try = [
        {
            "name": "Android Music",
            "client": "android_music",
            "user_agent": "com.google.android.apps.youtube.music/5.29.52 (Linux; U; Android 11) gzip",
        },
        {
            "name": "Android",
            "client": "android",
            "user_agent": "com.google.android.youtube/19.29.37 (Linux; U; Android 11) gzip",
        },
        {
            "name": "Android Testsuite",
            "client": "android_testsuite",
            "user_agent": "com.google.android.youtube/1.9 (Linux; U; Android 14) gzip",
        },
        {
            "name": "iOS",
            "client": "ios",
            "user_agent": "com.google.ios.youtube/19.29.1 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)",
        },
        {
            "name": "TV Embedded",
            "client": "tv_embedded",
            "user_agent": "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.0)",
        },
        {
            "name": "Web Embedded",
            "client": "web_embedded",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        },
    ]

    last_error = None
    
    for client in clients_to_try:
        try:
            print(f"🔄 Trying client: {client['name']} for {video_id}")
            
            http_headers = {
                "User-Agent": client["user_agent"],
                "Accept-Language": "en-US,en;q=0.9",
            }
            
            # Inject OAuth token as Authorization header!
            if access_token:
                http_headers["Authorization"] = f"Bearer {access_token}"
                http_headers["X-Goog-AuthUser"] = "0"
                http_headers["Origin"] = "https://music.youtube.com"
            
            ydl_opts = {
                "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "nocheckcertificate": True,
                "geo_bypass": True,
                "geo_bypass_country": "IN",
                "socket_timeout": 30,
                "retries": 3,
                "extractor_args": {
                    "youtube": {
                        "player_client": [client["client"]],
                        "skip": ["hls", "dash"],
                    }
                },
                "http_headers": http_headers,
            }

            # Add cookies if available
            cookie_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 
                "youtube_cookies.txt"
            )
            if os.path.exists(cookie_path):
                ydl_opts["cookiefile"] = cookie_path
                print(f"🍪 Using cookies file")

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}",
                    download=False
                )

                url = info.get("url")
                if not url:
                    raise Exception("No URL extracted")

                ext = info.get("ext", "webm")
                acodec = info.get("acodec", "opus")
                content_type = "audio/mp4" if (ext == "m4a" or acodec == "aac") else "audio/webm"

                stream_info = {
                    "url": url,
                    "duration": info.get("duration", 0),
                    "filesize": info.get("filesize") or info.get("filesize_approx", 0),
                    "content_type": content_type,
                    "ext": ext,
                    "client_used": client["name"],
                }
                
                set_cached_stream(video_id, stream_info)
                print(f"✅ Success with: {client['name']}")
                return stream_info

        except Exception as e:
            print(f"❌ {client['name']} failed: {str(e)[:150]}")
            last_error = e
            continue

    raise Exception(f"All clients failed. Last: {last_error}")



@app.get("/api/search")
def search_all(q: str = Query(...)):
    try:
        artists_results = yt.search(q, filter="artists", limit=1)[:1]
        songs_results = yt.search(q, filter="songs", limit=20)[:20]
        playlists_results = yt.search(q, filter="playlists", limit=10)[:10]

        mapped_results = []

        for item in artists_results:
            artist_id = item.get('browseId')
            if not artist_id:
                continue
            thumbnails = item.get('thumbnails', [])
            image_url = thumbnails[-1]['url'] if thumbnails else ""
            if "=" in image_url:
                image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
            mapped_results.append({
                "id": artist_id,
                "title": item.get('artist', item.get('title', 'Unknown Artist')),
                "subtitle": "Artist",
                "type": "artist",
                "image": image_url
            })

        for item in songs_results:
            video_id = item.get('videoId')
            if not video_id:
                continue
            thumbnails = item.get('thumbnails', [])
            image_url = thumbnails[-1]['url'] if thumbnails else ""
            if "=" in image_url:
                image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
            mapped_results.append({
                "id": video_id,
                "title": item.get('title', 'Unknown Title'),
                "subtitle": item.get('artists', [{}])[0].get('name', 'Unknown Artist'),
                "type": "song",
                "image": image_url,
                "duration": item.get('duration', '0:00')
            })

        for item in playlists_results:
            playlist_id = item.get('browseId')
            if not playlist_id:
                continue
            thumbnails = item.get('thumbnails', [])
            image_url = thumbnails[-1]['url'] if thumbnails else ""
            if "=" in image_url:
                image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
            mapped_results.append({
                "id": playlist_id,
                "title": item.get('title', 'Unknown Playlist'),
                "subtitle": f"Playlist • {item.get('author', 'YouTube Music')}",
                "type": "playlist",
                "image": image_url,
                "trackCount": item.get('itemCount', '')
            })

        return {"success": True, "data": mapped_results}
    except Exception as e:
        print(f"Search Error: {e}")
        return {"success": False, "error": str(e)}


@app.get("/api/search/songs")
def search_songs(q: str = Query(...)):
    try:
        results = yt.search(q, filter="songs")
        mapped_results = []
        for item in results:
            video_id = item.get('videoId')
            if not video_id:
                continue
            mapped_results.append({
                "id": video_id,
                "name": item.get('title'),
                "type": "song",
                "year": item.get('year', "2024"),
                "duration": item.get('duration_seconds', 0),
                "album": {
                    "name": item.get('album', {}).get('name', 'Single'),
                    "id": item.get('album', {}).get('id', '')
                },
                "artists": {
                    "primary": [
                        {"name": a.get('name'), "id": a.get('id'), "role": "primary_artists"}
                        for a in item.get('artists', [])
                    ]
                },
                "image": [
                    {"quality": "50x50", "url": item['thumbnails'][0]['url']},
                    {"quality": "150x150", "url": item['thumbnails'][-1]['url']},
                    {"quality": "500x500",
                     "url": item['thumbnails'][-1]['url'].replace('w120-h120', 'w500-h500')}
                ],
                "downloadUrl": [{"quality": "320kbps", "url": f"/api/stream/{video_id}"}]
            })
        return {"success": True, "data": {"results": mapped_results}}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/search/suggestions")
def get_suggestions(q: str = Query(...)):
    try:
        suggestions = yt.get_search_suggestions(q)
        return {"success": True, "data": suggestions}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/home")
def get_home_data():
    try:
        home_data = yt.get_home(limit=5)
        formatted_modules = []
        for shelf in home_data:
            title = shelf.get('title', 'Discover')
            contents = shelf.get('contents', [])
            mapped_contents = []
            for item in contents:
                item_id = (item.get('videoId')
                           or item.get('playlistId')
                           or item.get('browseId'))
                if not item_id:
                    continue
                if item.get('videoId'):
                    item_type = "song"
                elif item.get('playlistId'):
                    item_type = "playlist"
                else:
                    item_type = "album"
                thumbnails = item.get('thumbnails', [])
                image_url = (thumbnails[-1]['url']
                             if thumbnails
                             else "https://via.placeholder.com/500")
                if "=" in image_url:
                    image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
                mapped_contents.append({
                    "id": item_id,
                    "title": item.get('title', 'Unknown Title'),
                    "subtitle": item.get('subtitle', ''),
                    "type": item_type,
                    "image": image_url
                })
            if mapped_contents:
                formatted_modules.append({"title": title, "items": mapped_contents})
        return {"success": True, "data": formatted_modules}
    except Exception as e:
        return {"success": False, "error": "Failed to fetch home data", "details": str(e)}


@app.get("/api/playlist/{playlist_id}")
def get_playlist_details(playlist_id: str):
    try:
        if playlist_id.startswith("RD") or playlist_id.startswith("VL"):
            data = yt.get_watch_playlist(playlistId=playlist_id, limit=50)
            tracks = data.get('tracks', [])
            title = "Top Charts"
        else:
            data = yt.get_playlist(playlist_id, limit=100)
            tracks = data.get('tracks', [])
            title = data.get('title', 'Playlist')

        mapped_tracks = []
        for item in tracks:
            video_id = item.get('videoId')
            if not video_id:
                continue
            thumbnails = (item.get('thumbnails') or item.get('thumbnail') or [])
            image_url = ""
            if thumbnails:
                image_url = thumbnails[-1].get('url', '')
                if "=" in image_url:
                    image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
            duration = (item.get('duration')
                        or item.get('length')
                        or item.get('duration_seconds')
                        or "0:00")
            mapped_tracks.append({
                "id": video_id,
                "title": item.get('title', 'Unknown'),
                "subtitle": item.get('artists', [{}])[0].get('name', 'Unknown Artist'),
                "type": "song",
                "image": image_url,
                "duration": str(duration)
            })
        return {"success": True, "data": {"title": title, "tracks": mapped_tracks}}
    except Exception as e:
        print(f"Playlist Error: {e}")
        return {"success": False, "error": "Playlist restricted or invalid ID."}


@app.get("/api/artist/{artist_id}")
def get_artist_songs(artist_id: str):
    try:
        artist_data = yt.get_artist(artist_id)
        songs = artist_data.get("songs", {}).get("results", [])
        mapped_results = []
        for item in songs:
            video_id = item.get('videoId')
            if not video_id:
                continue
            thumbnails = item.get('thumbnails', [])
            image_url = thumbnails[-1]['url'] if thumbnails else ""
            if "=" in image_url:
                image_url = image_url.split('=')[0] + "=w500-h500-l90-rj"
            artists_list = item.get('artists', [])
            all_singers = (
                ", ".join([a.get('name') for a in artists_list if a.get('name')])
                if artists_list
                else artist_data.get('name', 'Unknown Artist')
            )
            mapped_results.append({
                "id": video_id,
                "title": item.get('title', 'Unknown Title'),
                "subtitle": all_singers,
                "type": "song",
                "image": image_url,
                "duration": item.get('duration', '0:00'),
                "plays": str(item.get('views') or item.get('plays') or "")
            })
        return {
            "success": True,
            "artistName": artist_data.get('name', 'Unknown Artist'),
            "artistImage": artist_data.get('thumbnails', [{}])[-1].get('url', ''),
            "data": mapped_results
        }
    except Exception as e:
        print(f"Artist Error: {e}")
        return {"success": False, "error": str(e)}



@app.get("/api/stream/{video_id}")
def proxy_stream(video_id: str, range: str = Header(None)):
    try:
        stream_info = extract_stream_url(video_id)
        stream_url = stream_info["url"]

        headers = {}
        if range:
            headers["Range"] = range

        upstream = requests.get(stream_url, headers=headers, stream=True, timeout=30)


        if upstream.status_code == 403:
            print(f"🔄 403 received, refreshing {video_id}")
            clear_cached_stream(video_id)
            stream_info = extract_stream_url(video_id)
            stream_url = stream_info["url"]
            upstream = requests.get(stream_url, headers=headers, stream=True, timeout=30)

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            except Exception as e:
                print(f"⚠️ Chunk error: {e}")

        response_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": upstream.headers.get(
                "Content-Type",
                stream_info.get("content_type", "audio/webm")
            ),
            "Cache-Control": "public, max-age=3600",
        }

        if "Content-Length" in upstream.headers:
            response_headers["Content-Length"] = upstream.headers["Content-Length"]
        if "Content-Range" in upstream.headers:
            response_headers["Content-Range"] = upstream.headers["Content-Range"]

        return StreamingResponse(
            generate(),
            status_code=206 if range else 200,
            headers=response_headers
        )

    except Exception as e:
        clear_cached_stream(video_id)
        print(f"❌ Stream Error [{video_id}]: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


@app.get("/api/stream-url/{video_id}")
def get_stream_url(video_id: str):
    try:
        info = extract_stream_url(video_id)
        return {"success": True, **info}
    except Exception as e:
        return {"success": False, "error": str(e)}



@app.get("/api/debug/stream/{video_id}")
def debug_stream(video_id: str):
    try:
        info = extract_stream_url(video_id)
        url = info["url"]
        test = requests.head(url, timeout=10)
        return {
            "success": True,
            "video_id": video_id,
            "client_used": info.get("client_used"),
            "stream_url": url,
            "is_googlevideo": "googlevideo.com" in url,
            "http_status": test.status_code,
            "content_type": test.headers.get("Content-Type"),
            "content_length": test.headers.get("Content-Length"),
            "accept_ranges": test.headers.get("Accept-Ranges"),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "cache_size": len(stream_cache),
        "oauth_active": os.path.exists("oauth.json"),
        "cookies_active": os.path.exists("youtube_cookies.txt"),
    }



frontend_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
    print(f"✅ Serving frontend from: {frontend_path}")
else:
    print(f"⚠️ Frontend folder not found at: {frontend_path}")




if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
