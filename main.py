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
from astrbot.api.message_components import Image, Reply
from astrbot.api import logger

# 导入图像处理模块
from . import image_utils

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

        # 全局 HTTP 会话（复用连接池）
        self.session = aiohttp.ClientSession()

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

    def _extract_image_from_event(self, event: AstrMessageEvent) -> Optional[Image]:
        for seg in event.message_obj.message:
            if isinstance(seg, Reply) and seg.chain:
                for sub in seg.chain:
                    if isinstance(sub, Image):
                        return sub
            elif isinstance(seg, Image):
                return seg
        return None
    
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

        # 处理网络图片（url 属性）
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
                    # 原始下载文件
                    raw_dest = os.path.join(self.temp_dir, f"download_{timestamp}{ext}")
                    with open(raw_dest, 'wb') as f:
                        f.write(await resp.read())
                    if not os.path.exists(raw_dest) or os.path.getsize(raw_dest) == 0:
                        raise IOError(f"下载文件无效: {raw_dest}")
                    logger.debug(f"网络图片下载成功: {image.url} -> {raw_dest}")
                    # 立即创建安全副本
                    safe_copy = self._create_safe_copy(raw_dest, prefix="safe_")
                    if safe_copy:
                        return safe_copy, None
                    else:
                        os.remove(raw_dest)
                        return None, "创建安全副本失败"
            except Exception as e:
                return None, f"下载异常: {str(e)}"

        return None, "图片组件缺少 file 或 url 属性，无法获取图片"

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
        target_image = self._extract_image_from_event(event)
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

        # 动图验证
        if need_animated:
            if not image_utils.is_animated_gif(safe_file_path):
                yield event.plain_result("请提供一个有效的 GIF 动图")
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

    @filter.command("左对称")
    async def left_symmetric(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"left_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.apply_symmetry_to_static, safe_file_path, output_path, 'left')
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("获取头像失败，请重试")
                        else:
                            yield event.plain_result("获取头像失败，请引用一张图片")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("获取头像失败，请引用一张图片")
            else:
                yield event.plain_result("无法获取用户信息，请引用一张图片")
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

    @filter.command("右对称")
    async def right_symmetric(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"right_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.apply_symmetry_to_static, safe_file_path, output_path, 'right')
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("获取头像失败，请重试")
                        else:
                            yield event.plain_result("获取头像失败，请引用一张图片")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("获取头像失败，请引用一张图片")
            else:
                yield event.plain_result("无法获取用户信息，请引用一张图片")
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

    @filter.command("上对称")
    async def top_symmetric(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"top_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.apply_symmetry_to_static, safe_file_path, output_path, 'top')
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("获取头像失败，请重试")
                        else:
                            yield event.plain_result("获取头像失败，请引用一张图片")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("获取头像失败，请引用一张图片")
            else:
                yield event.plain_result("无法获取用户信息，请引用一张图片")
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

    @filter.command("下对称")
    async def bottom_symmetric(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"bottom_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.apply_symmetry_to_static, safe_file_path, output_path, 'bottom')
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("获取头像失败，请重试")
                        else:
                            yield event.plain_result("获取头像失败，请引用一张图片")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("获取头像失败，请引用一张图片")
            else:
                yield event.plain_result("无法获取用户信息，请引用一张图片")
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

    @filter.command("奶龙")
    async def send_meme(self, event: AstrMessageEvent):
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

    @filter.command("变速")
    async def speed_gif(self, event: AstrMessageEvent):
        message_str = event.message_str.strip()
        speed_match = re.search(r'(\d+(?:\.\d+)?)', message_str)
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

    @filter.command("倒放")
    async def reverse_gif(self, event: AstrMessageEvent):
        async for ret in self._process_image_command(
            event,
            image_utils.reverse_gif,
            need_animated=True,
            fail_msg="GIF倒放失败，请确保文件是有效的GIF动图格式",
            cmd="reverse"
        ):
            yield ret

    @filter.command("反色")
    async def invert_image(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            ext = ".png"
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}{ext}")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"invert_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.invert_image, safe_file_path, output_path)
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像反色时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("获取头像失败，请重试")
                        else:
                            yield event.plain_result("牛魔，你引用的图呢？")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("牛魔，你引用的图呢？")
            else:
                yield event.plain_result("牛魔，你引用的图呢？")
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

    @filter.command("旋转")
    async def rotate_image(self, event: AstrMessageEvent):
        """旋转图片，支持自定义角度，默认顺时针90°"""
        message_str = event.message_str.strip()
        
        # 解析角度
        angle_match = re.search(r'(\d+(?:\.\d+)?)', message_str)
        if angle_match:
            angle_str = angle_match.group(1)
            # 检查是否为整数
            if '.' in angle_str:
                yield event.plain_result("我不会")
                return
            angle = int(angle_str)
            # 检查是否在0-360之间
            if angle < 0 or angle > 360:
                yield event.plain_result("我不会")
                return
        else:
            angle = 90  # 默认顺时针90°
        
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"rotate_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.rotate_image, safe_file_path, output_path, angle)
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像旋转时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("牛魔，你引用的图呢？")
                        else:
                            yield event.plain_result("牛魔，你引用的图呢？")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("牛魔，你引用的图呢？")
            else:
                yield event.plain_result("牛魔，你引用的图呢？")
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

    @filter.command("镜像")
    async def mirror_image_cmd(self, event: AstrMessageEvent):
        """对图片进行水平镜像翻转"""
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"mirror_{timestamp_out}.png")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.mirror_image, safe_file_path, output_path)
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像镜像时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("牛魔，你引用的图呢？")
                        else:
                            yield event.plain_result("牛魔，你引用的图呢？")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("牛魔，你引用的图呢？")
            else:
                yield event.plain_result("牛魔，你引用的图呢？")
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

    @filter.command("左右平移")
    async def shuffle_image_cmd(self, event: AstrMessageEvent):
        """对图片进行左右平移处理，图像从左往右再往左来回往复移动，共30帧"""
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"shuffle_{timestamp_out}.gif")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.shuffle_image, safe_file_path, output_path)
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像左右平移时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("牛魔，你引用的图呢？")
                        else:
                            yield event.plain_result("牛魔，你引用的图呢？")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("牛魔，你引用的图呢？")
            else:
                yield event.plain_result("牛魔，你引用的图呢？")
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.shuffle_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.shuffle_image(inp, out),
            need_animated=False,
            cmd="shuffle",
            force_ext=".gif"  # 强制输出为 GIF 格式
        ):
            yield ret


    @filter.command("左右横跳")
    async def bounce_image_cmd(self, event: AstrMessageEvent):
        """对图片进行左右横跳处理，图像左右振动，共12帧"""
        target_image = self._extract_image_from_event(event)
        if not target_image:
            user_id = self._get_user_id_from_event(event)
            if user_id:
                avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
                try:
                    async with self.session.get(avatar_url) as resp:
                        if resp.status == 200:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            safe_file_path = os.path.join(self.temp_dir, f"avatar_{timestamp}.png")
                            with open(safe_file_path, 'wb') as f:
                                f.write(await resp.read())
                            if os.path.exists(safe_file_path) and os.path.getsize(safe_file_path) > 0:
                                timestamp_out = datetime.now().strftime("%Y%m%d_%H%M%S")
                                output_path = os.path.join(self.temp_dir, f"bounce_{timestamp_out}.gif")
                                try:
                                    async with self.processing_semaphore:
                                        await asyncio.to_thread(image_utils.bounce_image, safe_file_path, output_path)
                                        if os.path.exists(output_path):
                                            img_seg = Image.fromFileSystem(output_path)
                                            yield event.chain_result([img_seg])
                                        else:
                                            yield event.plain_result("处理失败，输出文件未生成")
                                except Exception as e:
                                    logger.error(f"处理头像左右横跳时出错: {e}", exc_info=True)
                                    yield event.plain_result(f"处理失败：{str(e)}")
                                finally:
                                    if os.path.exists(safe_file_path):
                                        os.remove(safe_file_path)
                                    if os.path.exists(output_path):
                                        try:
                                            os.remove(output_path)
                                        except:
                                            pass
                            else:
                                yield event.plain_result("牛魔，你引用的图呢？")
                        else:
                            yield event.plain_result("牛魔，你引用的图呢？")
                except Exception as e:
                    logger.error(f"下载头像失败: {e}")
                    yield event.plain_result("牛魔，你引用的图呢？")
            else:
                yield event.plain_result("牛魔，你引用的图呢？")
            return

        async for ret in self._process_image_command(
            event,
            lambda inp, out: image_utils.bounce_gif(inp, out)
                             if image_utils.is_gif(inp)
                             else image_utils.bounce_image(inp, out),
            need_animated=False,
            cmd="bounce",
            force_ext=".gif"  # 强制输出为 GIF 格式
        ):
            yield ret
    @filter.command("添加")
    async def add_meme(self, event: AstrMessageEvent):
        target_image = self._extract_image_from_event(event)
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