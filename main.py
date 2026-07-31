import os
import random
import re
import shutil
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List, Callable, Any

import aiohttp
from PIL import Image as PILImage
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Image, Reply, Face
try:
    from astrbot.api.message_components import MFace
except ImportError:
    MFace = None  # MFace 在新版 astrbot 中已移除
from astrbot.api import logger

# 导入图像处理模块
from . import image_utils

# ===== 本地 QQ 表情资源目录配置 =====
# 处理 [表情:123] 等系统表情时，优先直接读取本机 QQ 客户端缓存的
# sysface_res 目录下的 s{id}.png 文件，避免 CDN 下载、离线可用且速度更快。
# 插件通常运行在 WSL Ubuntu 中，Windows E 盘通过 /mnt/e/ 访问；
# 也可通过环境变量 NAILONG_LOCAL_FACE_DIR 自定义目录。
_LOCAL_FACE_DIR_ENV = "NAILONG_LOCAL_FACE_DIR"
_LOCAL_FACE_DIRS = [
    # WSL 下 Windows E 盘的挂载路径
    "/mnt/e/新建文件夹 (2)/nt_qq/global/nt_data/Emoji/emoji-resource/sysface_res/apng",
    # Windows 原生路径（正斜杠 / 反斜杠两种写法）
    "E:/新建文件夹 (2)/nt_qq/global/nt_data/Emoji/emoji-resource/sysface_res/apng",
    r"E:\新建文件夹 (2)\nt_qq\global\nt_data\Emoji\emoji-resource\sysface_res\apng",
]
# 本地表情文件可能的扩展名（目录名为 apng，实际文件为 png）
_LOCAL_FACE_EXTS = (".png", ".apng", ".gif")

@register("astrbot_plugin_nailong", "Logxk", "一个发送奶龙表情包的插件", "1.0.0")
class MemePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_dir = os.path.dirname(__file__)
        self.old_meme_dir = os.path.join(self.plugin_dir, "resources")
        data_dir = StarTools.get_data_dir()
        self.meme_dir = str(data_dir / "nailong_resources")
        self.temp_dir = str(data_dir / "nailong_temp")
        self._ensure_dirs()
        self._migrate_old_resources()
        self.meme_list = self._scan_memes()

        # 并发控制
        max_concurrent = int(os.getenv("NAILONG_MAX_CONCURRENT", 2))
        self.processing_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"并发限流已启用，最大同时处理数: {max_concurrent}")

        # 全局 HTTP 会话（复用连接池，禁用系统代理避免代理故障影响图片下载）
        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        self.session = aiohttp.ClientSession(
            connector=connector,
            trust_env=False,  # 忽略 HTTP_PROXY/HTTPS_PROXY 环境变量
        )

        # 后台清理任务
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())

    def _ensure_dirs(self):
        os.makedirs(self.meme_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

    def _migrate_old_resources(self):
        if os.path.exists(self.old_meme_dir) and os.listdir(self.old_meme_dir):
            os.makedirs(self.meme_dir, exist_ok=True)
            if not os.listdir(self.meme_dir):
                for file in os.listdir(self.old_meme_dir):
                    src = os.path.join(self.old_meme_dir, file)
                    dst = os.path.join(self.meme_dir, file)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                logger.info(f"已将旧资源从 {self.old_meme_dir} 迁移到 {self.meme_dir}")

    def _scan_memes(self):
        meme_files = []
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
        try:
            if os.path.exists(self.meme_dir):
                for file in os.listdir(self.meme_dir):
                    if file.lower().endswith(valid_extensions):
                        meme_files.append(file)
        except Exception as e:
            logger.error(f"扫描表情包文件夹时出错: {e}")
        return meme_files

    def _clean_temp_files(self, max_age_hours=1):
        """清理超过指定时间的临时文件（仅清理特定前缀的文件，避免误删处理中的文件）"""
        try:
            now = datetime.now().timestamp()
            for file in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, file)
                if os.path.isfile(file_path):
                    # 只清理以 local_ 或 download_ 开头的文件（即原始下载文件）
                    if file.startswith(('local_', 'download_')):
                        if now - os.path.getmtime(file_path) > max_age_hours * 3600:
                            os.remove(file_path)
                            logger.debug(f"清理临时文件: {file}")
        except Exception as e:
            logger.error(f"清理临时文件时出错: {e}")

    async def _periodic_cleanup(self):
        try:
            while True:
                await asyncio.sleep(3600)
                self._clean_temp_files()
        except asyncio.CancelledError:
            logger.info("后台清理任务被取消")
            raise

    def _get_mentioned_qq(self, event: AstrMessageEvent) -> Optional[str]:
        """从消息中提取第一个被 @ 的用户的 QQ 号（排除 @全体成员）"""
        for seg in event.message_obj.message:
            from astrbot.api.message_components import At
            if isinstance(seg, At):
                qq = getattr(seg, 'qq', None)
                if qq and str(qq) != 'all':
                    return str(qq)
        return None

    async def _download_avatar(self, qq: str) -> Optional[str]:
        """下载指定 QQ 号的头像到临时目录，返回文件路径"""
        avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"
        try:
            async with self.session.get(avatar_url) as resp:
                if resp.status == 200:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    dest = os.path.join(self.temp_dir, f"avatar_{qq}_{timestamp}.png")
                    with open(dest, 'wb') as f:
                        f.write(await resp.read())
                    if os.path.exists(dest) and os.path.getsize(dest) > 0:
                        return dest
        except Exception as e:
            logger.error(f"下载头像失败 (qq={qq}): {e}")
        return None

    async def _process_avatar_command(
        self,
        event: AstrMessageEvent,
        handler: Callable,
        output_prefix: str,
        *handler_args,
    ):
        """通用头像处理：优先处理被 @ 用户头像，其次发言者头像，最后要求引用图片
        handler 签名为 (input_path, output_path, *handler_args)"""
        event.stop_event()
        # 优先：被 @ 的用户
        mentioned_qq = self._get_mentioned_qq(event)
        if mentioned_qq:
            avatar_path = await self._download_avatar(mentioned_qq)
            if avatar_path:
                try:
                    timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                    output_path = os.path.join(self.temp_dir, f"{output_prefix}_{timestamp_out}.png")
                    async with self.processing_semaphore:
                        await asyncio.to_thread(handler, avatar_path, output_path, *handler_args)
                        if os.path.exists(output_path):
                            img_seg = Image.fromFileSystem(output_path)
                            yield event.chain_result([img_seg])
                        else:
                            yield event.plain_result("❌ 处理失败，输出文件未生成")
                except Exception as e:
                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                    yield event.plain_result(f"❌ 处理失败：{str(e)}")
                finally:
                    if os.path.exists(avatar_path):
                        os.remove(avatar_path)
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except:
                            pass
                return

        # 其次：发言者自己的头像
        user_id = self._get_user_id_from_event(event)
        if user_id:
            avatar_path = await self._download_avatar(user_id)
            if avatar_path:
                try:
                    timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                    output_path = os.path.join(self.temp_dir, f"{output_prefix}_{timestamp_out}.png")
                    async with self.processing_semaphore:
                        await asyncio.to_thread(handler, avatar_path, output_path, *handler_args)
                        if os.path.exists(output_path):
                            img_seg = Image.fromFileSystem(output_path)
                            yield event.chain_result([img_seg])
                        else:
                            yield event.plain_result("❌ 处理失败，输出文件未生成")
                except Exception as e:
                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                    yield event.plain_result(f"❌ 处理失败：{str(e)}")
                finally:
                    if os.path.exists(avatar_path):
                        os.remove(avatar_path)
                    if os.path.exists(output_path):
                        try:
                            os.remove(output_path)
                        except:
                            pass
                return

        # 兜底：要求引用图片
        yield event.plain_result("牛魔，你引用的图呢？")

    async def _extract_image_from_event(self, event: AstrMessageEvent) -> Optional[Image]:
        """从QQ消息事件中提取图像或QQ表情（黄脸、互动、商城表情包等）
        
        策略：
        1. 先尝试从消息链中提取 Image/Face/MFace
        2. 再从 raw_message 中提取被框架忽略的 mface 段（商城表情包）
        3. 若仍未找到，通过 OneBot get_msg API 获取被引用消息中的 mface
        """
        # 先尝试同步提取（消息链 + raw_message）
        result = self._extract_image_from_event_sync(event)
        if result is not None:
            return result
        
        # 最后尝试：通过 OneBot API 获取被引用消息中的 mface
        return await self._resolve_mface_from_reply(event)
    
    def _extract_image_from_event_sync(self, event: AstrMessageEvent) -> Optional[Image]:
        """同步提取：消息链 + raw_message（不含 API 调用）"""
        face_id = None
        face_url = None
        
        # 第一步：遍历已解析的消息链
        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for sub in seg.chain:
                    if isinstance(sub, Image):
                        return sub
                    elif isinstance(sub, Face):
                        face_id = sub.id
                    elif MFace is not None and isinstance(sub, MFace):
                        face_url = sub.url if hasattr(sub, 'url') and sub.url else None
                        mface_id = getattr(sub, 'id', None)
                        if mface_id is not None:
                            try:
                                face_id = int(mface_id)
                            except (ValueError, TypeError):
                                pass
            elif isinstance(seg, Image):
                return seg
            elif isinstance(seg, Face):
                face_id = seg.id
            elif MFace is not None and isinstance(seg, MFace):
                # MFace 自带 url（适配器已解析）
                face_url = seg.url if hasattr(seg, 'url') and seg.url else None
                mface_id = getattr(seg, 'id', None)
                if mface_id is not None:
                    try:
                        face_id = int(mface_id)
                    except (ValueError, TypeError):
                        pass
        
        # 第二步：从 raw_message 中提取被框架过滤掉的 mface（商城表情包）
        raw_mface_url, raw_mface_id = self._extract_mface_from_raw(event)
        if not face_url:
            face_url = raw_mface_url
        # raw_message 中的 face_id 优先（因为包含商城表情 ID）
        if raw_mface_id is not None:
            face_id = raw_mface_id
        
        # 处理 mface（QQ商城表情包）：URL 优先，face_id 作回退
        if face_url and face_id is not None:
            # 同时有 URL 和 face_id：URL 优先，face_id URL 作回退
            fallback_urls = self._get_qq_face_urls(face_id)
            logger.info(f"mface 商城表情: id={face_id}, 主URL={face_url[:80]}, 回退URL数={len(fallback_urls)}")
            return self._make_image_from_url(face_url, fallback_urls=fallback_urls)
        
        if face_url:
            # 仅有 URL，无 face_id
            logger.info(f"mface 仅有URL, 无face_id: {face_url[:80]}")
            return self._make_image_from_url(face_url)
        
        # 处理 Face（QQ黄脸/互动表情/商城表情通过 face_id）
        if face_id is not None:
            # 优先使用本地表情文件（QQ 客户端缓存的 sysface_res），避免 CDN 下载
            local_img = self._make_image_from_local_face(face_id)
            if local_img is not None:
                return local_img
            # 本地无对应文件，回退到 CDN URL
            qq_face_urls = self._get_qq_face_urls(face_id)
            if qq_face_urls:
                return self._make_image_from_url(qq_face_urls[0], fallback_urls=qq_face_urls[1:])

        # 兜底：从 message_str 中解析 [表情:123] 文本表情
        # （当适配器未将表情解析为 Face 段、仅以文本形式保留时）
        local_img = self._extract_local_face_from_text(event)
        if local_img is not None:
            return local_img

        return None
    
    async def _resolve_mface_from_reply(self, event: AstrMessageEvent) -> Optional[Image]:
        """通过 OneBot get_msg API 获取被引用消息中的 mface（商城表情）
        
        当消息链和 raw_message 都无法提取到 mface 时（例如 aiocqhttp 适配器
        不在 reply 段中内嵌原始消息），通过 API 主动获取被引用的原始消息。
        """
        # 1. 提取 reply message_id
        reply_id = self._get_reply_message_id(event)
        if not reply_id:
            logger.debug("无法提取 reply message_id，跳过 API 查询")
            return None
        
        # 2. 调用 OneBot get_msg API
        try:
            bot = getattr(event, 'bot', None)
            api = getattr(bot, 'api', None)
            call_action = getattr(api, 'call_action', None)
            if not callable(call_action):
                logger.debug("无法获取 OneBot call_action，跳过 API 查询")
                return None
            
            logger.info(f"尝试通过 get_msg API 获取被引用消息: message_id={reply_id}")
            result = await call_action('get_msg', message_id=str(reply_id))
        except Exception as e:
            logger.warning(f"调用 get_msg API 失败: {e}")
            return None
        
        if not isinstance(result, dict):
            logger.debug(f"get_msg 返回非 dict 类型: {type(result)}")
            return None
        
        # 3. 解析响应中的 message 数组，查找 mface
        data = result.get('data', result)  # get_msg 响应通常在 data 字段中
        if isinstance(data, dict):
            message_array = data.get('message', data.get('message_list', []))
        elif isinstance(data, list):
            message_array = data
        else:
            logger.debug(f"get_msg 响应格式未知: {type(result)}")
            return None
        
        if not message_array:
            logger.debug("get_msg 响应中无 message 数组")
            return None
        
        # 4. 在消息段中搜索 mface
        url, face_id = self._search_mface_in_segments(message_array)
        if url is None and face_id is None:
            logger.debug("get_msg 响应中未找到 mface 段")
            return None
        
        logger.info(f"通过 get_msg API 找到商城表情: id={face_id}, url={url[:80] if url else '无'}")
        
        # 5. 构造 Image（与 sync 方法相同的逻辑）
        if url and face_id is not None:
            fallback_urls = self._get_qq_face_urls(face_id)
            return self._make_image_from_url(url, fallback_urls=fallback_urls)
        elif url:
            return self._make_image_from_url(url)
        elif face_id is not None:
            qq_face_urls = self._get_qq_face_urls(face_id)
            if qq_face_urls:
                return self._make_image_from_url(qq_face_urls[0], fallback_urls=qq_face_urls[1:])
        
        return None
    
    def _get_reply_message_id(self, event: AstrMessageEvent) -> Optional[str]:
        """从事件中提取 reply 的 message_id"""
        # 方式1：从已解析的 Reply 组件中获取
        for seg in event.message_obj.message:
            if isinstance(seg, Reply):
                reply_id = getattr(seg, 'id', None)
                if reply_id:
                    return str(reply_id)
        
        # 方式2：从 raw_message 的 reply 段中获取
        raw = getattr(event.message_obj, 'raw_message', None)
        if raw:
            message_array = getattr(raw, 'message', None) or getattr(raw, 'message_list', None)
            if message_array:
                for msg_seg in message_array:
                    seg_type = msg_seg.get('type', '') if isinstance(msg_seg, dict) else getattr(msg_seg, 'type', '')
                    if seg_type == 'reply':
                        seg_data = msg_seg.get('data', {}) if isinstance(msg_seg, dict) else getattr(msg_seg, 'data', {})
                        reply_id = seg_data.get('id', None) if isinstance(seg_data, dict) else None
                        if reply_id:
                            return str(reply_id)
        
        return None
    
    def _extract_mface_from_raw(self, event: AstrMessageEvent) -> Tuple[Optional[str], Optional[int]]:
        """从 raw_message 中提取 mface 的 URL 和 face_id（包括被框架忽略的段和额外数据）
        
        支持递归搜索 reply 段中嵌套的原始消息内容。
        
        Returns:
            (url, face_id) — url 为 CDN 直接地址，face_id 用于构造回退 URL
        """
        raw = getattr(event.message_obj, 'raw_message', None)
        if not raw:
            logger.debug("raw_message 为空，无法提取 mface")
            return None, None
        
        message_array = getattr(raw, 'message', None) or getattr(raw, 'message_list', None)
        if not message_array:
            # 记录 raw 的类型和属性用于调试
            logger.debug(f"raw_message 无 message/message_list 字段, type={type(raw).__name__}, attrs={[a for a in dir(raw) if not a.startswith('_')]}")
            return None, None
        
        return self._search_mface_in_segments(message_array)
    
    def _search_mface_in_segments(self, segments: list) -> Tuple[Optional[str], Optional[int]]:
        """递归搜索消息段列表中的 mface/face 数据
        
        支持场景：
        - 直接发送商城表情：mface 在顶层段中
        - 引用商城表情后发命令：mface 嵌套在 reply 段的 data.message 中
        """
        if not segments:
            return None, None
        
        for msg_seg in segments:
            seg_data = msg_seg.get('data', {}) if isinstance(msg_seg, dict) else getattr(msg_seg, 'data', {})
            seg_type = msg_seg.get('type', '') if isinstance(msg_seg, dict) else getattr(msg_seg, 'type', '')
            
            if not isinstance(seg_data, dict):
                seg_data = getattr(msg_seg, '__dict__', {}) if hasattr(msg_seg, '__dict__') else {}
            
            # mface（QQ商城表情）：提取 url 和 id
            if seg_type == 'mface':
                url = seg_data.get('url', '')
                face_id = seg_data.get('id', None)
                # id 可能是 int 或 str
                if face_id is not None:
                    try:
                        face_id = int(face_id)
                    except (ValueError, TypeError):
                        face_id = None
                logger.info(f"从 raw_message 提取到 mface: id={face_id}, url={url[:80] if url else '无'}")
                return url if url else None, face_id
            
            # face 段可能附带 url（LLBot 等实现）
            if seg_type == 'face':
                if 'url' in seg_data and seg_data['url']:
                    url = seg_data['url']
                    face_id = seg_data.get('id', None)
                    if face_id is not None:
                        try:
                            face_id = int(face_id)
                        except (ValueError, TypeError):
                            face_id = None
                    logger.info(f"从 raw_message face 段提取到: id={face_id}, url={url[:80]}")
                    return url, face_id
                # 记录 face 段完整数据用于调试
                logger.debug(f"raw_message face 段数据: {seg_data}")
            
            # reply 段：递归搜索嵌套的原始消息内容（引用商城表情后发命令的场景）
            if seg_type == 'reply':
                # 不同 OneBot 实现可能使用不同的字段名嵌套原始消息
                nested_msg = (
                    seg_data.get('message', None)
                    or seg_data.get('message_list', None)
                    or seg_data.get('messages', None)
                )
                if nested_msg and isinstance(nested_msg, list) and len(nested_msg) > 0:
                    logger.debug(f"递归搜索 reply 段中的嵌套消息: {len(nested_msg)} 段")
                    url, face_id = self._search_mface_in_segments(nested_msg)
                    if url is not None or face_id is not None:
                        return url, face_id
        
        return None, None

    def _get_local_face_dir(self) -> Optional[str]:
        """返回本地 QQ 表情资源目录（存在且为目录），不存在返回 None"""
        env_dir = os.getenv(_LOCAL_FACE_DIR_ENV, "").strip()
        if env_dir and os.path.isdir(env_dir):
            return env_dir
        for d in _LOCAL_FACE_DIRS:
            if os.path.isdir(d):
                return d
        return None

    def _get_local_face_path(self, face_id) -> Optional[str]:
        """根据表情 ID 返回本地表情文件路径 s{id}.png，本地无对应文件时返回 None"""
        if face_id is None:
            return None
        try:
            face_id = int(face_id)
        except (ValueError, TypeError):
            return None
        if face_id < 0:
            return None
        face_dir = self._get_local_face_dir()
        if not face_dir:
            return None
        for ext in _LOCAL_FACE_EXTS:
            path = os.path.join(face_dir, f"s{face_id}{ext}")
            if os.path.isfile(path):
                return path
        return None

    def _make_image_from_local_face(self, face_id) -> Optional[Image]:
        """创建指向本地表情文件的 Image 对象（file 为本地绝对路径），本地无对应文件时返回 None"""
        local_path = self._get_local_face_path(face_id)
        if not local_path:
            return None
        logger.info(f"使用本地表情文件: id={face_id}, path={local_path}")
        img = Image(file=local_path)
        img.path = local_path
        return img

    def _extract_local_face_from_text(self, event: AstrMessageEvent) -> Optional[Image]:
        """从 message_str 中解析 [表情:123] 格式的文本表情，并尝试使用本地表情文件"""
        message_str = getattr(event, 'message_str', '') or ''
        match = re.search(r'\[表情:(\d+)\]', message_str)
        if not match:
            return None
        face_id = int(match.group(1))
        return self._make_image_from_local_face(face_id)

    def _get_qq_face_urls(self, face_id: int):
        """返回QQ表情的候选URL列表（按优先级排列）
        
        支持：
        - QQ黄脸表情 (0-246): qzonestyle CDN
        - QQ互动/大表情 (0-500): gxh.vip.qq.com
        - QQ商城表情 (任意ID): gxh.vip.qq.com + 其他回退
        """
        if face_id < 0:
            return []
        urls = []
        
        # 经典黄脸表情 (0-246) 专用 CDN（GIF 格式）
        if face_id <= 246:
            urls.append(f"https://qzonestyle.gtimg.cn/qzone/em/e{face_id}.gif")
        
        # QQ商城表情 / 大表情 通用 CDN（支持任意 ID）
        urls.append(f"https://gxh.vip.qq.com/emojigxh/show?id={face_id}")
        
        # 大表情备用参数（>246 时尝试 t=1 参数）
        if face_id > 246:
            urls.append(f"https://gxh.vip.qq.com/emojigxh/show?id={face_id}&t=1")
        
        # 旧版 CDN 回退
        if face_id <= 500:
            urls.append(f"https://face.qq.com/scripts/face/qqface/{face_id}.gif")
        
        # 商城表情额外回退源
        if face_id > 246:
            urls.append(f"https://gxh.vip.qq.com/emojigxh/preview?id={face_id}")
        
        return urls



    _URL_SEP = "|||"  # URL 回退列表分隔符

    def _make_image_from_url(self, url: str, fallback_urls: list = None) -> Image:
        """从URL创建一个Image对象。
        
        通过特殊分隔符合并多个URL到 img.url 中，
        _download_image_to_temp 会依次尝试直到成功。
        """
        img = Image(file="")
        if fallback_urls:
            # 用分隔符合并所有 URL
            img.url = self._URL_SEP.join([url] + list(fallback_urls))
        else:
            img.url = url
        return img

    def _get_user_id_from_event(self, event: AstrMessageEvent) -> Optional[str]:
        """从事件对象中提取用户ID"""
        user_id = None
        
        # 方式1: 从 session_id 提取 (格式: platform:user_id@...)
        session_id = getattr(event, 'session_id', None)
        if session_id:
            session_str = str(session_id)
            if ':' in session_str:
                parts = session_str.split(':')
                if len(parts) >= 2:
                    user_id = parts[1].split('@')[0]
        
        # 方式2: 直接 user_id 属性
        if not user_id:
            user_id = getattr(event, 'user_id', None)
        
        # 方式3: 从 message_obj.sender 获取
        if not user_id:
            try:
                message_obj = getattr(event, 'message_obj', None)
                if message_obj:
                    sender = getattr(message_obj, 'sender', None)
                    if sender:
                        user_id = getattr(sender, 'user_id', None)
            except:
                pass
        
        # 方式4: 从 message_obj 直接获取 user_id
        if not user_id:
            try:
                message_obj = getattr(event, 'message_obj', None)
                if message_obj:
                    user_id = getattr(message_obj, 'user_id', None)
            except:
                pass
        
        logger.debug(f"获取用户ID - session_id: {getattr(event, 'session_id', 'N/A')}, user_id_attr: {getattr(event, 'user_id', 'N/A')}, result: {user_id}")
        return user_id

    async def _download_image_to_temp(self, image: Image) -> Tuple[Optional[str], Optional[str]]:
        """
        将图片组件保存到临时文件夹，并立即创建安全副本。
        返回 (安全副本路径, 错误信息)。安全副本将用于后续处理，原始下载文件可被清理。
        """
        # 优先处理本地文件（file 属性）
        if hasattr(image, 'file') and image.file:
            raw_name = image.file
            safe_name = os.path.basename(raw_name)
            # 候选路径列表（基于框架数据目录）
            data_dir = StarTools.get_data_dir()
            image_cache_dir = data_dir / "image"
            candidates = [
                raw_name if os.path.isabs(raw_name) else None,
                str(image_cache_dir / raw_name),
                str(image_cache_dir / safe_name),
                os.path.join(self.meme_dir, safe_name),
                safe_name,
            ]
            # 过滤掉 None 并去重
            seen = set()
            unique_candidates = []
            for p in candidates:
                if p and p not in seen:
                    seen.add(p)
                    unique_candidates.append(p)

            for path in unique_candidates:
                if path and os.path.exists(path):
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ext = os.path.splitext(safe_name)[1]
                    # 原始下载文件（可被清理）
                    raw_dest = os.path.join(self.temp_dir, f"local_{timestamp}{ext}")
                    try:
                        shutil.copy2(path, raw_dest)
                        if not os.path.exists(raw_dest):
                            raise IOError(f"复制后文件不存在: {raw_dest}")
                        logger.debug(f"本地文件复制成功: {path} -> {raw_dest}")
                        # 立即创建安全副本
                        safe_copy = self._create_safe_copy(raw_dest, prefix="safe_")
                        if safe_copy:
                            return safe_copy, None
                        else:
                            # 若安全副本创建失败，删除原始文件并返回错误
                            os.remove(raw_dest)
                            return None, "创建安全副本失败"
                    except Exception as e:
                        logger.error(f"复制本地文件失败 {path} -> {raw_dest}: {e}")
                        continue  # 尝试下一个候选路径

        # 处理网络图片（url 属性），支持 ||| 分隔的多URL回退
        if hasattr(image, 'url') and image.url:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # 支持 ||| 分隔的多URL回退
            urls_to_try = image.url.split(self._URL_SEP) if hasattr(self, '_URL_SEP') else [image.url]
            
            last_error = None
            for try_url in urls_to_try:
                try:
                    # QQ CDN 可能需要合适的 User-Agent 和 Referer
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Referer': 'https://qun.qq.com/',
                    }
                    async with self.session.get(try_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status != 200:
                            last_error = f"HTTP {resp.status}"
                            logger.debug(f"表情URL返回非200: {try_url[:80]} -> {resp.status}")
                            continue
                        content_type = resp.headers.get('Content-Type', '')
                        # QQ CDN 有时返回 text/html 但内容是图片，信任 URL 扩展名
                        ext_map = {
                            'image/jpeg': '.jpg',
                            'image/png': '.png',
                            'image/gif': '.gif',
                            'image/webp': '.webp',
                        }
                        ext = ext_map.get(content_type.split(';')[0].strip(), '.png')
                        # 也根据URL扩展名推断（QQ CDN 有时 Content-Type 不准）
                        url_lower = try_url.lower()
                        if url_lower.endswith('.gif'):
                            ext = '.gif'
                        elif url_lower.endswith('.jpg') or url_lower.endswith('.jpeg'):
                            ext = '.jpg'
                        elif url_lower.endswith('.webp'):
                            ext = '.webp'
                        # 原始下载文件
                        raw_dest = os.path.join(self.temp_dir, f"download_{timestamp}{ext}")
                        with open(raw_dest, 'wb') as f:
                            f.write(await resp.read())
                        if not os.path.exists(raw_dest) or os.path.getsize(raw_dest) == 0:
                            raise IOError(f"下载文件无效: {raw_dest}")
                        logger.debug(f"网络图片下载成功: {try_url} -> {raw_dest}")
                        # 立即创建安全副本
                        safe_copy = self._create_safe_copy(raw_dest, prefix="safe_")
                        if safe_copy:
                            return safe_copy, None
                        else:
                            os.remove(raw_dest)
                            return None, "创建安全副本失败"
                except Exception as e:
                    last_error = str(e)
                    continue
            if last_error and "404" in str(last_error):
                return None, "该商城表情暂不支持下载，请尝试其他表情或使用普通图片"
            return None, f"下载表情失败: {last_error}"

        return None, "图片组件缺少 file 或 url 属性，无法获取图片"


    async def _download_qq_face(self, face_url: str) -> Tuple[Optional[str], Optional[str]]:
        """下载QQ表情并保存到临时目录
        
        Args:
            face_url: 表情图片URL
            
        Returns:
            (保存路径, 错误信息)
        """
        if not face_url:
            return None, "无效的表情URL"
        
        try:
            output_file = os.path.join(self.temp_dir, f"qq_face_{hash(face_url) % 10000}.png")
            
            async with self.session.get(face_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    with open(output_file, 'wb') as f:
                        f.write(await resp.read())
                    return output_file, None
                else:
                    return None, f"下载失败: HTTP {resp.status}"
        except asyncio.TimeoutError:
            return None, "下载超时"
        except Exception as e:
            return None, f"下载错误: {str(e)}"

    def _create_safe_copy(self, src_path: str, prefix: str = "copy_") -> Optional[str]:
        """创建文件安全副本，返回副本路径，失败返回 None"""
        try:
            if not os.path.exists(src_path):
                logger.error(f"源文件不存在，无法创建副本: {src_path}")
                return None
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # 增加微秒避免冲突
            ext = os.path.splitext(src_path)[1]
            dst_path = os.path.join(self.temp_dir, f"{prefix}{timestamp}{ext}")
            shutil.copy2(src_path, dst_path)
            if os.path.exists(dst_path):
                logger.debug(f"安全副本创建成功: {dst_path}")
                return dst_path
            else:
                logger.error(f"副本创建失败: {src_path} -> {dst_path}")
                return None
        except Exception as e:
            logger.error(f"创建副本异常: {e}")
            return None

    async def _process_image_command(
        self,
        event: AstrMessageEvent,
        handler: Callable[[str, str], None],
        need_animated: bool = False,
        fail_msg: str = "处理失败",
        force_ext: Optional[str] = None,
        **kwargs
    ):
        """
        通用图片命令处理辅助方法
        :param event: 消息事件
        :param handler: 处理函数，接受 (input_path, output_path) 无返回值，失败时抛出异常
        :param need_animated: 是否要求图片为动图
        :param fail_msg: 失败时的文本提示（仅当 handler 不抛出异常且返回 False 时使用，现异常机制下基本无用）
        :param force_ext: 强制输出文件扩展名（例如 ".gif"）
        :param kwargs: 额外参数，用于构造输出文件名
        """
        event.stop_event()
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            yield event.plain_result("牛魔，你引用的图呢？")
            return

        safe_file_path, error = await self._download_image_to_temp(target_image)
        if error:
            yield event.plain_result(error)
            return
        if not safe_file_path:
            yield event.plain_result("无法获取图片文件")
            return

        # 详细记录文件状态
        logger.debug(f"安全副本路径: {safe_file_path}")
        if not os.path.exists(safe_file_path):
            logger.error(f"安全副本在开始时丢失: {safe_file_path}")
            yield event.plain_result("❌ 图片文件丢失，请重试")
            return
        file_size = os.path.getsize(safe_file_path)
        logger.debug(f"文件大小: {file_size} 字节")

        # 动图验证（GIF 或 APNG 均可）
        if need_animated:
            if not image_utils.is_animated_gif(safe_file_path):
                yield event.plain_result("请提供一个有效的动图（GIF 或 APNG）")
                return

        # 清理过期临时文件（仅清理原始下载文件，不影响安全副本）
        self._clean_temp_files()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if force_ext:
            ext = force_ext
        elif image_utils.is_gif(safe_file_path):
            ext = ".gif"
        else:
            ext = ".png"
        name_parts = [str(v) for v in kwargs.values() if v]
        name_parts.append(timestamp)
        output_filename = "_".join(name_parts) + ext
        output_path = os.path.join(self.temp_dir, output_filename)

        try:
            async with self.processing_semaphore:
                # 使用安全副本进行处理
                await asyncio.to_thread(handler, safe_file_path, output_path)
                # 检查输出文件是否生成
                if not os.path.exists(output_path):
                    raise RuntimeError("处理完成但输出文件未生成")
                img_seg = Image.fromFileSystem(output_path)
                yield event.chain_result([img_seg])
        except Exception as e:
            logger.error(f"处理图片时出错: {e}", exc_info=True)
            yield event.plain_result(f"❌ 处理失败：{str(e)}")
        finally:
            # 清理安全副本
            try:
                if os.path.exists(safe_file_path):
                    os.remove(safe_file_path)
                    logger.debug(f"已删除安全副本: {safe_file_path}")
            except Exception as e:
                logger.error(f"删除安全副本失败: {e}")

    @filter.regex(r'(?:\s|^)(左对称)(?:\s|$)')
    async def left_symmetric(self, event: AstrMessageEvent):
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                lambda inp, out: image_utils.apply_symmetry_to_static(inp, out, 'left'),
                "left"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.apply_symmetry_to_gif(inp, out, 'left')
                             if image_utils.is_gif(inp)
                             else image_utils.apply_symmetry_to_static(inp, out, 'left'),
            need_animated=False,
            cmd="left"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(右对称)(?:\s|$)')
    async def right_symmetric(self, event: AstrMessageEvent):
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                lambda inp, out: image_utils.apply_symmetry_to_static(inp, out, 'right'),
                "right"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.apply_symmetry_to_gif(inp, out, 'right')
                             if image_utils.is_gif(inp)
                             else image_utils.apply_symmetry_to_static(inp, out, 'right'),
            need_animated=False,
            cmd="right"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(上对称)(?:\s|$)')
    async def top_symmetric(self, event: AstrMessageEvent):
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                lambda inp, out: image_utils.apply_symmetry_to_static(inp, out, 'top'),
                "top"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.apply_symmetry_to_gif(inp, out, 'top')
                             if image_utils.is_gif(inp)
                             else image_utils.apply_symmetry_to_static(inp, out, 'top'),
            need_animated=False,
            cmd="top"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(下对称)(?:\s|$)')
    async def bottom_symmetric(self, event: AstrMessageEvent):
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                lambda inp, out: image_utils.apply_symmetry_to_static(inp, out, 'bottom'),
                "bottom"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.apply_symmetry_to_gif(inp, out, 'bottom')
                             if image_utils.is_gif(inp)
                             else image_utils.apply_symmetry_to_static(inp, out, 'bottom'),
            need_animated=False,
            cmd="bottom"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(奶龙)(?:\s|$|\d)?')
    async def send_meme(self, event: AstrMessageEvent):
        event.stop_event()
        if not self.meme_list:
            yield event.plain_result("暂时还没有表情包，快往 resources 文件夹里放一些图片吧~")
            return
        message_str = event.message_str.strip()
        count = 1
        match = re.search(r'^(\d+)\s*奶龙$|^奶龙\s*(\d+)$', message_str)
        if match:
            count_str = match.group(1) or match.group(2)
            if count_str:
                count = int(count_str)
        max_count = 10
        if count > max_count:
            yield event.plain_result(f"一次最多只能发送 {max_count} 张表情包哦~")
            return
        if count > len(self.meme_list):
            count = len(self.meme_list)
            yield event.plain_result(f"目前只有 {len(self.meme_list)} 张表情包，将全部发送~")
        chosen_memes = random.sample(self.meme_list, count)
        img_segments = [Image.fromFileSystem(os.path.join(self.meme_dir, f)) for f in chosen_memes]
        yield event.chain_result(img_segments)

    @filter.regex(r'(?:\s|^)(变速)(?:\s|$|\d)')
    async def speed_gif(self, event: AstrMessageEvent):
        event.stop_event()
        message_str = event.message_str.strip()
        speed_match = re.search(r'变速\s*(\d+(?:\.\d+)?)', message_str)
        if not speed_match:
            yield event.plain_result("请指定速度倍数，例如：/变速 2（2倍速）或 /变速 0.5（半速）")
            return
        speed_factor = float(speed_match.group(1))
        if speed_factor < 0.1:
            speed_factor = 0.1
            yield event.plain_result("速度不能低于0.1倍，已自动调整为0.1倍")
        elif speed_factor > 10:
            speed_factor = 10
            yield event.plain_result("速度不能高于10倍，已自动调整为10倍")

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.adjust_gif_speed(inp, speed_factor, out),
            need_animated=True,
            fail_msg="GIF处理失败，请确保文件是有效的GIF动图格式",
            cmd="speed",
            factor=str(speed_factor).replace('.', '_')
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(倒放)(?:\s|$)')
    async def reverse_gif(self, event: AstrMessageEvent):
        async for ret in self._process_image_command(
            event,
            image_utils.reverse_gif,
            need_animated=True,
            fail_msg="GIF倒放失败，请确保文件是有效的GIF动图格式",
            cmd="reverse"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(反色)(?:\s|$)')
    async def invert_image(self, event: AstrMessageEvent):
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.invert_image,
                "invert"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.invert_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.invert_image(inp, out),
            need_animated=False,
            cmd="invert"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(旋转)(?:\s|$)')
    async def rotate_image(self, event: AstrMessageEvent):
        """旋转图片，支持自定义角度，默认顺时针90°"""
        event.stop_event()
        message_str = event.message_str.strip()
        
        # 剔除 @mention，避免 QQ 号被误识别为角度
        clean_msg = re.sub(r'@\S+', '', message_str)
        
        # 解析角度：仅匹配紧邻"旋转"的数字（旋转180 / 旋转 90）
        angle_match = re.search(r'旋转\s*(\d+(?:\.\d+)?)', clean_msg)
        if angle_match:
            angle_str = angle_match.group(1)
            if '.' in angle_str:
                yield event.plain_result("我不会")
                return
            angle = int(angle_str)
            if angle < 0 or angle > 360:
                yield event.plain_result("我不会")
                return
        else:
            angle = 90
        
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.rotate_image,
                "rotate",
                angle,
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.rotate_gif(inp, out, angle)
                             if image_utils.is_gif(inp)
                             else image_utils.rotate_image(inp, out, angle),
            need_animated=False,
            cmd="rotate",
            angle=str(angle).replace('.', '_')
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(镜像)(?:\s|$)')
    async def mirror_image_cmd(self, event: AstrMessageEvent):
        """对图片进行水平镜像翻转"""
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.mirror_image,
                "mirror"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.mirror_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.mirror_image(inp, out),
            need_animated=False,
            cmd="mirror"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(左右平移)(?:\s|$)')
    async def shuffle_image_cmd(self, event: AstrMessageEvent):
        """对图片进行左右平移处理，图像从左往右再往左来回往复移动，共30帧"""
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.shuffle_image,
                "shuffle"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.shuffle_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.shuffle_image(inp, out),
            need_animated=False,
            cmd="shuffle",
            force_ext=".gif"
        ):
            yield ret


    @filter.regex(r'(?:\s|^)(左右横跳)(?:\s|$)')
    async def bounce_image_cmd(self, event: AstrMessageEvent):
        """对图片进行左右横跳处理，图像左右振动，共12帧"""
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.bounce_image,
                "bounce"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.bounce_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.bounce_image(inp, out),
            need_animated=False,
            cmd="bounce",
            force_ext=".gif"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(万花镜)(?:\s|$)')
    async def kaleidoscope_cmd(self, event: AstrMessageEvent):
        """万花镜：将图片处理为万花镜样式（八方向×五层放射铺排）。
        将原图等比缩小，沿八个方向（上/右上/右/右下/下/左下/左/左上）各向外铺开 5 张
        （共 40 张），图像由内到外逐层增大、允许部分重叠、外侧大图覆盖内侧小图，
        每张按所在方向对应的角度旋转，其余区域用透明填充。
        引用图片时处理引用图，未引用时默认处理目标用户头像（优先@用户，其次发言者）"""
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.kaleidoscope_image,
                "kaleidoscope"
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.kaleidoscope_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.kaleidoscope_image(inp, out),
            need_animated=False,
            cmd="kaleidoscope"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(?:(\d{1,2})\s*)?(万花筒)(?:\s*(\d{1,2}))?(?:\s|$)')
    async def tube_cmd(self, event: AstrMessageEvent):
        """万花筒：将图片处理为万花筒样式（圆形扇区镜像）。
        支持 6-24 的偶数面镜子：万花筒12 / 16万花筒 / 8万花筒 等，默认12面。
        引用图片时处理引用图，未引用时默认处理目标用户头像（优先@用户，其次发言者）"""
        message_str = event.message_str.strip()

        # 解析面数：从消息中提取数字（支持6-24的偶数）
        match = re.search(r'(?:^|\s)(\d{1,2})\s*万花筒|万花筒\s*(\d{1,2})(?:\s|$)', message_str)
        segments = 12  # 默认12面
        if match:
            num_str = match.group(1) or match.group(2)
            num = int(num_str)
            if 6 <= num <= 24:
                if num % 2 == 0:
                    segments = num
                else:
                    yield event.plain_result(f"❌ 万花筒仅支持偶数面（6-24），不支持奇数 {num} 哦～")
                    return
            else:
                yield event.plain_result(f"❌ 万花筒面数仅支持 6-24 范围，{num} 不在范围内～")
                return

        target_image = await self._extract_image_from_event(event)
        if not target_image:
            async for ret in self._process_avatar_command(
                event,
                image_utils.tube_image,
                "tube",
                segments,
            ):
                yield ret
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out, seg=segments: image_utils.tube_gif(inp, out, segments=seg)
                             if image_utils.is_gif(inp)
                             else image_utils.tube_image(inp, out, segments=seg),
            need_animated=False,
            cmd="tube"
        ):
            yield ret

    @filter.regex(r'(?:\s|^)(添加)(?:\s|$)')
    async def add_meme(self, event: AstrMessageEvent):
        # 权限检查：仅允许主人（2406873379）使用
        if str(event.get_sender_id()) != "2406873379":
            yield event.plain_result("❌ 你没有权限使用此命令哦~只有主人才能添加表情包！")
            return
        event.stop_event()
        target_image = await self._extract_image_from_event(event)
        if not target_image:
            yield event.plain_result("请发送一张图片，或引用一张包含图片的消息。\n使用方法：/添加 [图片]")
            return

        # 对于添加命令，我们只需要原始文件，不需要安全副本（因为直接复制到资源目录）
        file_path, error = await self._download_raw_image(target_image)
        if error:
            yield event.plain_result(error)
            return
        if not file_path:
            yield event.plain_result("无法获取图片文件")
            return

        if not os.path.exists(file_path):
            logger.error(f"添加时文件丢失: {file_path}")
            yield event.plain_result("❌ 图片文件已丢失，请重试")
            return

        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            ext = os.path.splitext(file_path)[1]
            if not ext:
                ext = '.jpg'
            filename = f"meme_{timestamp}{ext}"
            target_path = os.path.join(self.meme_dir, filename)
            shutil.copy2(file_path, target_path)
            self.meme_list = self._scan_memes()
            yield event.plain_result(f"✅ 表情包添加成功！\n文件名：{filename}\n当前共有 {len(self.meme_list)} 个表情包。")
        except Exception as e:
            logger.error(f"添加表情包时出错: {e}")
            yield event.plain_result(f"❌ 添加失败：{str(e)}")

    async def _download_raw_image(self, image: Image) -> Tuple[Optional[str], Optional[str]]:
        """仅下载原始文件到临时目录，不创建安全副本（用于添加命令）"""
        # 复用之前的下载逻辑，但返回原始路径
        if hasattr(image, 'file') and image.file:
            raw_name = image.file
            safe_name = os.path.basename(raw_name)
            data_dir = StarTools.get_data_dir()
            image_cache_dir = data_dir / "image"
            candidates = [
                raw_name if os.path.isabs(raw_name) else None,
                str(image_cache_dir / raw_name),
                str(image_cache_dir / safe_name),
                os.path.join(self.meme_dir, safe_name),
                safe_name,
            ]
            seen = set()
            unique_candidates = []
            for p in candidates:
                if p and p not in seen:
                    seen.add(p)
                    unique_candidates.append(p)

            for path in unique_candidates:
                if path and os.path.exists(path):
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ext = os.path.splitext(safe_name)[1]
                    dest = os.path.join(self.temp_dir, f"add_{timestamp}{ext}")
                    try:
                        shutil.copy2(path, dest)
                        if os.path.exists(dest):
                            return dest, None
                    except Exception as e:
                        logger.error(f"复制本地文件失败 {path} -> {dest}: {e}")
                        continue

        if hasattr(image, 'url') and image.url:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            try:
                async with self.session.get(image.url) as resp:
                    if resp.status != 200:
                        return None, f"下载失败，HTTP {resp.status}"
                    content_type = resp.headers.get('Content-Type', '')
                    ext_map = {
                        'image/jpeg': '.jpg',
                        'image/png': '.png',
                        'image/gif': '.gif',
                        'image/webp': '.webp',
                    }
                    ext = ext_map.get(content_type.split(';')[0], '.jpg')
                    dest = os.path.join(self.temp_dir, f"add_{timestamp}{ext}")
                    with open(dest, 'wb') as f:
                        f.write(await resp.read())
                    if os.path.exists(dest) and os.path.getsize(dest) > 0:
                        return dest, None
            except Exception as e:
                return None, f"下载异常: {str(e)}"

        return None, "图片组件缺少 file 或 url 属性，无法获取图片"

    async def terminate(self):
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        # 关闭 HTTP 会话
        if hasattr(self, 'session') and not self.session.closed:
            await self.session.close()
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"清理临时文件夹时出错: {e}")