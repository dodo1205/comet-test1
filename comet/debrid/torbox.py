import aiohttp
import asyncio

from RTN import parse

from comet.utils.general import is_video
from comet.utils.logger import logger


class TorBox:
    def __init__(self, session: aiohttp.ClientSession, debrid_api_key: str, ip: str):
        session.headers["Authorization"] = f"Bearer {debrid_api_key}"
        self.session = session
        self.proxy = None

        self.api_url = "https://api.torbox.app/v1/api"
        self.debrid_api_key = debrid_api_key

    async def check_premium(self):
        try:
            check_premium = await self.session.get(
                f"{self.api_url}/user/me?settings=false"
            )
            check_premium = await check_premium.text()
            if '"success":true' in check_premium:
                return True
        except Exception as e:
            logger.warning(f"Exception while checking premium status on TorBox: {e}")

        return False

    async def get_instant(self, chunk: list):
        try:
            response = await self.session.get(
                f"{self.api_url}/torrents/checkcached?hash={','.join(chunk)}&format=list&list_files=true"
            )
            return await response.json()
        except Exception as e:
            logger.warning(
                f"Exception while checking hash instant availability on TorBox: {e}"
            )

    async def get_availability(self, torrent_hashes: list):
        chunk_size = 50
        chunks = [
            torrent_hashes[i : i + chunk_size]
            for i in range(0, len(torrent_hashes), chunk_size)
        ]

        tasks = []
        for chunk in chunks:
            tasks.append(self.get_instant(chunk))

        responses = await asyncio.gather(*tasks)

        availability = [response for response in responses if response is not None]

        files = []
        for result in availability:
            if not result["success"] or not result["data"]:
                continue

            for torrent in result["data"]:
                torrent_files = torrent["files"]
                for file in torrent_files:
                    filename = file["name"].split("/")[1]

                    if not is_video(filename):
                        continue

                    if "sample" in filename.lower():
                        continue

                    filename_parsed = parse(filename)

                    files.append(
                        {
                            "info_hash": torrent["hash"],
                            "index": torrent_files.index(file),
                            "title": filename,
                            "size": file["size"],
                            "season": filename_parsed.seasons[0]
                            if len(filename_parsed.seasons) != 0
                            else None,
                            "episode": filename_parsed.episodes[0]
                            if len(filename_parsed.episodes) != 0
                            else None,
                        }
                    )

        return files
