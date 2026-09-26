from typing import Optional, List, Tuple
from astrbot.api import logger
from urllib.parse import urlparse
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from datetime import datetime
import random
import aiofiles
import aiofiles.os
import aiohttp
import errno
import shutil
import asyncio
import json
import os


ONE_DAY_IN_SECONDS = 86400


class ResourceManager:
    """
    资源管理器，负责用户头像和背景图片的获取、缓存和管理
    """

    def __init__(self, plugin_config, plugin_name: Optional[str] = None) -> None:
        self._http_timeout = aiohttp.ClientTimeout(total=5)  # 设置请求超时时间为5秒
        self._connection_limit = aiohttp.TCPConnector(limit=10)  # 限制并发连接数为10
        self._session = aiohttp.ClientSession(
            timeout=self._http_timeout, connector=self._connection_limit
        )
        self.plugin_config = plugin_config
        self.name = plugin_name or "astrbot_plugin_jrysprpr"

        self.avatar_cache_expiration = self.plugin_config.get(
            "avatar_cache_expiration", ONE_DAY_IN_SECONDS
        )  # 默认一天过期

        # 初始化jrys数据

        self.is_data_loaded = False

        self._storage_initialized = False
        self._plugin_data_dir: Optional[Path] = None
        self._background_cache_dir: Optional[Path] = None
        self._background_tmp_dir: Optional[Path] = None
        self._precache_task: Optional[asyncio.Task] = None

        self.data_dir = os.path.dirname(os.path.abspath(__file__))
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.background_dir = os.path.join(self.data_dir, "backgroundFolder")
        self.font_dir = os.path.join(self.data_dir, "font")

        self._http_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
        }

    async def get_background_image(self) -> Optional[Tuple[str, bool]]:
        """
        从 backgrounds.json 图片源随机获取背景图片。
        支持的图源格式（与 astrbot_plugin_fortnue 相同）：
        - "名字": ["url1", "url2", ...]            直链列表，随机挑一个
        - "名字": "url"                             单条直链
        - "名字": {"type": "api", "url": ..., "token": "data.0.urls.regular", ...}
                                                    请求 API，从 JSON 里按 token 提取图片地址
        - "名字": {"type": "object", "sources": [...]}  备选源列表
        返回 (图片路径, 是否需要清理)。
        """
        try:
            self._ensure_storage_dirs()

            sources = await self._load_backgrounds_data()
            if not sources:
                logger.warning("backgrounds.json 中没有可用的图片源")
                return None

            ignored = set(self.plugin_config.get("ignored_sources", []) or [])
            weights = self._get_source_weights()

            candidates = [k for k in sources.keys() if k not in ignored]
            if not candidates:
                logger.warning("backgrounds.json 中的图片源全部被忽略")
                return None

            # 最多尝试 3 个不同图源，避免个别图源失效导致整体失败
            tried: set = set()
            for _ in range(min(3, len(candidates))):
                remaining = [c for c in candidates if c not in tried]
                source_key = self._weighted_choice(remaining, weights)
                tried.add(source_key)

                result = await self._fetch_from_source(source_key, sources[source_key])
                if result:
                    return result

            logger.warning("已从多个图片源尝试，均获取背景图失败")
            return None

        except Exception as e:
            logger.error(f"获取背景图片时出错: {e}")
            return None

    async def _load_backgrounds_data(self) -> dict:
        """读取插件目录下的 backgrounds.json（带 mtime 缓存，改了文件自动重载）。"""
        path = os.path.join(self.data_dir, "backgrounds.json")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            logger.warning(f"未找到图片源文件: {path}")
            return {}

        cached = getattr(self, "_backgrounds_cache", None)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                data = json.loads(await f.read())
            if not isinstance(data, dict):
                logger.error("backgrounds.json 必须是 JSON 对象")
                return {}
            self._backgrounds_cache = (mtime, data)
            logger.info(f"读取图片源文件: {path}（{len(data)} 个图源）")
            return data
        except json.JSONDecodeError as e:
            logger.error(f"backgrounds.json 不是有效的 JSON: {e}")
            return {}
        except Exception as e:
            logger.error(f"读取 backgrounds.json 失败: {e}")
            return {}

    async def _fetch_from_source(self, name: str, spec) -> Optional[Tuple[str, bool]]:
        """从单个图源获取背景图。"""
        urls: List[str] = []

        if isinstance(spec, str) and spec:
            urls = [spec]
        elif isinstance(spec, list) and spec:
            urls = [u for u in spec if isinstance(u, str)]
            random.shuffle(urls)
            urls = urls[:5]  # 列表型图源最多试 5 条
        elif isinstance(spec, dict):
            t = spec.get("type")
            if t == "api":
                url = await self._resolve_api_image_url(name, spec)
                urls = [url] if url else []
            elif t == "object":
                items = spec.get("sources") or []
                if items:
                    item = random.choice(items)
                    if isinstance(item, str):
                        urls = [item]
                    elif isinstance(item, dict) and item.get("url"):
                        urls = [item["url"]]
            elif t == "array":
                items = [u for u in (spec.get("items") or spec.get("urls") or []) if isinstance(u, str)]
                random.shuffle(items)
                urls = items[:5]
            else:
                # 没写 type 的 dict，按 api 处理
                if spec.get("url"):
                    url = await self._resolve_api_image_url(name, spec)
                    urls = [url] if url else []

        for url in urls:
            if not url.startswith(("http://", "https://")):
                continue
            result = await self._fetch_bg_url(url)
            if result:
                return result

        logger.warning(f"图源 {name} 获取背景图失败")
        return None

    async def _fetch_bg_url(self, image_url: str) -> Optional[Tuple[str, bool]]:
        """下载单张背景图（带持久化缓存和临时清理逻辑）。"""
        cache_path = self._background_cache_path_for_url(image_url)

        # 已缓存则直接返回（持久化缓存不做清理）
        if cache_path.exists():
            return str(cache_path), False

        pre_cache_enabled = bool(
            self.plugin_config.get("pre_cache_background_images", False)
        )
        cleanup_downloads = bool(
            self.plugin_config.get("cleanup_background_downloads", True)
        )

        # 未启用预缓存时：默认按需下载后清理；关闭开关则仍然写入持久化缓存目录
        image_path = cache_path
        should_cleanup = False
        if (not pre_cache_enabled) and cleanup_downloads:
            image_path = self._background_tmp_path_for_url(image_url)
            should_cleanup = True

        ok = await self._download_to_path(image_url, image_path, label="背景图")
        if ok:
            logger.info(f"下载图片成功: {image_url}")
            return str(image_path), should_cleanup
        return None

    async def _resolve_api_image_url(self, name: str, spec: dict) -> Optional[str]:
        """解析 api 类型图源：请求接口，从 JSON 中按 token 提取图片地址。"""
        url = spec.get("url")
        if not url:
            return None

        # expected == "image" 表示接口直接返回图片，URL 本身就是最终图片地址
        expected = str(spec.get("expected", "url")).lower()
        if expected == "image":
            return url

        method = str(spec.get("method", "get")).upper()
        headers = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
        req_headers = {**self._http_headers, **headers}

        try:
            async with self._session.request(
                method, url, headers=req_headers
            ) as resp:
                if resp.status < 200 or resp.status >= 300:
                    logger.error(
                        f"图源 {name} API 请求失败: HTTP {resp.status} | {url}"
                    )
                    return None
                js = await resp.json(content_type=None)

            token = spec.get("token") or ""
            img_url = self._extract_token_value(js, token)
            if isinstance(img_url, list):
                img_url = random.choice(img_url) if img_url else None
            if not isinstance(img_url, str) or not img_url:
                logger.error(
                    f"图源 {name} 的 token '{token}' 未解析到有效的图片 URL"
                )
                return None
            return img_url
        except Exception as e:
            logger.error(f"解析图源 {name} 的 API 失败: {e}")
            return None

    @staticmethod
    def _extract_token_value(obj, token: str):
        """按 "data.0.urls.regular" 这种路径从 JSON 对象里取值。"""
        if not isinstance(token, str) or not token:
            return obj
        cur = obj
        for part in [p for p in token.split(".") if p]:
            if isinstance(cur, list):
                if not cur:
                    return None
                if part.isdigit() and int(part) < len(cur):
                    cur = cur[int(part)]
                    continue
                cur = random.choice(cur)
            elif isinstance(cur, dict):
                if part in cur:
                    cur = cur[part]
                else:
                    return None
            else:
                return None
        return cur

    def _get_source_weights(self) -> dict:
        """从配置获取图源权重映射，格式: ["ba:2.0", "miku:1.0"]"""
        weights = {}
        for item in self.plugin_config.get("source_weights", []) or []:
            if isinstance(item, str) and ":" in item:
                name, _, w = item.partition(":")
                try:
                    weight = float(w.strip())
                    if weight > 0:
                        weights[name.strip()] = weight
                except ValueError:
                    pass
        return weights

    @staticmethod
    def _weighted_choice(choices: list, weights: dict) -> str:
        """加权随机选择"""
        if not choices:
            return ""
        choice_weights = [weights.get(c, 1.0) for c in choices]
        total = sum(choice_weights)
        if total <= 0:
            return random.choice(choices)
        r = random.uniform(0, total)
        cumsum = 0
        for choice, weight in zip(choices, choice_weights):
            cumsum += weight
            if r <= cumsum:
                return choice
        return choices[-1]
    async def get_avatar_img(self, user_id: str, event=None) -> Optional[str]:
        """
        获取用户头像（多平台适配）
          1. 根据消息来源平台解析头像 URL
          2. 下载头像 3. 返回头像的路径
        Args:
            user_id (str): 用户 ID
            event: AstrMessageEvent，用于判断平台并获取平台相关数据

        Returns:
            str: 头像的路径，获取不到时返回 None（由调用方使用默认头像兜底）
        """
        try:
            self._ensure_storage_dirs()
            platform = ""
            if event is not None:
                try:
                    platform = event.get_platform_name() or ""
                except Exception:
                    platform = ""
            # 不同平台的 user_id 可能撞号，缓存文件名带平台前缀
            cache_key = f"{platform}_{user_id}" if platform else str(user_id)
            avatar_path = os.path.join(self.avatar_dir, f"{cache_key}.jpg")
            # 检查头像是否存在
            if await aiofiles.os.path.exists(avatar_path):

                def _file_stat(path):
                    try:
                        st = os.stat(path)
                        return st.st_mtime
                    except FileNotFoundError:
                        return None

                file_mtime = await asyncio.to_thread(_file_stat, avatar_path)
                file_age = datetime.now().timestamp() - file_mtime
                if (
                    file_age < self.avatar_cache_expiration
                ):  # 默认如果头像文件小于一天，则不下载
                    return avatar_path

            url = await self._resolve_avatar_url(event, platform, str(user_id))
            if not url:
                logger.info(
                    f"平台 {platform or '未知'} 无法获取用户 {user_id} 的头像，将使用默认头像"
                )
                return None

            ok = await self._download_to_path(url, Path(avatar_path), label="头像")
            if ok:
                return avatar_path
            return None

        except Exception as e:
            logger.error(f"获取用户头像失败: {e}")
            return None

    async def _resolve_avatar_url(
        self, event, platform: str, user_id: str
    ) -> Optional[str]:
        """
        按平台解析用户头像 URL：
        - aiocqhttp(OneBot v11): QQ 头像直链
        - qqofficial(QQ 官方机器人): 尝试从频道消息的 author.avatar 获取
          （群聊/C2C 官方接口不提供头像，返回 None 走默认头像）
        - telegram: 通过 Bot API getUserProfilePhotos 获取
        - 其他平台: 尝试按 QQ 号直链获取（兼容旧行为）
        """
        if platform in ("aiocqhttp", "onebot", "onebot11") or not platform:
            return f"http://q.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        if platform == "qqofficial":
            return self._resolve_qqofficial_avatar(event)

        if platform == "telegram":
            return await self._resolve_telegram_avatar(event, user_id)

        # 未知平台兜底：仍按 QQ 头像直链尝试
        return f"http://q.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    @staticmethod
    def _resolve_qqofficial_avatar(event) -> Optional[str]:
        """QQ 官方机器人：仅频道消息作者可能带有 avatar 字段。"""
        try:
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            author = getattr(raw, "author", None)
            avatar = getattr(author, "avatar", None) if author else None
            if avatar and str(avatar).startswith(("http://", "https://")):
                return str(avatar)
        except Exception as e:
            logger.warning(f"解析 QQ 官方机器人头像失败: {e}")
        return None

    async def _resolve_telegram_avatar(self, event, user_id: str) -> Optional[str]:
        """Telegram：通过 Bot API 获取用户头像文件下载链接。"""
        try:
            bot = getattr(event, "bot", None)
            # AstrBot Telegram 平台中 event.bot 可能是 Application，需取其 .bot
            app_bot = getattr(bot, "bot", None)
            if app_bot is not None:
                bot = app_bot
            if bot is None or not hasattr(bot, "get_user_profile_photos"):
                return None

            photos = await bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            if (
                not photos
                or getattr(photos, "total_count", 0) == 0
                or not getattr(photos, "photos", None)
            ):
                return None

            # 取最大尺寸的一张
            sizes = list(photos.photos[0])
            biggest = max(sizes, key=lambda p: getattr(p, "file_size", 0) or 0)
            tg_file = await bot.get_file(biggest.file_id)
            file_path = getattr(tg_file, "file_path", None)
            token = getattr(bot, "token", None)
            if token and file_path:
                return f"https://api.telegram.org/file/bot{token}/{file_path}"
            # python-telegram-bot 的 File 对象本身也提供完整 URL
            full_url = getattr(tg_file, "file_url", None)
            if full_url:
                return str(full_url)
        except Exception as e:
            logger.warning(f"解析 Telegram 头像失败: {e}")
        return None

    async def initialize(self):
        """插件加载/重载后执行（适合做缓存预热等异步任务）。"""
        self._ensure_storage_dirs()

        if self.plugin_config.get("pre_cache_background_images", False):
            self._start_background_precache()

    def _migrate_legacy_cache_dir(
        self, legacy_dir: Path, target_dir: Path, label: str
    ) -> None:
        """将旧版本缓存目录迁移到标准插件数据目录。"""
        try:
            if not legacy_dir.exists() or not legacy_dir.is_dir():
                return

            legacy_resolved = legacy_dir.resolve()
            target_resolved = target_dir.resolve()
            if legacy_resolved == target_resolved:
                return

            target_dir.mkdir(parents=True, exist_ok=True)

            moved = 0
            skipped = 0
            replaced = 0
            failed = 0

            for item in legacy_dir.iterdir():
                if not item.is_file():
                    continue

                dest = target_dir / item.name
                try:
                    if dest.exists():
                        try:
                            src_stat = item.stat()
                            dest_stat = dest.stat()
                            if src_stat.st_mtime <= dest_stat.st_mtime:
                                item.unlink(missing_ok=True)
                                skipped += 1
                                continue
                        except Exception:
                            item.unlink(missing_ok=True)
                            skipped += 1
                            continue

                        replaced += 1

                    try:
                        os.replace(item, dest)
                    except OSError as e:
                        if e.errno == errno.EXDEV:
                            shutil.copy2(item, dest)
                            item.unlink(missing_ok=True)
                        else:
                            raise

                    moved += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"迁移{label}缓存失败: {item} -> {dest} | {e}")

            try:
                if not any(legacy_dir.iterdir()):
                    legacy_dir.rmdir()
            except Exception:
                pass

            if moved or replaced or skipped or failed:
                logger.info(
                    f"{label}缓存迁移完成: "
                    f"from={legacy_dir} to={target_dir} "
                    f"moved={moved} replaced={replaced} skipped={skipped} failed={failed}"
                )
        except Exception as e:
            logger.warning(f"{label}缓存迁移异常: {e}")

    def _ensure_storage_dirs(self) -> None:
        """初始化插件大文件缓存目录（优先 data/plugin_data/{plugin_name}）。"""
        if self._storage_initialized:
            return

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            plugin_name = (
                self.name or getattr(self, "name", None) or "astrbot_plugin_jrysprpr"
            )
            data_root = get_astrbot_data_path()
            data_root_path = (
                data_root if isinstance(data_root, Path) else Path(str(data_root))
            )
            plugin_data_dir = data_root_path / "plugin_data" / plugin_name
            plugin_data_dir.mkdir(parents=True, exist_ok=True)

            self._plugin_data_dir = plugin_data_dir

            cache_dir = plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)
            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            # 缓存目录分类：avatars / background_images / background_images_tmp
            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            # 迁移旧版本缓存目录（插件目录 / 旧 plugin_data 结构 / 旧 fallback 结构）
            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
                plugin_data_dir / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, target_avatar_dir, label="头像"
                )

            legacy_background_dirs = [
                Path(self.background_dir) / "images",  # 旧 fallback 结构
                Path(self.data_dir) / "background_images",
                plugin_data_dir / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.background_dir) / "images_tmp",  # 旧 fallback 结构
                Path(self.data_dir) / "background_images_tmp",
                plugin_data_dir / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True
            logger.info(f"插件数据目录初始化完成: {plugin_data_dir}")
        except Exception as e:
            # 兼容：若无法获取 AstrBot 数据目录，则回退到插件目录
            logger.warning(f"初始化插件数据目录失败，将回退到插件目录缓存: {e}")
            self._plugin_data_dir = Path(self.data_dir)

            cache_dir = self._plugin_data_dir / "cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._background_cache_dir = cache_dir / "background_images"
            self._background_cache_dir.mkdir(parents=True, exist_ok=True)
            self._background_tmp_dir = cache_dir / "background_images_tmp"
            self._background_tmp_dir.mkdir(parents=True, exist_ok=True)

            target_avatar_dir = cache_dir / "avatars"
            self.avatar_dir = str(target_avatar_dir)
            os.makedirs(self.avatar_dir, exist_ok=True)

            legacy_avatar_dirs = [
                Path(self.data_dir) / "avatars",
            ]
            for legacy_dir in legacy_avatar_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, target_avatar_dir, label="头像"
                )

            legacy_background_dirs = [
                Path(self.background_dir) / "images",
                Path(self.data_dir) / "background_images",
            ]
            for legacy_dir in legacy_background_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_cache_dir, label="背景图"
                )

            legacy_background_tmp_dirs = [
                Path(self.background_dir) / "images_tmp",
                Path(self.data_dir) / "background_images_tmp",
            ]
            for legacy_dir in legacy_background_tmp_dirs:
                self._migrate_legacy_cache_dir(
                    legacy_dir, self._background_tmp_dir, label="背景图临时"
                )

            self._storage_initialized = True

    def _start_background_precache(self) -> None:
        """启动后台预缓存任务（不会阻塞插件加载/重载）。"""
        if self._precache_task and not self._precache_task.done():
            return
        self._precache_task = asyncio.create_task(self._pre_cache_background_images())

    def _background_cache_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_cache_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self._background_cache_dir / f"{digest}{ext}"

    def _background_tmp_path_for_url(self, url: str) -> Path:
        self._ensure_storage_dirs()
        assert self._background_tmp_dir is not None

        parsed = urlparse(url)
        ext = os.path.splitext(parsed.path)[1].lower()
        if not ext or len(ext) > 10:
            ext = ".img"
        return self._background_tmp_dir / f"{uuid4().hex}{ext}"

    async def _download_to_path(
        self, url: str, dest: Path, label: str = "图片", retries: int = 1
    ) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)
        retries = max(0, int(retries))

        for attempt in range(retries + 1):
            status: Optional[int] = None
            reason = ""
            tmp_path = dest.parent / f"{dest.name}.{uuid4().hex}.tmp"

            try:
                async with self._session.get(
                    url, headers=self._http_headers
                ) as response:
                    status = response.status
                    reason = (response.reason or "").strip()

                    if status < 200 or status >= 300:
                        # 5xx 可能是临时问题，允许重试；其它状态码直接失败
                        if 500 <= status <= 599 and attempt < retries:
                            logger.warning(
                                f"{label}下载失败({attempt + 1}/{retries + 1}): HTTP {status} {reason} | {url}"
                            )
                            continue

                        logger.error(f"{label}下载失败: HTTP {status} {reason} | {url}")
                        return False

                    # 流式写入，避免一次性读入内存
                    async with aiofiles.open(tmp_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            await f.write(chunk)

                await asyncio.to_thread(os.replace, tmp_path, dest)
                return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}Timeout | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(f"{label}下载失败: {http_info}Timeout | {url}")
            except aiohttp.ClientPayloadError as e:
                msg = str(e).strip()
                # 该类错误通常带有较长的内部异常信息，保持简短即可
                if ":" in msg:
                    msg = msg.split(":", 1)[0].strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except aiohttp.ClientError as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            except Exception as e:
                msg = str(e).strip()
                if len(msg) > 200:
                    msg = msg[:200] + "..."
                http_info = f"HTTP {status} {reason} | " if status is not None else ""
                if attempt < retries:
                    logger.warning(
                        f"{label}下载失败({attempt + 1}/{retries + 1}): {http_info}{type(e).__name__}: {msg} | {url}"
                    )
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                logger.error(
                    f"{label}下载失败: {http_info}{type(e).__name__}: {msg} | {url}"
                )
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass

        return False

    async def _collect_all_background_urls(self) -> List[str]:
        """收集 backgrounds.json 中所有静态直链（api 类型图源无法预知结果，跳过）。"""
        sources = await self._load_backgrounds_data()

        urls: set[str] = set()

        def _add(u):
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                urls.add(u)

        for spec in sources.values():
            if isinstance(spec, str):
                _add(spec)
            elif isinstance(spec, list):
                for u in spec:
                    _add(u)
            elif isinstance(spec, dict):
                t = spec.get("type")
                if t == "object":
                    for item in spec.get("sources") or []:
                        if isinstance(item, str):
                            _add(item)
                        elif isinstance(item, dict):
                            _add(item.get("url"))
                elif t == "array":
                    for u in spec.get("items") or spec.get("urls") or []:
                        _add(u)
                elif spec.get("type") is None and spec.get("expected") == "image":
                    _add(spec.get("url"))
                elif t == "api" and str(spec.get("expected", "")).lower() == "image":
                    # api 直接返回图片时 URL 本身就是图片地址
                    _add(spec.get("url"))

        return sorted(urls)

    async def _pre_cache_background_images(self) -> None:
        self._ensure_storage_dirs()

        urls = await self._collect_all_background_urls()
        total = len(urls)
        if total == 0:
            logger.warning("预缓存背景图：未找到任何图片 URL")
            return

        try:
            concurrency = int(self.plugin_config.get("pre_cache_concurrency", 3))
        except Exception:
            concurrency = 3
        concurrency = max(1, min(concurrency, 10))

        already_cached = 0
        to_download: List[Tuple[str, Path]] = []
        for url in urls:
            dest = self._background_cache_path_for_url(url)
            if dest.exists():
                already_cached += 1
            else:
                to_download.append((url, dest))

        logger.info(
            f"预缓存背景图开始: total={total}, cached={already_cached}, download={len(to_download)}, concurrency={concurrency}"
        )

        if hasattr(self, "put_kv_data"):
            try:
                await self.put_kv_data(
                    "bg_cache_status",
                    {
                        "status": "running",
                        "total": total,
                        "cached": already_cached,
                        "download": len(to_download),
                        "started_at": datetime.now().isoformat(),
                    },
                )
            except Exception as e:
                logger.warning(f"写入 KV 缓存状态失败: {e}")

        sem = asyncio.Semaphore(concurrency)

        async def _dl(url: str, dest: Path) -> bool:
            if dest.exists():
                return True
            async with sem:
                if dest.exists():
                    return True
                return await self._download_to_path(url, dest, label="背景图")

        downloaded = 0
        failed = 0
        cancelled = False
        try:
            results = await asyncio.gather(
                *(_dl(url, dest) for url, dest in to_download),
                return_exceptions=True,
            )
            for r in results:
                if r is True:
                    downloaded += 1
                else:
                    # False 或 Exception 都算失败（个别 URL 可能已失效）
                    failed += 1
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if hasattr(self, "put_kv_data"):
                try:
                    await self.put_kv_data(
                        "bg_cache_status",
                        {
                            "status": "cancelled" if cancelled else "done",
                            "total": total,
                            "cached": already_cached,
                            "download": len(to_download),
                            "downloaded": downloaded,
                            "failed": failed,
                            "ended_at": datetime.now().isoformat(),
                        },
                    )
                except Exception as e:
                    logger.warning(f"写入 KV 缓存状态失败: {e}")

        logger.info(
            f"预缓存背景图完成: total={total}, cached={already_cached}, downloaded={downloaded}, failed={failed}"
        )

    async def _load_jrys_data(self) -> dict:
        """
        初始化 jrys.json 文件
        1. 检查当前目录下是否存在 jrys.json 文件
        2. 如果不存在，则创建一个空的 jrys.json 文件
        3. 如果存在，则读取文件内容
        4. 如果文件内容不是有效的 JSON 格式，则打印错误信息
        """

        if self.is_data_loaded:
            return self.jrys_data

        jrys_path = os.path.join(self.data_dir, "jrys.json")

        # 检查 jrys.json 文件是否存在,如果不存在，则创建一个空的 jrys.json 文件
        if not os.path.exists(jrys_path):
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps({}))
                logger.info(f"创建空的运势数据文件: {jrys_path}")

        # 读取 JSON 文件
        try:
            async with aiofiles.open(jrys_path, "r", encoding="utf-8") as f:
                content = await f.read()
                # json.loads是CPU密集型，用 to_thread 包装
                self.jrys_data = await asyncio.to_thread(json.loads, content)
                self.is_data_loaded = True  # 标记数据已加载
                logger.info(f"读取运势数据文件: {jrys_path}")

            return self.jrys_data

        except FileNotFoundError:
            logger.error(f"文件 {jrys_path} 没找到")
            return {}
        except json.JSONDecodeError:
            logger.error(f"文件 {jrys_path} 不是有效的 JSON 格式")
            return {}

    async def _save_jrys_data(self):
        """保存 jrys 数据到 jrys.json"""
        jrys_path = os.path.join(self.data_dir, "jrys.json")
        try:
            async with aiofiles.open(jrys_path, "w", encoding="utf-8") as f:
                content = await asyncio.to_thread(
                    json.dumps, self.jrys_data, ensure_ascii=False, indent=4
                )
                await f.write(content)
        except Exception as e:
            logger.error(f"保存运势数据失败: {e}")
