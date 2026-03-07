import os
import random
import re
import shutil
from datetime import datetime
from PIL import Image as PILImage, ImageSequence
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image
from astrbot.api import logger

@register("astrbot_plugin_nailong", "Logxk", "一个发送奶龙表情包的插件", "1.0.0")
class MemePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_dir = os.path.dirname(__file__)
        self.meme_dir = os.path.join(self.plugin_dir, "resources")
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        self._ensure_dirs()
        self.meme_list = self._scan_memes()

    def _ensure_dirs(self):
        if not os.path.exists(self.meme_dir):
            os.makedirs(self.meme_dir)
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

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
        try:
            now = datetime.now().timestamp()
            for file in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, file)
                if os.path.isfile(file_path):
                    if now - os.path.getmtime(file_path) > max_age_hours * 3600:
                        os.remove(file_path)
        except Exception as e:
            logger.error(f"清理临时文件时出错: {e}")

    def _is_gif(self, file_path):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == '.gif':
            return True
        try:
            with open(file_path, 'rb') as f:
                header = f.read(6)
                return header.startswith(b'GIF87a') or header.startswith(b'GIF89a')
        except:
            return False

    def _is_animated_gif(self, gif_path):
        try:
            with PILImage.open(gif_path) as img:
                if img.format == 'GIF':
                    frame_count = 0
                    for _ in ImageSequence.Iterator(img):
                        frame_count += 1
                        if frame_count > 1:
                            return True
                return False
        except:
            return False

    # ---------- 核心处理：保留原始帧，只改duration ----------
    def _process_gif_preserve(self, gif_path):
        """提取GIF的原始帧和元数据，包括每帧的透明色"""
        frames = []
        durations = []
        disposals = []
        frame_transparencies = []  # 每帧的透明色索引，若无则为None
        loop = 0
        try:
            with PILImage.open(gif_path) as img:
                loop = img.info.get('loop', 0)
                for frame in ImageSequence.Iterator(img):
                    frame_copy = frame.copy()
                    frames.append(frame_copy)
                    durations.append(frame.info.get('duration', 100))
                    disposals.append(frame.info.get('disposal', 2))
                    # 记录该帧的透明色索引（如果存在）
                    transparency = frame.info.get('transparency')
                    frame_transparencies.append(transparency)
                return frames, durations, disposals, loop, frame_transparencies
        except Exception as e:
            logger.error(f"处理GIF时出错: {e}")
            return None, None, None, None, None

    def _save_gif_preserve(self, frames, durations, disposals, loop, frame_transparencies, output_path):
        """保存GIF，只修改duration，保留原始像素和透明色，强制disposal=2避免闪烁"""
        if not frames:
            return False
        try:
            # 构建透明色列表，将None替换为-1（表示无透明色）
            transparency_list = []
            for t in frame_transparencies:
                if t is None:
                    transparency_list.append(-1)
                else:
                    transparency_list.append(t)

            save_kwargs = {
                'save_all': True,
                'append_images': frames[1:],
                'duration': durations,
                'loop': loop,
                'disposal': 2,                # 强制清除到背景色，避免残留闪烁
                'optimize': False,             # 不优化以保留原始数据
            }
            # 只有当至少有一帧有透明色时才传递transparency参数
            if any(t != -1 for t in transparency_list):
                save_kwargs['transparency'] = transparency_list
                # 背景色设为0，通常与透明色一致（很多GIF背景色就是0）
                save_kwargs['background'] = 0

            frames[0].save(output_path, **save_kwargs)
            return os.path.exists(output_path)
        except Exception as e:
            logger.error(f"保存GIF时出错: {e}")
            # 降级：完全不处理透明度
            try:
                frames[0].save(
                    output_path,
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=loop,
                    disposal=2,
                    optimize=False
                )
                return os.path.exists(output_path)
            except:
                return False

    def _adjust_gif_speed(self, gif_path, speed_factor, output_path):
        try:
            if not self._is_gif(gif_path):
                return False

            frames, durations, disposals, loop, frame_transparencies = self._process_gif_preserve(gif_path)
            if not frames or len(frames) <= 1:
                return False

            # 调整速度，确保最短20ms
            new_durations = [max(20, int(d / speed_factor)) for d in durations]

            return self._save_gif_preserve(frames, new_durations, disposals, loop, frame_transparencies, output_path)
        except Exception as e:
            logger.error(f"变速失败: {e}")
            return False

    def _reverse_gif(self, gif_path, output_path):
        try:
            if not self._is_gif(gif_path):
                return False

            frames, durations, disposals, loop, frame_transparencies = self._process_gif_preserve(gif_path)
            if not frames or len(frames) <= 1:
                return False

            # 反转所有列表
            frames.reverse()
            durations.reverse()
            disposals.reverse()
            frame_transparencies.reverse()

            return self._save_gif_preserve(frames, durations, disposals, loop, frame_transparencies, output_path)
        except Exception as e:
            logger.error(f"倒放失败: {e}")
            return False

    # ---------- 命令处理 ----------
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

    async def _get_image_file(self, target_image):
        file_path = None
        if hasattr(target_image, 'file') and target_image.file:
            file_name = target_image.file
            possible_paths = [
                file_name,
                os.path.join(os.path.dirname(self.plugin_dir), 'data', 'image', file_name),
                os.path.join('data', 'image', file_name),
                os.path.join(self.plugin_dir, 'resources', file_name),
            ]
            for path in possible_paths:
                if os.path.exists(path):
                    file_path = path
                    break

        if not file_path and hasattr(target_image, 'url') and target_image.url:
            import aiohttp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            download_path = os.path.join(self.temp_dir, f"download_{timestamp}.gif")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(target_image.url) as resp:
                        if resp.status == 200:
                            with open(download_path, 'wb') as f:
                                f.write(await resp.read())
                            if self._is_gif(download_path):
                                file_path = download_path
                            else:
                                os.remove(download_path)
                                return None, "下载的文件不是GIF格式"
                        else:
                            return None, f"下载图片失败，HTTP状态码: {resp.status}"
            except Exception as e:
                return None, f"下载图片失败: {str(e)}"
        return file_path, None

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

        target_image = None
        for seg in event.message_obj.message:
            if type(seg).__name__ == 'Reply':
                if hasattr(seg, 'chain') and seg.chain:
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            target_image = reply_seg
                            break
            elif isinstance(seg, Image):
                target_image = seg

        if not target_image:
            yield event.plain_result("请引用一个GIF动图，或在消息中附带一个GIF动图。\n使用方法：引用GIF后发送 /变速 2")
            return

        try:
            file_path, error_msg = await self._get_image_file(target_image)
            if error_msg:
                yield event.plain_result(error_msg)
                return
            if not file_path:
                yield event.plain_result("无法找到图片文件")
                return
            if not os.path.exists(file_path):
                yield event.plain_result("图片文件不存在")
                return
            if not self._is_gif(file_path):
                yield event.plain_result("目前只支持GIF动图变速")
                return
            if not self._is_animated_gif(file_path):
                yield event.plain_result("检测到静态图片，目前只支持GIF动图变速")
                return

            self._clean_temp_files()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            speed_text = f"{speed_factor}x".replace('.', '_')
            output_filename = f"speed_{speed_text}_{timestamp}.gif"
            output_path = os.path.join(self.temp_dir, output_filename)

            yield event.plain_result(f"⏳ 正在处理GIF，请稍候...")
            if self._adjust_gif_speed(file_path, speed_factor, output_path):
                speed_display = f"{speed_factor}倍速" if speed_factor > 1 else f"{1/speed_factor}倍慢速"
                img_seg = Image.fromFileSystem(output_path)
                yield event.chain_result([img_seg])
                yield event.plain_result(f"✅ 已转换为 {speed_display} 模式")
            else:
                yield event.plain_result("❌ GIF处理失败，请确保文件是有效的GIF动图格式")
        except Exception as e:
            logger.error(f"处理GIF时出错: {e}")
            yield event.plain_result(f"❌ 处理失败：{str(e)}")

    @filter.command("倒放")
    async def reverse_gif(self, event: AstrMessageEvent):
        target_image = None
        for seg in event.message_obj.message:
            if type(seg).__name__ == 'Reply':
                if hasattr(seg, 'chain') and seg.chain:
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            target_image = reply_seg
                            break
            elif isinstance(seg, Image):
                target_image = seg

        if not target_image:
            yield event.plain_result("请引用一个GIF动图，或在消息中附带一个GIF动图。\n使用方法：引用GIF后发送 /倒放")
            return

        try:
            file_path, error_msg = await self._get_image_file(target_image)
            if error_msg:
                yield event.plain_result(error_msg)
                return
            if not file_path:
                yield event.plain_result("无法找到图片文件")
                return
            if not os.path.exists(file_path):
                yield event.plain_result("图片文件不存在")
                return
            if not self._is_gif(file_path):
                yield event.plain_result("目前只支持GIF动图倒放")
                return
            if not self._is_animated_gif(file_path):
                yield event.plain_result("检测到静态图片，目前只支持GIF动图倒放")
                return

            self._clean_temp_files()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"reverse_{timestamp}.gif"
            output_path = os.path.join(self.temp_dir, output_filename)

            yield event.plain_result(f"⏳ 正在处理GIF倒放，请稍候...")
            if self._reverse_gif(file_path, output_path):
                img_seg = Image.fromFileSystem(output_path)
                yield event.chain_result([img_seg])
                yield event.plain_result(f"✅ GIF倒放完成")
            else:
                yield event.plain_result("❌ GIF倒放失败，请确保文件是有效的GIF动图格式")
        except Exception as e:
            logger.error(f"处理GIF倒放时出错: {e}")
            yield event.plain_result(f"❌ 处理失败：{str(e)}")

    @filter.command("添加")
    async def add_meme(self, event: AstrMessageEvent):
        images = []
        for seg in event.message_obj.message:
            if isinstance(seg, Image):
                images.append(seg)
            elif type(seg).__name__ == 'Reply':
                if hasattr(seg, 'chain') and seg.chain:
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            images.append(reply_seg)

        if not images:
            yield event.plain_result("请发送一张图片，或引用一张包含图片的消息。\n使用方法：/添加 [图片]")
            return

        image_seg = images[0]
        try:
            saved = False
            filename = None
            if hasattr(image_seg, 'file') and image_seg.file:
                file_path = image_seg.file
                if file_path and isinstance(file_path, str):
                    possible_paths = [
                        file_path,
                        os.path.join(os.path.dirname(self.plugin_dir), 'data', 'image', file_path),
                        os.path.join('data', 'image', file_path),
                    ]
                    for path in possible_paths:
                        if os.path.exists(path):
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            ext = os.path.splitext(path)[1]
                            if not ext:
                                ext = '.jpg'
                            filename = f"meme_{timestamp}{ext}"
                            target_path = os.path.join(self.meme_dir, filename)
                            shutil.copy2(path, target_path)
                            saved = True
                            break

            if not saved and hasattr(image_seg, 'url') and image_seg.url:
                import aiohttp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                url_path = image_seg.url.split('?')[0]
                ext = os.path.splitext(url_path)[1]
                if not ext:
                    ext = '.jpg'
                filename = f"meme_{timestamp}{ext}"
                file_path = os.path.join(self.meme_dir, filename)
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(image_seg.url) as resp:
                            if resp.status == 200:
                                with open(file_path, 'wb') as f:
                                    f.write(await resp.read())
                                saved = True
                except Exception as e:
                    logger.error(f"下载图片时出错: {e}")
                    yield event.plain_result(f"下载图片失败: {str(e)}")
                    return

            if saved:
                self.meme_list = self._scan_memes()
                yield event.plain_result(f"✅ 表情包添加成功！\n文件名：{filename}\n当前共有 {len(self.meme_list)} 个表情包。")
            else:
                yield event.plain_result("无法保存图片，请尝试直接发送图片（不要引用）")
        except Exception as e:
            logger.error(f"添加表情包时出错: {e}")
            yield event.plain_result(f"❌ 添加失败：{str(e)}")

    async def terminate(self):
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
        except Exception as e:
            logger.error(f"清理临时文件夹时出错: {e}")
