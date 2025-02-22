import aiohttp
import time
import mediaflow_proxy.utils.http_utils

from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)

from comet.utils.models import settings, database, trackers
from comet.utils.general import parse_media_id
from comet.metadata.manager import MetadataScraper
from comet.scrapers.manager import TorrentManager
from comet.utils.general import config_check, format_title, get_client_ip
from comet.debrid.manager import get_debrid_extension, get_debrid
from comet.utils.streaming import custom_handle_stream_request
from comet.utils.logger import logger

streams = APIRouter()


async def remove_ongoing_search_from_database(media_id: str):
    await database.execute(
        "DELETE FROM ongoing_searches WHERE media_id = :media_id",
        {"media_id": media_id},
    )


async def is_first_search(media_id: str) -> bool:
    try:
        await database.execute(
            "INSERT INTO first_searches (media_id, timestamp) VALUES (:media_id, :timestamp)",
            {"media_id": media_id, "timestamp": time.time()},
        )

        return True
    except Exception:
        return False


async def background_scrape(
    torrent_manager: TorrentManager, media_id: str, debrid_service: str
):
    try:
        async with aiohttp.ClientSession() as new_session:
            await torrent_manager.scrape_torrents(new_session)

            if debrid_service != "torrent":
                await torrent_manager.get_and_cache_debrid_availability(new_session)

            logger.log(
                "SCRAPER",
                "📥 Background scrape + availability check complete!",
            )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background scrape + availability check failed: {e}")
    finally:
        await remove_ongoing_search_from_database(media_id)


async def background_availability_check(torrent_manager: TorrentManager, media_id: str):
    try:
        async with aiohttp.ClientSession() as new_session:
            await torrent_manager.get_and_cache_debrid_availability(new_session)
            logger.log(
                "SCRAPER",
                "📥 Background availability check complete!",
            )
    except Exception as e:
        logger.log("SCRAPER", f"❌ Background availability check failed: {e}")
    finally:
        await remove_ongoing_search_from_database(media_id)


@streams.get("/stream/{media_type}/{media_id}.json")
@streams.get("/{b64config}/stream/{media_type}/{media_id}.json")
async def stream(
    request: Request,
    media_type: str,
    media_id: str,
    background_tasks: BackgroundTasks,
    b64config: str = None,
):
    config = config_check(b64config)

    ongoing_search = await database.fetch_one(
        "SELECT timestamp FROM ongoing_searches WHERE media_id = :media_id",
        {"media_id": media_id},
    )

    if ongoing_search:
        return {
            "streams": [
                {
                    "name": "[🔄] Comet",
                    "description": "Search in progress, please try again in a few seconds...",
                    "url": "https://comet.fast",
                }
            ]
        }

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        metadata, aliases = await MetadataScraper(session).fetch_metadata_and_aliases(
            media_type, media_id
        )
        if metadata is None:
            logger.log("SCRAPER", f"❌ Failed to fetch metadata for {media_id}")
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet",
                        "description": "Unable to get metadata.",
                        "url": "https://comet.fast",
                    }
                ]
            }

        title = metadata["title"]
        year = metadata["year"]
        year_end = metadata["year_end"]
        season = metadata["season"]
        episode = metadata["episode"]

        log_title = f"({media_id}) {title}"
        if media_type == "series":
            log_title += f" S{season:02d}E{episode:02d}"

        logger.log("SCRAPER", f"🔍 Starting search for {log_title}")

        id, season, episode = parse_media_id(media_type, media_id)
        media_only_id = id if id != "kitsu" else season

        debrid_service = config["debridService"]
        torrent_manager = TorrentManager(
            debrid_service,
            config["debridApiKey"],
            get_client_ip(request),
            media_type,
            media_id,
            media_only_id,
            title,
            year,
            year_end,
            season,
            episode,
            aliases,
            settings.REMOVE_ADULT_CONTENT and config["removeTrash"],
        )

        await torrent_manager.get_cached_torrents()
        logger.log(
            "SCRAPER", f"📦 Found cached torrents: {len(torrent_manager.torrents)}"
        )

        is_first = await is_first_search(media_id)
        has_cached_results = len(torrent_manager.torrents) > 0

        cached_results = []
        non_cached_results = []

        if not has_cached_results:
            logger.log("SCRAPER", f"🔎 Starting new search for {log_title}")
            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {"media_id": media_id, "timestamp": time.time()},
            )
            background_tasks.add_task(remove_ongoing_search_from_database, media_id)

            await torrent_manager.scrape_torrents(session)
            logger.log(
                "SCRAPER",
                f"📥 Scraped torrents: {len(torrent_manager.torrents)}",
            )

        elif is_first and has_cached_results:
            logger.log(
                "SCRAPER",
                f"🔄 Starting background scrape + availability check for {log_title}",
            )
            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {"media_id": media_id, "timestamp": time.time()},
            )

            background_tasks.add_task(
                background_scrape, torrent_manager, media_id, debrid_service
            )

            cached_results.append(
                {
                    "name": "[🔄] Comet",
                    "description": "First search for this media - More results will be available in a few seconds...",
                    "url": "https://comet.fast",
                }
            )

        await torrent_manager.get_cached_availability()
        if not has_cached_results and debrid_service != "torrent":
            logger.log("SCRAPER", "🔄 Checking availability on debrid service...")
            await torrent_manager.get_and_cache_debrid_availability(session)

        cached_count = 0
        if debrid_service != "torrent":
            cached_count = sum(
                1 for torrent in torrent_manager.torrents.values() if torrent["cached"]
            )
            logger.log(
                "SCRAPER",
                f"💾 Available cached torrents: {cached_count}/{len(torrent_manager.torrents)}",
            )

        if (
            cached_count == 0
            and len(torrent_manager.torrents) > 0
            and not is_first
            and debrid_service != "torrent"
        ):
            logger.log(
                "SCRAPER",
                f"🔄 Starting background availability check for {log_title}",
            )

            await database.execute(
                f"INSERT {'OR IGNORE ' if settings.DATABASE_TYPE == 'sqlite' else ''}INTO ongoing_searches VALUES (:media_id, :timestamp){' ON CONFLICT DO NOTHING' if settings.DATABASE_TYPE == 'postgresql' else ''}",
                {"media_id": media_id, "timestamp": time.time()},
            )

            background_tasks.add_task(
                background_availability_check, torrent_manager, media_id
            )

            cached_results.append(
                {
                    "name": "[🔄] Comet",
                    "description": "Checking debrid availability in background - More results will be available in a few seconds...",
                    "url": "https://comet.fast",
                }
            )

        initial_torrent_count = len(torrent_manager.torrents)

        torrent_manager.rank_torrents(
            config["rtnSettings"],
            config["rtnRanking"],
            config["maxResultsPerResolution"],
            config["maxSize"],
            config["cachedOnly"],
            config["removeTrash"],
        )
        logger.log(
            "SCRAPER",
            f"⚖️  Torrents after RTN filtering: {len(torrent_manager.ranked_torrents)}/{initial_torrent_count}",
        )

        debrid_extension = get_debrid_extension(debrid_service)

        if (
            config["debridStreamProxyPassword"] != ""
            and settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            != config["debridStreamProxyPassword"]
        ):
            cached_results.append(
                {
                    "name": "[⚠️] Comet",
                    "description": "Debrid Stream Proxy Password incorrect.\nStreams will not be proxied.",
                    "url": "https://comet.fast",
                }
            )

        is_not_offcloud = debrid_service != "offcloud"
        result_season = season if season is not None else "n"
        result_episode = episode if episode is not None else "n"

        torrents = torrent_manager.torrents
        for info_hash in torrent_manager.ranked_torrents:
            torrent = torrents[info_hash]
            rtn_data = torrent["parsed"]

            debrid_emoji = (
                "🧲"
                if debrid_service == "torrent"
                else ("⚡" if torrent["cached"] else "⬇️")
            )

            the_stream = {
                "name": f"[{debrid_extension}{debrid_emoji}] Comet {rtn_data.resolution}",
                "description": format_title(
                    rtn_data,
                    torrent["title"],
                    torrent["seeders"],
                    torrent["size"],
                    torrent["tracker"],
                    config["resultFormat"],
                ),
                "behaviorHints": {
                    "bingeGroup": "comet|" + info_hash,
                    "videoSize": torrent["size"],
                    "filename": rtn_data.raw_title,
                },
            }

            if debrid_service == "torrent":
                the_stream["infoHash"] = info_hash
                the_stream["fileIdx"] = (
                    torrent["fileIndex"] if torrent["fileIndex"] is not None else 0
                )

                if len(torrent["sources"]) == 0:
                    the_stream["sources"] = trackers
                else:
                    the_stream["sources"] = torrent["sources"]
            else:
                the_stream["url"] = (
                    f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{info_hash}/{torrent['fileIndex'] if torrent['cached'] and is_not_offcloud else 'n'}/{title}/{result_season}/{result_episode}"
                )

            if torrent["cached"]:
                cached_results.append(the_stream)
            else:
                non_cached_results.append(the_stream)

        return {"streams": cached_results + non_cached_results}


@streams.get("/{b64config}/playback/{hash}/{index}/{name}/{season}/{episode}")
async def playback(
    request: Request,
    b64config: str,
    hash: str,
    index: str,
    name: str,
    season: str,
    episode: str,
):
    config = config_check(b64config)
    if not config:
        return FileResponse("comet/assets/invalidconfig.mp4")

    season = int(season) if season != "n" else None
    episode = int(episode) if episode != "n" else None

    async with aiohttp.ClientSession() as session:
        cached_link = await database.fetch_one(
            f"SELECT download_url FROM download_links_cache WHERE debrid_key = '{config['debridApiKey']}' AND info_hash = '{hash}' AND ((cast(:season as INTEGER) IS NULL AND season IS NULL) OR season = cast(:season as INTEGER)) AND ((cast(:episode as INTEGER) IS NULL AND episode IS NULL) OR episode = cast(:episode as INTEGER)) AND timestamp + 3600 >= :current_time",
            {
                "current_time": time.time(),
                "season": season,
                "episode": season,
            },
        )

        download_url = None
        if cached_link:
            download_url = cached_link["download_url"]

        ip = get_client_ip(request)
        if download_url is None:
            debrid = get_debrid(
                session,
                None,
                config["debridService"],
                config["debridApiKey"],
                ip,
            )
            download_url = await debrid.generate_download_link(
                hash, index, name, season, episode
            )
            if not download_url:
                return FileResponse("comet/assets/uncached.mp4")

            query = f"""
            INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
            INTO download_links_cache (debrid_key, info_hash, name, season, episode, download_url, timestamp)
            VALUES (:debrid_key, :info_hash, :name, :season, :episode, :download_url, :timestamp)
            {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
            """

            await database.execute(
                query,
                {
                    "debrid_key": config["debridApiKey"],
                    "info_hash": hash,
                    "name": name,
                    "season": season,
                    "episode": episode,
                    "download_url": download_url,
                    "timestamp": time.time(),
                },
            )

        if (
            settings.PROXY_DEBRID_STREAM
            and settings.PROXY_DEBRID_STREAM_PASSWORD
            == config["debridStreamProxyPassword"]
        ):
            return await custom_handle_stream_request(
                request.method,
                download_url,
                mediaflow_proxy.utils.http_utils.get_proxy_headers(request),
                media_id=hash,
                ip=ip,
            )

        return RedirectResponse(download_url, status_code=302)
